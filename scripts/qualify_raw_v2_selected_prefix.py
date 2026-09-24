"""Create an opt-in selected-instrument quality overlay over a strict prefix report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.raw_v2_selected_prefix_qualification import qualify_selected_prefix


def instrument(text):
    code, sep, venue = text.partition("=")
    if not sep or not code.strip() or not venue.strip():
        raise argparse.ArgumentTypeError("instrument must be CODE=VENUE")
    return code.strip(), venue.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--strict-prefix-report", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--instrument", action="append", type=instrument, required=True)
    args = parser.parse_args(argv)

    instruments = {}
    for code, venue in args.instrument:
        if code in instruments and instruments[code] != venue:
            parser.error("one venue per code is required")
        instruments[code] = venue

    try:
        result = qualify_selected_prefix(
            args.db,
            args.strict_prefix_report,
            output_root=args.output_root,
            instruments=instruments,
        )
        report = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        print(f"Selected prefix overlay failed before report completion: {exc}", file=sys.stderr)
        return 3

    print(result)
    if not report["selected_prefix_structure_verified"]:
        return 3
    return 0 if report["selected_smoke_quality_eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
