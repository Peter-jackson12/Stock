"""기존 raw SQLite의 첫/마지막 행을 명시적 실행 시 읽기 전용으로 확인한다."""
from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3

DEFAULT_DATABASE = Path(__file__).resolve().parent / "sampledata" / "raw_ticks" / "20260914_raw.db"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args(argv)
    # mode=ro prevents a stale/missing path from silently creating a database.
    uri = args.database.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cursor = conn.cursor()
        for table in ("raw_trades", "raw_quotes"):
            print("===", table, "===")
            print(" 최초 행:", cursor.execute(f"SELECT * FROM {table} ORDER BY rowid ASC LIMIT 1").fetchone())
            print(" 최근 행:", cursor.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone())
            print(" 대략 행 수(max rowid):", cursor.execute(f"SELECT MAX(rowid) FROM {table}").fetchone())
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
