"""Bounded log observations. Fresh logs are evidence, not process/quality proof."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

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
            lines = lines[1:]  # discard potentially partial line at bounded tail start
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
                continue  # a damaged line must not break the entire operations screen
            samples.append(dict(time=stamp.isoformat(), trades=counts[0], quotes=counts[1], queue=counts[2]))
    observation["recent_messages"] = [line for line in lines if any(
        marker in line for marker in ("실패", "오류", "침묵", "적체", "종료"))][-5:]
    if not samples:
        return observation | {"status": "no_heartbeat", "reason": "tail_has_no_parseable_heartbeat"}
    latest = samples[-1]
    age = (now - datetime.fromisoformat(latest["time"])).total_seconds()
    return observation | dict(status="clock_ahead" if age < -5 else "recent" if age <= 180 else "stale",
                              heartbeat=latest, age_seconds=round(age, 1))
