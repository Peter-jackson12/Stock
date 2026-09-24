"""Qualify a bounded raw-v2 morning prefix without qualifying the tail."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collector.raw_v2_prefix_qualification import qualify_raw_v2_prefix


def market_second(text):
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("market second must be an integer") from exc
    if not 0 < value < 86_400:
        raise argparse.ArgumentTypeError("market second must be in 1..86399")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--expected-session-id", required=True)
    parser.add_argument("--closure-evidence", required=True)
    parser.add_argument(
        "--end-market-second",
        type=market_second,
        required=True,
        help="exclusive KST boundary; 36000 means 10:00:00 KST",
    )
    args = parser.parse_args(argv)
    result = qualify_raw_v2_prefix(
        args.db,
        output_root=args.output_root,
        expected_session_id=args.expected_session_id,
        closure_evidence=args.closure_evidence,
        end_market_second=args.end_market_second,
    )
    report = json.loads(result.read_text(encoding="utf-8"))
    print(result)
    if not report["prefix_structure_verified"]:
        return 3
    return 0 if report["prefix_research_eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
