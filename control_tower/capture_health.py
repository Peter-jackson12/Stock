"""Manager-local liveness clock; persisted old reports cannot start it fresh."""
from dataclasses import asdict
import math
import time

from control_tower.managed_capture import ACTIVE
from control_tower.windows_process import WindowsProcess


class CaptureHealth:
    def __init__(self, *, clock=time.monotonic, timeout=15, process_factory=WindowsProcess):
        if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 1 <= timeout <= 60:
            raise ValueError("heartbeat timeout must be 1..60 seconds")
        self.clock, self.timeout, self.process_factory = clock, timeout, process_factory
        self.launch = self.nonce = self.last_seq = self.last_progress = None
        self.issued = None

    def observe(self, store, current):
        now = self.clock()
        if current is None or current["state"] not in ACTIVE:
            self.launch = self.nonce = self.last_progress = None
            return dict(state="inactive", reason="no active managed session")
        owner = current["owner"]
        if owner is None:
            return dict(state="unknown", reason="child has not claimed launch")
        try:
            with self.process_factory(owner["pid"]) as process:
                if asdict(process.facts) != owner:
                    raise ValueError("process identity changed")
                process.require_alive()
        except (OSError, ValueError) as exc:
            self.launch = self.nonce = self.last_progress = None
            return dict(state="unknown", reason=f"OS identity/liveness: {exc}")
        expired = self.issued is not None and now - (self.last_progress if self.last_progress is not None else self.issued) >= self.timeout
        if self.launch != current["id"] or self.nonce is None or expired:
            self.nonce, self.last_seq = store.challenge(current["id"], owner)
            self.launch, self.issued, self.last_progress = current["id"], now, None
            return dict(state="unknown", reason="awaiting a new challenge response")
        if current.get("challenge_ack") != self.nonce:
            return dict(state="unknown", reason="current manager challenge is unacknowledged")
        seq = current.get("heartbeat_seq", 0)
        if type(seq) is not int or seq < self.last_seq:
            self.nonce = self.last_progress = None
            return dict(state="unknown", reason="peer heartbeat sequence reversed")
        if seq > self.last_seq:
            self.last_seq, self.last_progress = seq, now
        if self.last_progress is None:
            return dict(state="unknown", reason="no new peer progress")
        return dict(state="responsive", reason="OS identity and current challenge verified",
                    age_seconds=round(now - self.last_progress, 3), heartbeat_seq=seq,
                    data_quality="unverified")
