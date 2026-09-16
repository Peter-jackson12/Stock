"""Stop/report bridge for a managed raw-v2 queue; no OCX or live heartbeats yet."""
from dataclasses import replace
import math
from pathlib import Path
import time

from control_tower.lifecycle import Finalization, StopReceiver


class QueueStopReports:
    """Publish actual raw sequence only after accepted callbacks finish draining.

    Before then the command remains pending: callback backlog has no exact raw
    sequence count yet. The caller sends each yielded report before resuming the
    generator. Delivery failure must close the generator (or abort the queue).
    A held worker independently times out after five seconds, leaving incomplete.
    """

    def __init__(self, queue, identity):
        snapshot = queue.snapshot()
        if (Path(snapshot["dataset_path"]) != Path(identity.dataset_path)
                or snapshot["session_id"] != identity.session_id
                or snapshot["feed_scope"] != identity.feed_scope):
            raise ValueError("queue/session identity mismatch")
        self.queue, self.identity = queue, identity
        self.receiver = StopReceiver(identity)
        self._started = False

    def stop(self, command, previous, *, close_ns, timeout=5):
        """command=None denotes local shutdown, without a remote stop acknowledgement."""
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 < timeout <= 5:
            raise ValueError("stop report timeout must be in (0, 5] seconds")
        if previous.identity != self.identity:
            raise ValueError("previous report identity mismatch")
        if self._started or (command is not None and not self.receiver.accept(command, previous)):
            raise ValueError("stop already accepted; cannot re-execute")
        self._started = True
        deadline = time.monotonic() + timeout
        completed = False
        try:
            self.queue.request_stop(close_ns, hold_finalize=True)
            if not self.queue.drained.wait(max(0, deadline - time.monotonic())):
                raise TimeoutError("queue drain report unavailable")
            snapshot = self.queue.snapshot()
            if (snapshot["state"] != "draining" or snapshot["pending_callbacks"]
                    or snapshot["dropped_callbacks"] or snapshot["writer_closed"]):
                raise ValueError("clean held drain unavailable")
            if (previous.dropped or previous.write_failures or previous.writer_closed
                    or snapshot["accepted_callbacks"] < previous.callback_count
                    or snapshot["committed_seq"] < previous.accepted_seq
                    or (previous.input_stopped and snapshot["committed_seq"] != previous.accepted_seq)):
                raise ValueError("drain conflicts with previous report")
            draining = replace(previous, revision=previous.revision + 1, state="draining",
                callback_count=snapshot["accepted_callbacks"], accepted_seq=snapshot["committed_seq"],
                committed_seq=snapshot["committed_seq"], queued=0, in_flight=0,
                last_event_ns=snapshot["last_event_ns"], last_commit_at_utc=snapshot["last_commit_at_utc"],
                input_stopped=True, writer_closed=False,
                stop_request_id=None if command is None else command.request_id)
            yield draining
            if time.monotonic() >= deadline:
                raise TimeoutError("stop report deadline exceeded")
            self.queue.complete_stop()
            if not self.queue.wait(max(0, deadline - time.monotonic())):
                raise TimeoutError("writer close report unavailable")
            snapshot = self.queue.snapshot()
            if snapshot["state"] != "closed" or not snapshot["writer_closed"] or snapshot["finalization"] is None:
                raise ValueError("writer did not close cleanly")
            final = replace(draining, revision=draining.revision + 1, state="closed", writer_closed=True,
                            finalization=Finalization(**snapshot["finalization"]))
            completed = True
            yield final
        finally:
            if not completed:
                self.queue.abort("stop report exchange interrupted")
