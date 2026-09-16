"""Small fixtures only; no production databases."""
import json
import sqlite3

import pytest

from scripts.verify_tick_storage import main, verify


def database(tmp_path):
    path = tmp_path / "raw.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE raw_trades(t_time TEXT)")
        conn.execute("CREATE TABLE raw_quotes(q_time TEXT)")
        conn.executemany("INSERT INTO raw_trades VALUES (?)", [("090000",), ("090001",)])
        conn.execute("INSERT INTO raw_quotes VALUES ('090000')")
    return path


def test_exact_counts_and_integrity_preserve_source(tmp_path):
    path = database(tmp_path)
    before = path.read_bytes()
    report = verify(path, expected_trades=2, expected_quotes=1)
    assert report["status"] == "passed"
    assert report["quick_check"] == ["ok"] and report["files_unchanged"]
    assert report["capture_completeness"] == report["data_quality"] == "unverified"
    assert path.read_bytes() == before


def test_rowid_is_not_a_row_count(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM raw_trades WHERE rowid=1")
    report = verify(path, expected_trades=2, expected_quotes=1)
    assert report["status"] == "failed" and report["actual_counts"]["trades"] == 1


def test_missing_file_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        verify(path, expected_trades=0, expected_quotes=0)
    assert not path.exists()


def test_corrupt_db_is_not_reported_as_passed(tmp_path):
    path = tmp_path / "bad.db"
    path.write_bytes(b"not a database" * 100)
    report = verify(path, expected_trades=0, expected_quotes=0)
    assert report["status"] == "unavailable" and "error" in report


def test_timeout_is_not_a_success(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.executemany("INSERT INTO raw_trades VALUES (?)", [("090000",)] * 1000)
    ticks = iter([0] + [2] * 100)
    report = verify(path, expected_trades=1002, expected_quotes=1, max_seconds=1, clock=lambda: next(ticks))
    assert report["status"] == "unavailable" and "interrupted" in report["error"]


def test_file_change_during_check_is_uncertain(tmp_path, monkeypatch):
    path = database(tmp_path)
    snapshots = iter([[1], [2]])
    monkeypatch.setattr("scripts.verify_tick_storage.fingerprint", lambda path: next(snapshots))
    assert verify(path, expected_trades=2, expected_quotes=1)["status"] == "changed_during_check"


def test_cli_requires_off_hours_and_preserves_output(tmp_path):
    path = database(tmp_path)
    output = tmp_path / "result.json"
    args = ["--db", str(path), "--expected-trades", "2", "--expected-quotes", "1", "--output", str(output)]
    with pytest.raises(SystemExit):
        main(args)
    assert not output.exists()
    assert main(args + ["--off-hours"]) == 0
    saved = output.read_bytes()
    assert json.loads(saved)["status"] == "passed"
    with pytest.raises(SystemExit):
        main(args + ["--off-hours"])
    assert output.read_bytes() == saved
