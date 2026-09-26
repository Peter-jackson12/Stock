"""MWFD-04 호스트 호출 한도(약 180초) 예외 드라이버.

run_mwfd_04_full.CellRunner의 compute 단계를 코드 변경 없이 그대로 호출하되,
계측(tracemalloc)만 끈 상태로 실행한다. tracemalloc은 계산 결과에 영향을 주지 않으며
(005930 셀에서 feature digest 동일 확인), 활동량이 가장 큰 셀의 causal feature 단계가
tracemalloc 오버헤드 때문에 단일 호출 한도를 넘는 경우에만 사용한다.
사용 사실과 계측 차이는 run root의 host_limit_exceptions/에 create-only로 기록한다.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time
import tracemalloc

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research.fast_backtest.full_run import checkpoint_name
from research.fast_backtest.runtime_probe import sha256_file, write_json_create
from scripts.run_mwfd_04_full import CellRunner, Context, peak_rss_bytes


def main(argv=None) -> int:
    value = argparse.ArgumentParser()
    value.add_argument("--run-root", required=True)
    value.add_argument("--total-stock-root", required=True)
    value.add_argument("--scratch-dir", required=True)
    value.add_argument("--cell-index", type=int, required=True)
    value.add_argument("--steps", required=True, help="comma list of compute steps, e.g. events,gate or features")
    value.add_argument("--reason", required=True)
    args = value.parse_args(argv)
    if tracemalloc.is_tracing():
        raise RuntimeError("driver expects tracemalloc off")
    run_root = Path(args.run_root).absolute()
    ctx = Context(run_root, Path(args.total_stock_root).resolve(strict=True))
    cell = ctx.population.cells[args.cell_index - 1]
    runner = CellRunner(ctx, Path(args.scratch_dir), time.perf_counter() + 1e9)
    final, building, scratch = runner.paths(cell)
    if (final / "completion.json").exists() or (building / "prepare.json").exists():
        raise RuntimeError("cell already past compute")
    steps = args.steps.split(",")
    for step in steps:
        expected = runner.compute_next_step(cell, building, scratch)
        if expected != step:
            raise RuntimeError(f"next compute step is {expected}, not {step}")
        runner.compute_step(cell, building, scratch, step)
    following = runner.compute_next_step(cell, building, scratch)
    if following is not None:
        runner.persist_for_defer(cell, following)
    record_dir = run_root / "host_limit_exceptions"
    record_dir.mkdir(exist_ok=True)
    record = {
        "schema": "mwfd_04_host_limit_exception_v1",
        "cell": cell.to_dict(),
        "steps": runner.steps,
        "next_step": following,
        "reason": args.reason,
        "instrumentation": "tracemalloc disabled for these steps only; computation code unchanged "
                           "(run_mwfd_04_full.CellRunner.compute_step, code provenance verified by Context)",
        "driver_sha256": sha256_file(Path(__file__)),
        "process_peak_rss_bytes": peak_rss_bytes(),
        "at": datetime.now(timezone(timedelta(hours=9))).isoformat(),
    }
    name = f"{checkpoint_name(cell)}-{'-'.join(steps)}.json"
    write_json_create(record_dir / name, record)
    print(json.dumps({"cell": cell.cell_id, "steps": [(s["stage"], round(s["wall_seconds"], 1)) for s in runner.steps],
                      "next": following}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
