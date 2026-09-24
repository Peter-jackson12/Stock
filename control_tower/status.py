"""Bounded log observations. Fresh logs are evidence, not process/quality proof."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import json

from control_tower.lifecycle import ProcessIdentity, Finalization

KST = timezone(timedelta(hours=9))
MAX_LOG_BYTES = 64 * 1024
HEARTBEAT = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*체결:\s*([\d,]+)건\s*\|\s*호가:\s*([\d,]+)건.*대기큐:\s*([\d,]+)"
)


def observe_collector(root, *, now=None):
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        raise ValueError("timezone-aware observation time required")
    now = now.astimezone(KST)
    path = Path(root) / "logs" / f"kiwoom_universe_{now:%Y%m%d}.log"
    observation = dict(status="unavailable", observed_at=now.isoformat(), log_path=str(path),
                       process_state="unverified", control="external", heartbeat=None, recent_messages=[])
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - MAX_LOG_BYTES)
            stream.seek(offset)
            data = stream.read(MAX_LOG_BYTES)
        lines = data.decode("utf-8", errors="replace").splitlines()
        if offset and lines:
            lines = lines[1:]
    except OSError as exc:
        return observation | {"reason": type(exc).__name__}
    samples = []
    for line in lines:
        match = HEARTBEAT.search(line)
        if match:
            try:
                stamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
                counts = [int(x.replace(",", "")) for x in match.groups()[1:]]
            except ValueError:
                continue
            samples.append(dict(time=stamp.isoformat(), trades=counts[0], quotes=counts[1], queue=counts[2],
                                counts_kind="callbacks" if "수신 콜백" in line else "legacy_rows"))
    observation["recent_messages"] = [line for line in lines if any(
        marker in line for marker in ("실패", "오류", "침묵", "적체", "종료"))][-5:]
    if not samples:
        return observation | {"status": "no_heartbeat", "reason": "tail_has_no_parseable_heartbeat"}
    latest = samples[-1]
    age = (now - datetime.fromisoformat(latest["time"])).total_seconds()
    return observation | dict(status="clock_ahead" if age < -5 else "recent" if age <= 180 else "stale",
                              heartbeat=latest, age_seconds=round(age, 1))


def validate_raw_capture_payload(payload):
    """Validate a producer status claim without I/O; not storage/feed certification.

    Shared by the bounded reader and the presentation reducer. Validation must
    not be bypassed by supplying an arbitrary dictionary to the latter.
    """
    if payload["status_schema"] != "raw_capture_status_v1" or payload["control_heartbeat"] is not False:
        raise ValueError("unsupported status schema")
    identity = ProcessIdentity(**payload["identity"])
    stamp = datetime.fromisoformat(payload["observed_at_utc"].replace("Z", "+00:00"))
    if stamp.utcoffset() != timedelta(0):
        raise ValueError("explicit UTC observation required")
    snap = payload["snapshot"]
    for error in (payload["error"], snap["error"]):
        if error is not None and not isinstance(error, str):
            raise ValueError("invalid status error")
    if (snap["session_id"] != identity.session_id or snap["dataset_path"] != identity.dataset_path
            or snap["feed_scope"] != identity.feed_scope):
        raise ValueError("status identity mismatch")
    for key in ("accepted_callbacks", "committed_callbacks", "queued", "in_flight", "pending_callbacks", "dropped_callbacks", "committed_seq"):
        if type(snap[key]) is not int or snap[key] < 0:
            raise ValueError("invalid status counter")
    if (snap["accepted_callbacks"] - snap["committed_callbacks"] != snap["pending_callbacks"]
            or snap["queued"] + snap["in_flight"] != snap["pending_callbacks"]):
        raise ValueError("status callback accounting mismatch")
    if snap["state"] not in ("starting", "running", "draining", "closed", "interrupted", "failed"):
        raise ValueError("unknown status state")
    if snap["state"] == "closed":
        final = Finalization(**snap["finalization"])
        if (snap["writer_closed"] is not True or snap["pending_callbacks"] or snap["dropped_callbacks"]
                or final.final_seq != snap["committed_seq"]):
            raise ValueError("inconsistent closed status")
    return identity, stamp


def observe_raw_capture(root, *, now=None):
    """Read one bounded status file, not raw DBs; no liveness/quality attestation."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("timezone-aware observation time required")
    path = Path(root) / "operations_state" / "capture_status.json"
    result = dict(status="unavailable", path=str(path), process_state="unverified", data_quality="unverified")
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_LOG_BYTES + 1)
        if len(data) > MAX_LOG_BYTES:
            raise ValueError("status file exceeds size limit")
        payload = json.loads(data)
        _, stamp = validate_raw_capture_payload(payload)
        age = (now - stamp).total_seconds()
        return result | dict(status="clock_ahead" if age < -5 else "recent" if age <= 30 else "stale",
                             age_seconds=round(age, 1), payload=payload)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return result | {"reason": str(exc)}
