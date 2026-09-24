"""Run bounded FID A-B-A evidence analysis and fixed pre-registered assessment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.kiwoom.fid_read_ab_analysis import (
    FidReadAnalysisError,
    analyze_fid_read_ab_session,
)
from collector.kiwoom.fid_read_ab_assessment import assess_fid_read_ab


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--expected-revision")
    args = parser.parse_args(argv)
    try:
        analysis = analyze_fid_read_ab_session(
            args.session_dir,
            expected_revision=args.expected_revision,
        )
        assessment = assess_fid_read_ab(analysis)
    except (OSError, FidReadAnalysisError, ValueError, TypeError) as exc:
        print(json.dumps({
            "status": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "note": "raw database was not opened or scanned",
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps({
        "analysis": analysis,
        "assessment": assessment,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
