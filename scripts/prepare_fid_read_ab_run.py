"""Prepare a short-lived FID A-B-A manual run plan after fresh admission.

This command never launches the collector.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.kiwoom.fid_read_ab_admission import AdmissionInputs, KST, RUN_READY, collect_and_evaluate
from collector.kiwoom.fid_read_ab_run_plan import (
    FidReadRunPlanError,
    build_run_plan,
    write_new_plan,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--official-market-date", required=True)
    parser.add_argument("--official-market-source-note", required=True)
    parser.add_argument("--execution-approved", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    inputs = AdmissionInputs(
        repo_root=str(args.repo_root.resolve()),
        expected_revision=args.expected_revision,
        official_market_date=args.official_market_date,
        official_market_source_note=args.official_market_source_note,
        execution_approved=args.execution_approved,
    )
    now = datetime.now(KST)
    try:
        admission = collect_and_evaluate(inputs, now_kst=now)
        if admission.get("status") != RUN_READY:
            print(json.dumps({
                "schema": "fid_read_ab_run_plan_prepare_v1",
                "status": "PLAN_NOT_CREATED",
                "admission": admission,
                "automatic_execution": False,
            }, ensure_ascii=False, indent=2))
            return 3 if admission.get("status") == "RUN_UNCERTAIN" else 2
        plan = build_run_plan(admission, expected_revision=args.expected_revision, now_kst=now)
        path = write_new_plan(args.output, plan)
    except (OSError, ValueError, TypeError, FidReadRunPlanError) as exc:
        print(json.dumps({
            "schema": "fid_read_ab_run_plan_prepare_v1",
            "status": "UNAVAILABLE",
            "error": f"{type(exc).__name__}: {exc}",
            "automatic_execution": False,
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    print(json.dumps({
        "schema": "fid_read_ab_run_plan_prepare_v1",
        "status": "PLAN_CREATED",
        "path": str(path),
        "created_at_kst": plan["created_at_kst"],
        "expires_at_kst": plan["expires_at_kst"],
        "expected_revision": plan["expected_revision"],
        "admission_sha256": plan["admission_sha256"],
        "automatic_execution": False,
        "requires_fresh_verification": True,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
