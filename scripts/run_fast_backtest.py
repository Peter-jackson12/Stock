"""Fast Backtest v1 plan 실행 CLI."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research.fast_backtest.pipeline import read_candidates, run_fast_backtest_pipeline
from research.fast_backtest.plan import FastBacktestPlan


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Run screening-only Fast Backtest v1")
    value.add_argument("--plan", required=True)
    value.add_argument("--metadata-csv", required=True)
    value.add_argument("--events-jsonl", required=True)
    value.add_argument("--candidates", required=True)
    return value


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    path = run_fast_backtest_pipeline(
        plan=FastBacktestPlan.read(args.plan),
        metadata_csv=args.metadata_csv,
        events_jsonl=args.events_jsonl,
        candidate_records=read_candidates(args.candidates),
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
