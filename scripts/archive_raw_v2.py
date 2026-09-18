"""Explicit small-data archive/restore prototype; no automatic source removal."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collector.raw_archive import archive_raw, restore_raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    archive = commands.add_parser("archive")
    archive.add_argument("source", type=Path)
    archive.add_argument("destination", type=Path)
    archive.add_argument("--session-id", required=True)
    archive.add_argument("--closure-note", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("bundle", type=Path)
    restore.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "archive":
        archive_raw(args.source, args.destination, root=ROOT,
                    session_id=args.session_id, closure_note=args.closure_note)
        print(args.destination / "archive.json")
    else:
        print(restore_raw(args.bundle, args.destination))


if __name__ == "__main__":
    main()
