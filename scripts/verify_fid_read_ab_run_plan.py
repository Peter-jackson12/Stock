"""Verify a short-lived FID A-B-A run plan and reveal the manual command.

Verification reruns read-only admission checks. It never launches the collector.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.kiwoom.fid_read_ab_admission import KST
from collector.kiwoom.fid_read_ab_run_plan import (
    FidReadRunPlanError,
    read_run_plan,
    verify_plan_for_manual_command,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True,
                        help="exact SHA freshly supplied by the control tower")
    parser.add_argument("--execution-approved", action="store_true",
                        help="fresh explicit approval for this one manual run")
    args = parser.parse_args(argv)
    try:
        plan = read_run_plan(args.plan)
        result = verify_plan_for_manual_command(
            plan,
            trusted_expected_revision=args.expected_revision,
            execution_approved_now=args.execution_approved,
            now_kst=datetime.now(KST),
        )
    except (OSError, ValueError, TypeError, FidReadRunPlanError) as exc:
        print(json.dumps({
            "schema": "fid_read_ab_run_plan_verification_v1",
            "status": "UNAVAILABLE",
            "error": f"{type(exc).__name__}: {exc}",
            "manual_command": None,
            "automatic_execution": False,
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "MANUAL_COMMAND_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
