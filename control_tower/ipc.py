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


class CaptureConnection:
    """Bind transport failures to durable uncertainty, never to clean shutdown.

    Serial manager-thread use only. Freshness begins when a complete report has
    arrived, not before a blocking receive. Lost/malformed frames require explicit
    reconnection and reconciliation; this adapter never retries a stop.
    """

    def __init__(self, capture, channel, *, clock=time.monotonic_ns):
        self.capture, self.channel, self.clock = capture, channel, clock

    def receive(self):
        try:
            report = self.channel.receive_report()
            return self.capture.receive(report, now_ns=self.clock())
        except BaseException:
            self.channel.close()
            self.capture.lost_contact(self.capture.identity,
                                      reason="control report unavailable or invalid",
                                      now_ns=self.clock())
            raise

    def dispatch_stop(self):
        self.capture.dispatch_stop(self.channel.send_stop, now_ns=self.clock())
