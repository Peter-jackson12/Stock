"""Read-only admission CLI for one approved FID A-B-A Mock run.

This command does not fetch Git refs, instantiate OCX, log in, subscribe, kill
processes, delete locks, or open raw databases.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.kiwoom.fid_read_ab_admission import (
    AdmissionInputs,
    RUN_BLOCKED,
    RUN_READY,
    RUN_UNCERTAIN,
    collect_and_evaluate,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--official-market-date", required=True, help="YYYY-MM-DD confirmed externally")
    parser.add_argument("--official-market-source-note", required=True)
    parser.add_argument(
        "--execution-approved",
        action="store_true",
        help="set only after the user explicitly approves this one Mock A-B-A run",
    )
    args = parser.parse_args(argv)
    inputs = AdmissionInputs(
        repo_root=str(args.repo_root.resolve()),
        expected_revision=args.expected_revision,
        official_market_date=args.official_market_date,
        official_market_source_note=args.official_market_source_note,
        execution_approved=args.execution_approved,
    )
    try:
        report = collect_and_evaluate(inputs)
    except Exception as exc:
        print(json.dumps({
            "schema": "fid_read_ab_admission_v1",
            "status": "UNAVAILABLE",
            "error": f"{type(exc).__name__}: {exc}",
            "non_actions": [
                "no OCX instantiation/login/SetRealReg",
                "no process kill/restart/relogin/window close",
                "no raw database open/scan/count/hash",
            ],
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] == RUN_READY:
        return 0
    if report["status"] == RUN_UNCERTAIN:
        return 3
    if report["status"] == RUN_BLOCKED:
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
