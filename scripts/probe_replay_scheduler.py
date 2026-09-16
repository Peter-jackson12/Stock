"""Explicit isolated Windows scheduler probes; never use production input or OCX."""
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


def prepare_replay(root):
    from collector.raw_v2 import RawV2Writer
    from engine.tick_ordering import OrderedTick
    from control_tower.service import plan_replay

    # Preserve the real action's code, changing only its import search environment.
    bootstrap = ("import site, sys\n"
                 f"site.addsitedir({str(ROOT / '.venv/Lib/site-packages')!r})\n"
                 f"sys.path.insert(0, {str(ROOT)!r})\n")
    (root / "scripts/scheduled_replay.py").write_text(
        bootstrap + (ROOT / "scripts/scheduled_replay.py").read_text(encoding="utf-8"), encoding="utf-8")
    path = root / "sampledata/raw_ticks_v2/synthetic.db"
    with RawV2Writer(path, source="scheduler_probe", session_id="synthetic",
                     market_date="2026-09-16", feed_scope="synthetic_only") as writer:
        quote = OrderedTick("scheduler_probe", "synthetic", 1, 0, "005930", "unknown", "quote",
                            bid=10000, ask=10001, bid_size=10, ask_size=3,
                            market_second=32399, bid_sizes=(10, 10, 10), ask_sizes=(3, 3, 3))
        writer.append(quote, received_at_utc="2026-09-16T00:00:00+00:00",
                      raw_fields={"synthetic": True})
        writer.finish(close_ns=20)
    return plan_replay(root, db=str(path), code="005930", venue="unknown", quantity=1,
                       cash="100000", fee_rate="0.001", buy_latency_sec="1", sell_latency_sec="1",
                       cancel_latency_sec="1", max_quote_age_sec="2", cooldown_sec="10", exit_rule="fixed")


def main():
    if sys.argv[1:] not in (["--execute"], ["--execute", "--scheduled-replay"]):
        raise SystemExit("--execute [--scheduled-replay] required: registers and deletes one isolated diagnostic task")
    scheduled = "--scheduled-replay" in sys.argv
    if scheduled:
        from control_tower.offline_worker import require_offline
        require_offline(ROOT)
    root = ROOT / "operations_state/scheduler_probes" / uuid4().hex
    (root / ".venv/Scripts").mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copyfile(ROOT / ".venv/Scripts/pythonw.exe", root / ".venv/Scripts/pythonw.exe")
    shutil.copyfile(ROOT / ".venv/pyvenv.cfg", root / ".venv/pyvenv.cfg")
    (root / "scripts/scheduled_replay.py").write_text(
        "from pathlib import Path\nimport sys\nPath(__file__).resolve().parents[1].joinpath('marker.txt').write_text(sys.argv[1])\n", encoding="utf-8")
    job = prepare_replay(root) if scheduled else JobStore(root).submit("replay_raw_v2", {"diagnostic_only": True})
    name = "StockReplay-" + job
    registered = False
    result = dict(checked_at_utc=datetime.now(timezone.utc).isoformat(),
                  isolated_root=str(root), job=job, registered=False, marker_verified=False,
                  task_removed=False, real_research=scheduled, synthetic_input=scheduled, ocx_used=False,
                  scheduled_trigger=scheduled, replay_verified=False, duplicate_blocked=False)
    try:
        # Weekend boundary is not needed: use the next 19:00 KST (10:00 UTC).
        now = datetime.now(timezone.utc)
        due = now + timedelta(seconds=75) if scheduled else now.replace(hour=10, minute=0, second=0, microsecond=0)
        if not scheduled and due < now + timedelta(minutes=2):
            due += timedelta(days=1)
        result["due_utc"] = due.isoformat()
        schedule_replay(root, job, due)
        result["registered"] = registered = True
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if scheduled:
            print(f"Waiting for Windows time trigger: {due.isoformat()}", flush=True)
            deadline = time.monotonic() + 110
            while time.monotonic() < deadline:
                record = JobStore(root).recent()[0]
                if record["status"] in ("succeeded", "failed"):
                    break
                time.sleep(.5)
            result["job_record"] = record
            if record["status"] != "succeeded":
                raise RuntimeError(f"scheduled replay did not succeed: {record['status']}")
            saved = json.loads(Path(record["result"]["result_path"]).read_text(encoding="utf-8"))
            result["replay_verified"] = (saved["input_complete"] is True
                and saved["processed_event_counts"] == {"quote": 1}
                and record["result"]["integrity"] == "verified"
                and record["result"]["raw_records"] == 1)
            # A fresh interpreter represents another scheduler invocation after manager restart.
            subprocess.run([str(ROOT / ".venv/Scripts/python.exe"),
                            str(root / "scripts/scheduled_replay.py"), job], check=True, timeout=20,
                           capture_output=True, creationflags=flags)
            result["duplicate_blocked"] = (JobStore(root).recent()[0] == record
                and len(list((root / "research_runs").glob("*/result.json"))) == 1)
        else:
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
            try:
                cleanup = subprocess.run(["schtasks.exe", "/Delete", "/TN", name, "/F"],
                    timeout=10, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                result["task_removed"] = cleanup.returncode == 0
            except Exception as exc:
                result["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        (root / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        print(root / "result.json")
    verified = result["replay_verified"] and result["duplicate_blocked"] if scheduled else result["marker_verified"]
    return 0 if verified and result["task_removed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
