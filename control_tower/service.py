"""Allowlisted local operations; no shell strings, collector stop, or heavy worker."""
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from collector.raw_v2 import read_raw_v2
from control_tower.jobs import JobStore
from scripts.inspect_tick_research import inspect
from scripts.run_tick_research import parser

ROOT = Path(__file__).resolve().parents[1]


def confined_file(root, value, folder, *, suffix=None, name=None):
    root = Path(root).resolve()
    path = Path(value)
    path = (path if path.is_absolute() else root / path).resolve(strict=True)
    allowed = root / folder
    # A symlink/junction at the allowed directory itself must not widen access.
    if allowed.resolve() != allowed or not path.is_relative_to(allowed) or not path.is_file():
        raise ValueError(f"file must be within {folder}")
    if (suffix and path.suffix != suffix) or (name and path.name != name):
        raise ValueError("unexpected input filename")
    return path


def queue_inspection(root, path):
    file = confined_file(root, path, "research_runs", name="result.json")
    return JobStore(root).submit("inspect_result", {"path": str(file)})


def plan_replay(root, *, db, code, venue, quantity, cash, fee_rate, buy_latency_sec,
                sell_latency_sec, cancel_latency_sec, max_quote_age_sec, cooldown_sec, exit_rule):
    if not str(code).strip() or not str(venue).strip():
        raise ValueError("code and venue required")
    file = confined_file(root, db, "sampledata/raw_ticks_v2", suffix=".db")
    values = dict(code=code, venue=venue, quantity=quantity, cash=cash, fee_rate=fee_rate,
                  buy_latency_sec=buy_latency_sec, sell_latency_sec=sell_latency_sec,
                  cancel_latency_sec=cancel_latency_sec, max_quote_age_sec=max_quote_age_sec,
                  cooldown_sec=cooldown_sec, exit_rule=exit_rule)
    argv = ["--db", str(file), "--output-root", str(Path(root).resolve() / "research_runs")]
    for key, value in values.items():
        argv.extend(["--" + key.replace("_", "-"), str(value)])
    try:
        args = parser().parse_args(argv)
    except SystemExit as exc:
        raise ValueError("invalid replay settings") from exc
    if args.fee_rate >= 1:
        raise ValueError("fee_rate must be less than 1")
    with read_raw_v2(file) as (manifest, _):
        # Header only: a saved plan does not certify checksum, quality or freshness.
        header = dict(manifest)
    return JobStore(root).submit("replay_raw_v2", dict(argv=argv, header_at_plan=header,
                                      validation="header_only_not_execution_approval"))


def run_one_inspection(root):
    store = JobStore(root)
    owner = f"{os.getpid()}:{uuid4().hex}"
    job = store.claim_inspection(owner)
    if job is None:
        return None
    try:
        if set(job["payload"]) != {"path"}:
            raise ValueError("unexpected inspection payload")
        path = confined_file(root, job["payload"]["path"], "research_runs", name="result.json")
        result = inspect(path)
        # Worker success means inspection succeeded; research failed/running is retained inside result.
        store.complete(job["id"], owner, result=result)
    except Exception as exc:
        store.complete(job["id"], owner, error=f"{type(exc).__name__}: {exc}")
    return job["id"]


def start_inspection_worker():
    """Separate local process survives UI disconnect; processes at most 10 tiny jobs."""
    kwargs = dict(cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True, shell=False)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen([sys.executable, str(ROOT / "scripts/control_worker.py")], **kwargs)
    return process.pid
