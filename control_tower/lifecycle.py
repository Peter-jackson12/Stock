"""Versioned capture-control contract, exercised with fake peers only.

Single-owner reducer: a future serialized transport must deliver reports/commands.
No OS process API, OCX, raw-file I/O, UI action or automatic restart lives here.
Reports are producer claims; a closed writer is not a dataset quality certificate.
All deadlines use the manager's monotonic clock, never provider/wall timestamps.
"""
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
import json
from pathlib import PurePosixPath, PureWindowsPath
import re

SCHEMA = "capture_control_v1"
MAX_MESSAGE_BYTES = 32 * 1024
TRANSITIONS = {
    "starting": {"login_required", "draining", "interrupted", "failed"},
    "login_required": {"subscribed", "draining", "interrupted", "failed"},
    "subscribed": {"receiving", "draining", "interrupted", "failed"},
    "receiving": {"draining", "interrupted", "failed"},
    "interrupted": {"draining", "failed"},
    "draining": {"closed", "interrupted", "failed"},
    "closed": set(),
    "failed": set(),
}


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"invalid {name}")


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError(f"invalid {name}")


def _utc(value):
    _text(value, "UTC timestamp")
    if datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() != timedelta(0):
        raise ValueError("explicit UTC timestamp required")


@dataclass(frozen=True)
class ProcessIdentity:
    session_id: str
    pid: int
    started_at_utc: str
    executable: str
    code_revision: str
    python_bits: int
    server: str
    feed_scope: str
    dataset_path: str

    def __post_init__(self):
        for name in ("session_id", "executable", "code_revision", "feed_scope", "dataset_path"):
            _text(getattr(self, name), name)
        _integer(self.pid, "pid", 1)
        _utc(self.started_at_utc)
        if type(self.python_bits) is not int or self.python_bits not in (32, 64):
            raise ValueError("32/64-bit environment required")
        if self.server not in ("mock", "live", "fixture"):
            raise ValueError("explicit server required")
        for path in (self.executable, self.dataset_path):
            if not (PureWindowsPath(path).is_absolute() or PurePosixPath(path).is_absolute()):
                raise ValueError("absolute identity paths required")


@dataclass(frozen=True)
class Finalization:
    final_seq: int
    close_ns: int
    payload_sha256: str

    def __post_init__(self):
        _integer(self.final_seq, "final_seq")
        _integer(self.close_ns, "close_ns", 1)
        if not isinstance(self.payload_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.payload_sha256):
            raise ValueError("SHA-256 producer claim required")


@dataclass(frozen=True)
class CaptureReport:
    identity: ProcessIdentity
    revision: int
    state: str
    callback_count: int = 0
    accepted_seq: int = 0
    committed_seq: int = 0
    queued: int = 0
    in_flight: int = 0
    dropped: int = 0
    write_failures: int = 0
    last_event_ns: int | None = None
    last_commit_at_utc: str | None = None
    input_stopped: bool = False
    writer_closed: bool = False
    finalization: Finalization | None = None
    stop_request_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.identity, ProcessIdentity) or self.state not in TRANSITIONS:
            raise ValueError("invalid report identity/state")
        _integer(self.revision, "revision", 1)
        for name in ("callback_count", "accepted_seq", "committed_seq", "queued", "in_flight", "dropped", "write_failures"):
            _integer(getattr(self, name), name)
        if self.committed_seq > self.accepted_seq or self.queued + self.in_flight != self.accepted_seq - self.committed_seq:
            raise ValueError("queue/in-flight/commit accounting mismatch")
        if self.accepted_seq:
            _integer(self.last_event_ns, "last_event_ns")
        elif self.last_event_ns is not None:
            raise ValueError("empty stream cannot have a last event")
        if self.committed_seq:
            _utc(self.last_commit_at_utc)
        elif self.last_commit_at_utc is not None:
            raise ValueError("no committed events")
        if type(self.input_stopped) is not bool or type(self.writer_closed) is not bool:
            raise ValueError("explicit stop/close booleans required")
        if (self.input_stopped or self.writer_closed or self.dropped or self.write_failures) and self.state in (
                "starting", "login_required", "subscribed", "receiving"):
            raise ValueError("active state conflicts with stop/error evidence")
        if self.writer_closed and self.state == "draining":
            raise ValueError("closed writer requires a final or error report")
        if self.stop_request_id is not None:
            _text(self.stop_request_id, "stop_request_id")
            if self.state not in ("draining", "closed", "interrupted", "failed"):
                raise ValueError("stop acknowledgement requires stop/error state")
        if self.state in ("draining", "closed") and not self.input_stopped:
            raise ValueError("input must be stopped before drain/close")
        if self.state == "closed":
            if (not self.writer_closed or self.queued or self.in_flight or self.dropped or self.write_failures
                    or not isinstance(self.finalization, Finalization)):
                raise ValueError("clean final commit and closed writer required")
            if self.finalization.final_seq != self.accepted_seq or self.finalization.close_ns <= (self.last_event_ns or 0):
                raise ValueError("finalization does not match accepted stream")
        elif self.finalization is not None:
            raise ValueError("finalization belongs to closed state only")


