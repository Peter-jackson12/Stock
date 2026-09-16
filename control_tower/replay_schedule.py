"""One-shot Windows tasks for explicit offline plans, with a durable dispatch latch."""
from datetime import datetime, timedelta, timezone, time
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import xml.etree.ElementTree as ET

from control_tower.jobs import JobStore, utc_now
from control_tower.offline_worker import KST, run_replay_job

NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def validate_id(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("valid job id required")


def task_xml(root, job_id, due, expires, user):
    validate_id(job_id)
    root = Path(root).resolve()
    task = ET.Element("Task", xmlns=NS, version="1.2")
    trigger = ET.SubElement(ET.SubElement(task, "Triggers"), "TimeTrigger")
    ET.SubElement(trigger, "StartBoundary").text = due.isoformat()
    ET.SubElement(trigger, "EndBoundary").text = expires.isoformat()
    ET.SubElement(trigger, "Enabled").text = "true"
    principal = ET.SubElement(ET.SubElement(task, "Principals"), "Principal", id="Owner")
    for key, value in (("UserId", user), ("LogonType", "InteractiveToken"), ("RunLevel", "LeastPrivilege")):
        ET.SubElement(principal, key).text = value
    settings = ET.SubElement(task, "Settings")
    for key, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                       ("StopIfGoingOnBatteries", "false"), ("StartWhenAvailable", "false"),
                       ("Enabled", "true"), ("Hidden", "true"), ("WakeToRun", "false"), ("ExecutionTimeLimit", "PT1H")):
        ET.SubElement(settings, key).text = value
    action = ET.SubElement(ET.SubElement(task, "Actions", Context="Owner"), "Exec")
    ET.SubElement(action, "Command").text = str(root / ".venv/Scripts/pythonw.exe")
    ET.SubElement(action, "Arguments").text = subprocess.list2cmdline([str(root / "scripts/scheduled_replay.py"), job_id])
    ET.SubElement(action, "WorkingDirectory").text = str(root)
    return ET.tostring(task, encoding="utf-16", xml_declaration=True)


def schedule_replay(root, job_id, due, *, now=None, run=subprocess.run):
    validate_id(job_id)
    root = Path(root).resolve()
    now = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    if due.tzinfo is None or not now + timedelta(minutes=1) <= due <= now + timedelta(days=7):
        raise ValueError("explicit timezone and due time between one minute and seven days ahead required")
    local = due.astimezone(KST)
    if local.weekday() < 5 and time(8) <= local.time().replace(tzinfo=None) < time(16, 30):
        raise ValueError("capture-hours replay schedule rejected")
    for path in (root / ".venv/Scripts/pythonw.exe", root / "scripts/scheduled_replay.py"):
        if not path.is_file():
            raise ValueError("configured replay scheduler environment unavailable")
    due = due.astimezone(timezone.utc)
    expires = due + timedelta(minutes=5)
    store = JobStore(root)
    with store._write() as conn:
        row = conn.execute("SELECT kind,status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or tuple(row) != ("replay_raw_v2", "planned"):
            raise ValueError("unexecuted replay plan required")
        conn.execute("INSERT INTO replay_schedules VALUES (?,?,?,'pending','unknown',NULL)",
                     (job_id, due.isoformat(), expires.isoformat()))
    # Save intent first. Registration uncertainty never causes an automatic retry.
    try:
        user = os.environ["USERDOMAIN"] + "\\" + os.environ["USERNAME"]
        xml = task_xml(root, job_id, due, expires, user)
        path = root / "operations_state" / f"schedule_{job_id}.xml"
        with path.open("xb") as stream:
            stream.write(xml)
            stream.flush()
            os.fsync(stream.fileno())
        result = run(["schtasks.exe", "/Create", "/TN", f"StockReplay-{job_id}", "/XML", str(path)],
                     capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError(f"Windows task registration returned {result.returncode}; inspect the scheduled task before retrying")
        registration, error = "registered", None
    except Exception as exc:
        registration, error = "unknown", f"{type(exc).__name__}: {exc}"
    with store._write() as conn:
        conn.execute("UPDATE replay_schedules SET registration=?,error=? WHERE job_id=?", (registration, error, job_id))
    if error:
        raise RuntimeError(error)
    return f"StockReplay-{job_id}"


def schedules(root):
    path = JobStore(root).path.resolve()
    if not path.exists():
        return []
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='replay_schedules'").fetchone():
            return []
        return [dict(row) for row in conn.execute("SELECT * FROM replay_schedules ORDER BY due DESC LIMIT 100")]


def dispatch_schedule(root, job_id, *, now=None):
    validate_id(job_id)
    now = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    store = JobStore(root)
    with store._write() as conn:
        row = conn.execute("SELECT * FROM replay_schedules WHERE job_id=?", (job_id,)).fetchone()
        if row is None or row["state"] != "pending":
            return False
        if now < datetime.fromisoformat(row["due"]):
            return False
        if now > datetime.fromisoformat(row["expires"]):
            conn.execute("UPDATE replay_schedules SET state='expired' WHERE job_id=?", (job_id,))
            return False
        changed = conn.execute("UPDATE jobs SET status='queued',updated_at=? WHERE id=? AND kind='replay_raw_v2' AND status='planned'", (utc_now(), job_id)).rowcount
        conn.execute("UPDATE replay_schedules SET state=? WHERE job_id=?", ("dispatched" if changed else "cancelled", job_id))
        return bool(changed)


def run_scheduled(root, job_id):
    if not dispatch_schedule(root, job_id):
        return
    try:
        run_replay_job(root, job_id)
    except Exception as exc:
        # Dispatch already consumed: preserve uncertain queued/running state and
        # diagnostic evidence. Another scheduler invocation must not repeat it.
        with JobStore(root)._write() as conn:
            conn.execute("UPDATE replay_schedules SET error=? WHERE job_id=?", (str(exc)[:2048], job_id))
        raise


def expire_missed(root, *, now=None):
    now = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    with JobStore(root)._write() as conn:
        return conn.execute("UPDATE replay_schedules SET state='expired' WHERE job_id IN (SELECT job_id FROM replay_schedules WHERE state='pending' AND julianday(expires) < julianday(?) LIMIT 100)",
                            (now.isoformat(),)).rowcount
