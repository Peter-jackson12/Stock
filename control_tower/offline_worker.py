"""Explicit, bounded, after-hours replay; no collector discovery or restart."""
from datetime import datetime, time, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from collector.kiwoom.collector_lease import CollectorLease
from collector.raw_v2 import read_raw_v2
from collector.research_input_policy import require_research_input
from control_tower.jobs import JobStore
from control_tower.managed_capture import ManagedCaptures, ACTIVE
from control_tower.service import confined_file
from control_tower.status import observe_collector
from scripts.run_tick_research import parser
from engine.tick_research_run import run_raw_v2
from control_tower.storage_guard import require_disk_space

MAX_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 100_000
KST = timezone(timedelta(hours=9))


def require_offline(root, now=None):
    require_disk_space(root)
    now = datetime.now(KST) if now is None else now.astimezone(KST)
    if now.weekday() < 5 and time(8) <= now.time().replace(tzinfo=None) < time(16, 30):
        raise ValueError("장외 워커는 평일 08:00~16:30에 실행하지 않습니다")
    capture = ManagedCaptures(root).get()
    if capture and capture["state"] in ACTIVE:
        raise ValueError("관리 수집 요청이 미해결 상태입니다")
    if observe_collector(root)["status"] in ("recent", "clock_ahead"):
        raise ValueError("최근 수집 로그 또는 시각 이상이 있어 실행하지 않습니다")


def run_replay_job(root, job_id, *, now=None):
    root = Path(root).resolve()
    require_offline(root, now)
    with CollectorLease(root):
        require_offline(root, now)
        store = JobStore(root)
        owner = f"{os.getpid()}:{uuid4().hex}"
        job = store.claim_replay(job_id, owner)
        if job is None:
            return None
        try:
            payload = job["payload"]
            if set(payload) != {"argv", "header_at_plan", "validation"}:
                raise ValueError("unexpected replay payload")
            try:
                args = parser().parse_args(payload["argv"])
            except SystemExit as exc:
                raise ValueError("invalid stored replay arguments") from exc
            path = confined_file(root, args.db, "sampledata/raw_ticks_v2", suffix=".db")
            output = root / "research_runs"
            if args.output_root != output or output.resolve() != output:
                raise ValueError("unexpected research output directory")
            if args.fee_rate >= 1 or not args.code.strip() or not args.venue.strip():
                raise ValueError("invalid simulation settings")
            if path.stat().st_size > MAX_BYTES:
                raise ValueError("UI replay input exceeds 32 MiB; use explicit offline CLI")
            # Fully consume before strategy execution, so quality rejection cannot
            # be mistaken for a completed integrity scan.
            with read_raw_v2(path) as (manifest, rows):
                require_research_input(manifest)
                if manifest != payload["header_at_plan"]:
                    raise ValueError("raw manifest changed since plan creation")
                count = 0
                for _ in rows:
                    count += 1
                    if count > MAX_RECORDS:
                        raise ValueError("UI replay exceeds 100,000 raw records")
            config = dict(source=manifest["source"], session_id=manifest["session_id"],
                code=args.code, venue=args.venue, cash=args.cash, fee_rate=args.fee_rate,
                buy_latency_ns=args.buy_latency_sec, sell_latency_ns=args.sell_latency_sec,
                cancel_latency_ns=args.cancel_latency_sec, max_quote_age_ns=args.max_quote_age_sec)
            result = run_raw_v2(path, output_root=output, simulator_config=config,
                quantity=args.quantity, exit_rule=args.exit_rule, cooldown_ns=args.cooldown_sec)
            store.complete(job_id, owner, result=dict(result_path=str(result),
                integrity="verified", raw_records=count, data_quality="see_research_result"))
        except Exception as exc:
            store.complete(job_id, owner, error=f"{type(exc).__name__}: {exc}")
        return job_id


def start_replay_worker(root, job_id):
    root = Path(root).resolve()
    require_offline(root)
    # Durable queue before spawning. A failed spawn stays queued for explicit retry.
    JobStore(root).queue_replay(job_id)
    return retry_replay_worker(root, job_id)


def retry_replay_worker(root, job_id):
    root = Path(root).resolve()
    require_offline(root)
    if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        raise ValueError("invalid job id")
    with (root / "operations_state" / f"replay_{job_id}.log").open("ab") as log:
        return subprocess.Popen([sys.executable, str(root / "scripts/replay_worker.py"), job_id],
            cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).pid
