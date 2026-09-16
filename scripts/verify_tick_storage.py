"""Off-hours, read-only raw-v1 storage check. Never certifies capture completeness.

Scans SQLite pages and exact row counts; do not run against an active collector.
Expected counts must come from the matching session log, not max(rowid).
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import time


def fingerprint(path):
    result = []
    for item in (path, Path(str(path) + "-wal")):
        try:
            stat = item.stat()
            result.append([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns])
        except FileNotFoundError:
            result.append(None)
    return result


def verify(path, *, expected_trades, expected_quotes, max_seconds=300, clock=time.monotonic):
    for value in (expected_trades, expected_quotes):
        if type(value) is not int or value < 0:
            raise ValueError("explicit nonnegative session counts required")
    if not math.isfinite(max_seconds) or not 0 < max_seconds <= 1800:
        raise ValueError("max_seconds must be finite and in (0, 1800]")
    path = Path(path).resolve(strict=True)
    before = fingerprint(path)
    started = clock()
    report = dict(database=str(path), checked_at_utc=datetime.now(timezone.utc).isoformat(),
                  expected_counts={"trades": expected_trades, "quotes": expected_quotes},
                  actual_counts={}, quick_check=None, status="unavailable",
                  capture_completeness="unverified", data_quality="unverified")
    conn = None
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA cache_size=-8192")
        conn.set_progress_handler(lambda: int(clock() - started > max_seconds), 1000)
        conn.execute("BEGIN")
        # One read snapshot across integrity and count checks. No checkpoints/indexes.
        report["quick_check"] = [row[0] for row in conn.execute("PRAGMA quick_check(10)")]
        for label, table in (("trades", "raw_trades"), ("quotes", "raw_quotes")):
            report["actual_counts"][label] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        conn.rollback()
        report["status"] = "passed" if (report["quick_check"] == ["ok"] and
            report["actual_counts"] == report["expected_counts"]) else "failed"
    except sqlite3.Error as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:
            conn.close()
    report["files_unchanged"] = fingerprint(path) == before
    if not report["files_unchanged"]:
        report["status"] = "changed_during_check"
    report["elapsed_seconds"] = round(clock() - started, 3)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--expected-trades", type=int, required=True)
    parser.add_argument("--expected-quotes", type=int, required=True)
    parser.add_argument("--off-hours", action="store_true", help="confirm the collector has stopped")
    parser.add_argument("--max-seconds", type=float, default=300)
    parser.add_argument("--output", type=Path, help="new diagnostic JSON file; existing files are refused")
    args = parser.parse_args(argv)
    if not args.off_hours:
        parser.error("--off-hours is required after checking collector shutdown")
    if args.output and args.output.exists():
        parser.error("output already exists")
    try:
        report = verify(args.db, expected_trades=args.expected_trades, expected_quotes=args.expected_quotes,
                        max_seconds=args.max_seconds)
    except (OSError, ValueError) as exc:
        report = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(text + "\n")
    print(text)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
