"""Bounded, single-owner transport for an already established control channel.

This layer does not authenticate, discover, launch or adopt processes. The caller
must supply a trusted socket and an explicitly registered session. Never expose
it as an unauthenticated network listener. Socket writes are not peer receipts.
"""
import math
import struct
import time

from control_tower.lifecycle import (
    MAX_MESSAGE_BYTES, decode_report, decode_stop, encode_report, encode_stop,
)
from control_tower.report_journal import ReplayRequest, encode_request, decode_request, encode_page, decode_page


class ControlChannel:
    """One serial caller owns the socket; any failed frame poisons the channel."""

    def __init__(self, sock, *, timeout=1.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 5:
            raise ValueError("transport timeout must be in (0, 5] seconds")
        self.socket, self.timeout, self.closed = sock, timeout, False

    def close(self):
        if not self.closed:
            self.closed = True
            self.socket.close()

    def _remaining(self, deadline):
        if self.closed:
            raise ConnectionError("control channel closed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("control frame deadline exceeded")
        self.socket.settimeout(remaining)

    def _read(self, size, deadline):
        result = bytearray()
        while len(result) < size:
            self._remaining(deadline)
            chunk = self.socket.recv(size - len(result))
            if not chunk:
                raise EOFError("control channel ended before complete frame")
            result.extend(chunk)
        return bytes(result)

    def _send(self, value, encode):
        try:
            body = encode(value).encode("utf-8")
            packet = struct.pack("!I", len(body)) + body
            self._remaining(time.monotonic() + self.timeout)
            # sendall's socket timeout covers the entire call, not each write.
            self.socket.sendall(packet)
        except BaseException:
            self.close()
            raise

    def _receive(self, decode):
        try:
            deadline = time.monotonic() + self.timeout
            size, = struct.unpack("!I", self._read(4, deadline))
            if not 1 <= size <= MAX_MESSAGE_BYTES:
                raise ValueError("invalid control frame size")
            return decode(self._read(size, deadline).decode("utf-8"))
        except BaseException:
            self.close()
            raise

    def send_stop(self, command):
        self._send(command, encode_stop)

    def receive_stop(self):
        return self._receive(decode_stop)

    def send_report(self, report):
        self._send(report, encode_report)

    def receive_report(self):
        return self._receive(decode_report)

    def send_replay_request(self, request):
        self._send(request, encode_request)

    def receive_replay_request(self):
        return self._receive(decode_request)

    def send_replay_page(self, page):
        self._send(page, encode_page)

    def receive_replay_page(self):
        return self._receive(decode_page)


class CaptureConnection:
    """Bind transport failures to durable uncertainty, never to clean shutdown.

    Serial manager-thread use only. Freshness begins when a complete report has
    arrived, not before a blocking receive. Lost/malformed frames require explicit
    reconnection and reconciliation; this adapter never retries a stop.
    """

    def __init__(self, capture, channel, *, clock=time.monotonic_ns, process=None):
        self.capture, self.channel, self.clock, self.process = capture, channel, clock, process
        if process is not None:
            try:
                process.verify(capture.identity)
            except BaseException:
                channel.close()
                raise

    def receive(self):
        try:
            report = self.channel.receive_report()
            if self.process is not None:
                # Buffered drain/final reports remain inspectable after exit.
                self.process.verify(report.identity, require_alive=False)
            changed = self.capture.receive(report, now_ns=self.clock())
            if self.process is not None and report.state not in ("closed", "failed"):
                try:
                    self.process.require_alive()
                except ProcessLookupError:
                    self.capture.lost_contact(self.capture.identity,
                                              reason="bound process exited; final report required",
                                              now_ns=self.clock())
            return changed
        except BaseException:
            self.channel.close()
            self.capture.lost_contact(self.capture.identity,
                                      reason="control report unavailable or invalid",
                                      now_ns=self.clock())
            raise

    def dispatch_stop(self):
        def send(command):
            try:
                if self.process is not None:
                    self.process.verify(command.target)
                self.channel.send_stop(command)
            except BaseException:
                self.channel.close()
                raise
        self.capture.dispatch_stop(send, now_ns=self.clock())

    def reconcile_page(self, *, limit=128):
        """One bounded request/page; callers explicitly schedule further pages.

        Historical reports do not refresh liveness and never resend stop. The
        same missing page can be requested after failure using durable revision.
        """
        cursor = 0 if self.capture.report is None else self.capture.report.revision
        request = ReplayRequest(self.capture.identity, cursor, limit)
        try:
            self.channel.send_replay_request(request)
            page = self.channel.receive_replay_page()
            if page.identity != request.identity or page.after_revision != cursor or len(page.reports) > limit:
                raise ValueError("replay response does not match request")
            if self.process is not None:
                self.process.verify(page.identity, require_alive=False)
            if page.reports:
                self.capture.reconcile(page.reports, now_ns=self.clock())
            else:
                self.capture.lost_contact(self.capture.identity, reason="empty replay is not a heartbeat", now_ns=self.clock())
            return page
        except BaseException:
            self.channel.close()
            self.capture.lost_contact(self.capture.identity, reason="replay incomplete or invalid", now_ns=self.clock())
            raise
