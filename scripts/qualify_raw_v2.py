"""CLI for strategy-free closed raw-v2 qualification."""
import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collector.raw_v2_qualification import qualify_raw_v2


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--expected-session-id", required=True)
    parser.add_argument("--closure-evidence", required=True,
                        help="bounded reference/note for separately verified writer and process closure")
    args = parser.parse_args(argv)
    result = qualify_raw_v2(args.db, output_root=args.output_root,
                            expected_session_id=args.expected_session_id,
                            closure_evidence=args.closure_evidence)
    report = json.loads(result.read_text(encoding="utf-8"))
    print(result)
    if not report["stream_integrity_verified"]:
        return 3
    return 0 if report["research_eligible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
