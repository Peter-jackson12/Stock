"""
scripts/check_lob_db_types.py — 기존 LOB DB 파일의 컬럼 타입 감사

sampledata/temp/*_LOB.db 는 REAL 로 선언된 컬럼에도 numpy 스칼라가 그대로
바인딩되면 BLOB 으로 저장되는 버그가 있었다(2026-09-08 발견, collector/
build_lob_db.py 수정으로 재발 방지). 이 스크립트는 이미 만들어진 LOB DB
파일들을 스캔해서 같은 문제가 있는 파일/테이블/컬럼을 찾아낸다.

실행:
    uv run python scripts/check_lob_db_types.py
    uv run python scripts/check_lob_db_types.py --glob "sampledata/temp/*_LOB.db"
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from collector.build_lob_db import NUMERIC_COLUMNS, validate_numeric_column_types


def scan_db(db_path: Path) -> list[str]:
    """db_path 안의 모든 테이블을 검사해서 문제 메시지 목록을 반환한다."""
    problems: list[str] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        for table in tables:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")}
            if not NUMERIC_COLUMNS or not set(NUMERIC_COLUMNS).issubset(cols):
                continue  # LOB 스키마가 아닌 테이블은 건너뛴다
            try:
                validate_numeric_column_types(conn, table)
            except TypeError as e:
                problems.append(str(e))
    finally:
        conn.close()
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--glob",
        default="sampledata/temp/*_LOB.db",
        help="검사할 DB 파일 glob 패턴 (기본: sampledata/temp/*_LOB.db)",
    )
    args = parser.parse_args()

    db_paths = sorted(PROJECT_ROOT.glob(args.glob))
    if not db_paths:
        print(f"⚠️ 패턴에 매칭되는 DB 파일이 없습니다: {args.glob}")
        return 0

    total_bad = 0
    for db_path in db_paths:
        problems = scan_db(db_path)
        if problems:
            total_bad += 1
            print(f"❌ {db_path.name}")
            for p in problems:
                print(f"   - {p}")
        else:
            print(f"✅ {db_path.name}")

    print(f"\n총 {len(db_paths)}개 파일 중 {total_bad}개에서 BLOB/TEXT 오염 발견")
    return 1 if total_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
