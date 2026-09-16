"""Bounded raw-v2 callback queue; no OCX calls or production collector wiring.

The producer supplies callback-entry timestamps and raw fields. Only the worker
creates/uses/closes CaptureSession and SQLite. Queue acceptance is not persistence.
Overflow interrupts capture; queued input and an error marker are drained, but
the file remains incomplete. Actual callback latency/load still needs measurement.
"""
from collections import deque
import json
import threading

from collector.kiwoom.capture_session import CaptureSession


class QueuedCapture:
    def __init__(self, path, *, capacity=1024, batch_size=128, max_packet_bytes=65536,
                 session_factory=CaptureSession, **session_options):
        for name, value in (("capacity", capacity), ("batch_size", batch_size),
                            ("max_packet_bytes", max_packet_bytes)):
            if type(value) is not int or value < 1:
                raise ValueError(f"positive integer {name} required")
        started_ns = session_options.get("started_ns")
        if type(started_ns) is not int or started_ns < 0:
            raise ValueError("explicit nonnegative started_ns required")
        self._path, self._options, self._factory = path, dict(session_options), session_factory
        self._capacity, self._batch_size, self._max_bytes = capacity, batch_size, max_packet_bytes
        self._condition = threading.Condition()
        self._queue = deque()
        self._batch = []
        self._thread = None
        self._state, self._error = "new", None
        self._accepting = self._stopping = self._interrupted = False
        self._terminal = None
        self._close_ns = None
        self._last_ns = started_ns
        self._accepted = self._committed = self._dropped = self._committed_seq = 0
        self.ready, self.done = threading.Event(), threading.Event()

    def start(self):
        with self._condition:
            if self._thread is not None:
                raise ValueError("new session/file required to restart")
            self._state = "starting"
            self._thread = threading.Thread(target=self._run, name="raw-v2-writer", daemon=False)
            self._thread.start()
        return self

    def submit_tick(self, *, code, venue, real_type, fids, received_ns, received_at_utc):
        """Copy raw input into the FIFO without disk I/O or waiting for free space.

        Returns False on overflow/unserializable input and stops accepting input.
        Calls and request_stop are serialized by one short condition lock.
        """
        with self._condition:
            if not self._accepting:
                raise ValueError("capture is not accepting input")
            if type(received_ns) is not int or received_ns < self._last_ns:
                self._accepting, self._stopping, self._interrupted = False, True, True
                self._state, self._error = "interrupted", "callback receipt clock reversed or invalid"
                self._dropped += 1
                self._condition.notify_all()
                raise ValueError("callback receipt clock reversed or invalid")
            packet = dict(code=code, venue=venue, real_type=real_type, fids=fids,
                          received_ns=received_ns, received_at_utc=received_at_utc)
            reason = None
            try:
                if not isinstance(fids, dict) or any(not isinstance(k, str) or
                        (v is not None and not isinstance(v, str)) for k, v in fids.items()):
                    raise ValueError("FID keys/values must be raw text or null values")
                encoded = json.dumps(packet, ensure_ascii=False, allow_nan=False)
                if len(encoded.encode("utf-8")) > self._max_bytes:
                    raise ValueError("callback packet exceeds size limit")
                packet = json.loads(encoded)  # detach caller-owned nested values
            except (TypeError, ValueError) as exc:
                reason = f"{type(exc).__name__}: {exc}"[:512]
            self._last_ns = received_ns
            if reason is not None or len(self._queue) >= self._capacity:
                self._accepting, self._stopping, self._interrupted = False, True, True
                self._state = "interrupted"
                self._dropped += 1
                self._terminal = ("callback_error" if reason else "queue_overflow",
                                  received_ns, received_at_utc,
                                  {"lost_callbacks": 1, "reason": reason or "queue capacity exceeded"})
                self._condition.notify_all()
                return False
            self._queue.append(packet)
            self._accepted += 1
            self._condition.notify_all()
            return True

    def request_stop(self, close_ns):
        """Stop acceptance atomically; completion is reported only after worker close."""
        with self._condition:
            if self._stopping and self._close_ns == close_ns and not self._interrupted:
                return
            if not self._accepting:
                raise ValueError("capture cannot be finalized")
            if type(close_ns) is not int or close_ns <= self._last_ns:
                raise ValueError("exclusive close boundary must follow all accepted input")
            self._close_ns = close_ns
            self._accepting, self._stopping, self._state = False, True, "draining"
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            return dict(state=self._state, accepting=self._accepting, error=self._error,
                        accepted_callbacks=self._accepted, committed_callbacks=self._committed,
                        queued=len(self._queue), in_flight=len(self._batch), dropped_callbacks=self._dropped,
                        pending_callbacks=self._accepted - self._committed,
                        committed_seq=self._committed_seq, data_quality="unverified")

    def abort(self, reason="capture aborted"):
        """Request incomplete shutdown, including while writer startup is pending."""
        with self._condition:
            if self._thread is None:
                raise ValueError("capture has not started")
            if self._state in ("closed", "failed"):
                return
            self._accepting, self._stopping, self._interrupted = False, True, True
            self._state, self._error = "interrupted", str(reason)[:512]
            self._condition.notify_all()

    def wait(self, timeout):
        """A timeout leaves the worker running; it never means successful closure."""
        return self.done.wait(timeout)

    def _run(self):
        try:
            with self._factory(self._path, **self._options) as session:
                with self._condition:
                    self._committed_seq = session.writer.count
                    if not self._stopping:
                        self._state, self._accepting = "running", True
                    self.ready.set()
                while True:
                    with self._condition:
                        self._condition.wait_for(lambda: self._queue or self._stopping)
                        self._batch = [self._queue.popleft() for _ in range(min(len(self._queue), self._batch_size))]
                        batch = self._batch
                        if not batch:
                            break
                    for packet in batch:
                        if session.state == "interrupted":
                            # Preserve already accepted callbacks after an earlier callback error.
                            session.control("callback_error", packet["received_ns"], packet["received_at_utc"],
                                            {"reason": "queued after interruption", "callback": packet})
                        else:
                            session.on_tick(**packet)
                        if session.state == "interrupted":
                            with self._condition:
                                self._accepting, self._stopping, self._interrupted = False, True, True
                                self._state = "interrupted"
                    session.commit()
                    with self._condition:
                        self._committed += len(batch)
                        self._committed_seq = session.writer.count
                        self._batch = []
                if self._terminal is not None:
                    session.control(*self._terminal)
                    session.commit()
                    with self._condition:
                        self._committed_seq = session.writer.count
                if not self._interrupted:
                    session.finish(self._close_ns)
            # Publish closed only after SQLite context exit also succeeds.
            with self._condition:
                self._state = "interrupted" if self._interrupted else "closed"
        except BaseException as exc:
            with self._condition:
                self._accepting = False
                self._state, self._error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            self.ready.set()
            self.done.set()
