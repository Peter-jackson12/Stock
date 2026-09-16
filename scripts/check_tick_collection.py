"""실행 중 틱 DB의 적재 진행을 짧은 읽기 전용 조회로 관찰한다.
전체 COUNT/무결성 검사/체크포인트를 실행하지 않는다. 자동 경보나 주문 허가 게이트가 아니다.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
TABLES = {"trades": ("raw_trades", "t_time"), "quotes": ("raw_quotes", "q_time")}


def read_snapshot(path: Path) -> dict:
    path = Path(path).resolve()
    stat = path.stat()  # 누락 파일을 SQLite가 생성하지 않게 먼저 검사한다.
    result = {"sampled_at": datetime.now(KST).isoformat(),
              "file_identity": [stat.st_dev, stat.st_ino], "streams": {}}
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
    try:
        conn.execute("PRAGMA query_only=ON")
        for label, (table, column) in TABLES.items():
            # 두 쿼리의 관측 순간은 약간 다르며, 수집 전체의 원자적 스냅샷은 아니다.
            row = conn.execute(
                f"SELECT rowid, {column}, code FROM {table} ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            result["streams"][label] = {
                "last_rowid": row[0] if row else 0,
                "last_recorded_time": str(row[1]) if row else None,
                "last_code": row[2] if row else None,
            }
    finally:
        conn.close()  # 다음 관측까지 WAL 읽기 트랜잭션을 붙잡지 않는다.
    return result


def compare(before: dict, after: dict) -> dict:
    replaced = before["file_identity"] != after["file_identity"]
    streams = {}
    for label in TABLES:
        previous = before["streams"][label]["last_rowid"]
        current = after["streams"][label]
        delta = current["last_rowid"] - previous
        state = "reset_or_replaced" if replaced or delta < 0 else "advanced" if delta > 0 else "unchanged"
        streams[label] = {**current, "rowid_advance": delta if state != "reset_or_replaced" else None,
                          "state": state}
    return {
        "streams": streams,
        "observed_progress": any(s["state"] == "advanced" for s in streams.values()),
        "reset_or_replaced": any(s["state"] == "reset_or_replaced" for s in streams.values()),
    }


def inspect(path: Path, interval: float) -> dict:
    if not 0 <= interval <= 60:
        raise ValueError("interval must be between 0 and 60 seconds")
    started = time.monotonic()
    before = read_snapshot(path)
    time.sleep(interval)
    after = read_snapshot(path)
    return {"database": str(Path(path).resolve()), "before": before, "after": after,
            "elapsed_seconds": round(time.monotonic() - started, 3), **compare(before, after)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--date", help="YYYYMMDD (생략하면 한국 시간 오늘)")
    source.add_argument("--db", type=Path, help="직접 지정할 raw DB")
    parser.add_argument("--interval", type=float, default=3, help="두 관측 사이 대기 초, 0~60 (기본 3)")
    parser.add_argument("--json", action="store_true", help="JSON 출력")
    args = parser.parse_args(argv)
    date = args.date or datetime.now(KST).strftime("%Y%m%d")
    try:
        if datetime.strptime(date, "%Y%m%d").strftime("%Y%m%d") != date:
            raise ValueError("date must be YYYYMMDD")
        path = args.db or ROOT / "sampledata" / "raw_ticks" / f"{date}_raw.db"
        report = inspect(path, args.interval)
    except (OSError, sqlite3.Error, ValueError) as exc:
        message = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(message, ensure_ascii=False) if args.json else f"확인 불가: {message['error']}")
        return 2
    report["note"] = ("rowid 증가는 정확한 행 수가 아니며, 적재 진행은 무누락/저지연 보장이 아닙니다. "
                      "기록 시각은 현재 수집기의 PC 수신 시각입니다. 정체만으로 장애를 판정하지 않습니다.")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"DB: {report['database']}")
        print(f"관측 간격: {report['elapsed_seconds']}초")
        for label, stream in report["streams"].items():
            print(f"{label}: {stream['state']} | rowid 증가={stream['rowid_advance']} "
                  f"| 마지막 기록={stream['last_recorded_time']} | 종목={stream['last_code']}")
        print(report["note"])
    return 0  # 관찰 성공이다. 수집 정상 판정/실매매 허가가 아니다.


if __name__ == "__main__":
    raise SystemExit(main())