def encode_report(report):
    if not isinstance(report, CaptureReport):
        raise ValueError("capture report required")
    text = json.dumps({"schema": SCHEMA, **asdict(report)}, allow_nan=False, sort_keys=True)
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("report exceeds size limit")
    return text


def decode_report(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("bounded report text required")
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or data.pop("schema", None) != SCHEMA:
            raise ValueError("unsupported control schema")
        if set(data) != {field.name for field in fields(CaptureReport)}:
            raise ValueError("report fields do not match schema")
        data["identity"] = ProcessIdentity(**data["identity"])
        if data["finalization"] is not None:
            data["finalization"] = Finalization(**data["finalization"])
        return CaptureReport(**data)
    except (TypeError, KeyError) as exc:
        raise ValueError("malformed control report") from exc


@dataclass(frozen=True)
class StopCommand:
    request_id: str
    target: ProcessIdentity
    expected_revision: int

    def __post_init__(self):
        _text(self.request_id, "request_id")
        if not isinstance(self.target, ProcessIdentity):
            raise ValueError("explicit command target required")
        _integer(self.expected_revision, "expected_revision", 1)


def encode_stop(command):
    if not isinstance(command, StopCommand):
        raise ValueError("stop command required")
    text = json.dumps({"schema": SCHEMA, "command": "stop", **asdict(command)}, allow_nan=False, sort_keys=True)
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("command exceeds size limit")
    return text


def decode_stop(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("bounded command text required")
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or data.pop("schema", None) != SCHEMA or data.pop("command", None) != "stop":
            raise ValueError("unsupported control command")
        if set(data) != {"request_id", "target", "expected_revision"}:
            raise ValueError("command fields do not match schema")
        data["target"] = ProcessIdentity(**data["target"])
        return StopCommand(**data)
    except (TypeError, KeyError) as exc:
        raise ValueError("malformed stop command") from exc


class StopReceiver:
    """Peer-side latch: stop input exactly once per in-memory session instance.

    A future adapter invokes its stop procedure only when accept returns True.
    Duplicates return False, so it should resend the latest cached report instead.
    """
    def __init__(self, identity):
        if not isinstance(identity, ProcessIdentity):
            raise ValueError("explicit receiver identity required")
        self.identity, self.command = identity, None

    def accept(self, command, report):
        if not isinstance(command, StopCommand) or not isinstance(report, CaptureReport):
            raise ValueError("typed command and report required")
        if command.target != self.identity or report.identity != self.identity:
            raise ValueError("process/session identity mismatch")
        if self.command is not None:
            if command != self.command:
                raise ValueError("conflicting stop command")
            return False
        if command.expected_revision > report.revision or report.state in ("closed", "failed"):
            raise ValueError("command does not match controllable report")
        self.command = command
        return True


class CaptureLifecycle:
    """In-memory manager for ONE explicitly registered session; no discovery/adoption.

    Calls must be serialized by one owner. The durable adapter replays validated
    control history on restart; PID/logs alone never reconstruct this instance.
    revision counts all protocol reports, including heartbeats; delivery is ordered.
    """
    def __init__(self, identity, *, heartbeat_timeout_ns):
        if not isinstance(identity, ProcessIdentity):
            raise ValueError("explicit managed identity required")
        _integer(heartbeat_timeout_ns, "heartbeat_timeout_ns", 1)
        self.identity, self.heartbeat_timeout_ns = identity, heartbeat_timeout_ns
        self.report = None
        self.stop_command = None
        self._stop_deadline = None
        self._stop_acknowledged = False
        self._stop_completed = False
        self._last_seen = None
        self._clock = -1
        self._lost_reason = None

    def _now(self, now_ns):
        _integer(now_ns, "manager monotonic clock")
        if now_ns < self._clock:
            raise ValueError("manager clock reversed")
        self._clock = now_ns

    def _target(self, identity):
        if identity != self.identity:
            raise ValueError("process/session identity mismatch")

    def receive(self, report, *, now_ns):
        self._now(now_ns)
        if not isinstance(report, CaptureReport):
            raise ValueError("capture report required")
        self._target(report.identity)
        previous = self.report
        if previous is not None:
            if report == previous:
                return False  # duplicate messages must not keep a dead peer fresh
            if report.revision != previous.revision + 1:
                raise ValueError("ordered contiguous report revision required")
            if report.state != previous.state and report.state not in TRANSITIONS[previous.state]:
                raise ValueError("invalid lifecycle transition")
            for name in ("callback_count", "accepted_seq", "committed_seq", "dropped", "write_failures"):
                if getattr(report, name) < getattr(previous, name):
                    raise ValueError("capture counters cannot decrease")
            if previous.last_event_ns is not None and report.last_event_ns < previous.last_event_ns:
                raise ValueError("event receipt clock reversed")
            if previous.input_stopped and (not report.input_stopped or report.accepted_seq != previous.accepted_seq):
                raise ValueError("cannot resume input after stop; new session required")
            if previous.writer_closed and not report.writer_closed:
                raise ValueError("writer cannot reopen")
            if previous.state == "closed" and report.finalization != previous.finalization:
                raise ValueError("closed finalization cannot change")
        elif (report.revision != 1 or report.state != "starting" or report.callback_count or report.accepted_seq):
            raise ValueError("empty starting handshake required; no process adoption")
        if report.stop_request_id is not None:
            if self.stop_command is None or report.stop_request_id != self.stop_command.request_id:
                raise ValueError("unknown stop acknowledgement")
        if self.stop_command is not None and report.state in ("draining", "closed") and report.stop_request_id != self.stop_command.request_id:
            raise ValueError("matching stop acknowledgement required")
        self.report, self._last_seen, self._lost_reason = report, now_ns, None
        if report.stop_request_id is not None:
            self._stop_acknowledged = True
            self._stop_completed = report.state == "closed"
        return True

    def lost_contact(self, identity, *, reason, now_ns):
        self._now(now_ns)
        self._target(identity)
        _text(reason, "contact loss reason")
        self._lost_reason = reason

    def manager_restarted(self):
        """Invalidate the previous manager clock and liveness after history replay.

        Unfinished stops retain their identity but no reusable deadline. Only a
        matching final/error report can resolve their unknown outcome.
        """
        self._clock = -1
        self._last_seen = None
        self._lost_reason = "manager restarted; reconciliation required"
        if self.stop_command is not None:
            self._stop_deadline = 0

    def view(self, *, now_ns):
        self._now(now_ns)
        age = None if self._last_seen is None else now_ns - self._last_seen
        # A verified protocol close remains a recorded outcome after the peer exits.
        closed = self.report is not None and self.report.state == "closed"
        uncertain = age is None or self._lost_reason is not None or age >= self.heartbeat_timeout_ns
        state = "closed" if closed else "unknown" if uncertain else self.report.state
        command_status = None
        if self.stop_command is not None:
            failed = self.report is not None and (self.report.state == "failed" or self.report.dropped or self.report.write_failures)
            if self._stop_completed:
                command_status = "completed"
            elif failed:
                command_status = "failed"
            elif uncertain or now_ns >= self._stop_deadline:
                command_status = "unknown"
            else:
                command_status = "acknowledged" if self._stop_acknowledged else "pending"
        return dict(state=state, last_reported_state=self.report.state if self.report else None,
                    age_ns=age, contact_loss=self._lost_reason, stop_status=command_status,
                    data_quality="unverified", automatic_restart=False)

    def request_stop(self, request_id, target, *, now_ns, timeout_ns):
        self._now(now_ns)
        self._target(target)
        _text(request_id, "request_id")
        _integer(timeout_ns, "stop timeout", 1)
        if self.stop_command is not None:
            if request_id != self.stop_command.request_id:
                raise ValueError("stop already requested; reuse its request_id")
            return self.stop_command  # timeout/retry never resets the original deadline
        if self.view(now_ns=now_ns)["state"] in ("unknown", "closed", "failed"):
            raise ValueError("fresh controllable session required")
        self.stop_command = StopCommand(request_id, self.identity, self.report.revision)
        self._stop_deadline = now_ns + timeout_ns
        return self.stop_command
