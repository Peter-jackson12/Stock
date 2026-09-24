"""Acquire a sidecar-free frozen working snapshot of one raw-v2 residue set.

The source SQLite database is never opened. This command accepts only the
zero-byte-WAL + 32768-byte-SHM residue policy and writes a fresh evidence/working
snapshot under an existing local-NTFS output directory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector.raw_v2_snapshot import (
    RESIDUE_POLICY,
    acquire_frozen_snapshot,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--expected-session-id", required=True)
    parser.add_argument(
        "--residue-policy",
        choices=(RESIDUE_POLICY,),
        required=True,
    )
    args = parser.parse_args(argv)
    try:
        paths = acquire_frozen_snapshot(
            args.db,
            output_root=args.output_root,
            expected_session_id=args.expected_session_id,
            residue_policy=args.residue_policy,
        )
        report = json.loads(paths.result.read_text(encoding="utf-8"))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Snapshot acquisition rejected before report completion: {exc}", file=sys.stderr)
        return 3
    print(paths.result)
    return 0 if report["snapshot_ready_for_prefix_qualification"] else 2


if __name__ == "__main__":
    import sqlite3
    raise SystemExit(main())
