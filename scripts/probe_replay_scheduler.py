"""Explicit Windows scheduling smoke: isolated marker only, no research/OCX."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from control_tower.jobs import JobStore
from control_tower.replay_schedule import schedule_replay


def main():
    if sys.argv[1:] != ["--execute"]:
        raise SystemExit("--execute required: registers, manually triggers and deletes one isolated diagnostic task")
    root = ROOT / "operations_state/scheduler_probes" / uuid4().hex
    (root / ".venv/Scripts").mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copyfile(ROOT / ".venv/Scripts/pythonw.exe", root / ".venv/Scripts/pythonw.exe")
    shutil.copyfile(ROOT / ".venv/pyvenv.cfg", root / ".venv/pyvenv.cfg")
    (root / "scripts/scheduled_replay.py").write_text(
        "from pathlib import Path\nimport sys\nPath(__file__).resolve().parents[1].joinpath('marker.txt').write_text(sys.argv[1])\n", encoding="utf-8")
    job = JobStore(root).submit("replay_raw_v2", {"diagnostic_only": True})
    name = "StockReplay-" + job
    registered = False
    result = dict(checked_at_utc=datetime.now(timezone.utc).isoformat(),
                  isolated_root=str(root), job=job, registered=False, marker_verified=False,
                  task_removed=False, real_research=False, ocx_used=False)
    try:
        # Weekend boundary is not needed: use the next 19:00 KST (10:00 UTC).
        now = datetime.now(timezone.utc)
        due = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if due < now + timedelta(minutes=2):
            due += timedelta(days=1)
        schedule_replay(root, job, due)
        result["registered"] = registered = True
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(["schtasks.exe", "/Run", "/TN", name], check=True, timeout=10,
                       capture_output=True, creationflags=flags)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (root / "marker.txt").exists():
            time.sleep(.1)
        result["marker_verified"] = (root / "marker.txt").read_text() == job
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if registered:
            cleanup = subprocess.run(["schtasks.exe", "/Delete", "/TN", name, "/F"],
                timeout=10, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            result["task_removed"] = cleanup.returncode == 0
        (root / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        print(root / "result.json")
    return 0 if result["marker_verified"] and result["task_removed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
