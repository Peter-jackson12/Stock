import sqlite3

import pytest

from scripts import check_tick_collection as check


def create(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE raw_trades(t_time TEXT, code TEXT)")
    conn.execute("CREATE TABLE raw_quotes(q_time TEXT, code TEXT)")
    conn.commit()
    return conn


def test_missing_db_is_not_created(tmp_path, capsys):
    path = tmp_path / "absent.db"
    assert check.main(["--db", str(path), "--interval", "0"]) == 2
    assert not path.exists()
    assert "확인 불가" in capsys.readouterr().out


def test_reads_committed_wal_and_detects_growth(tmp_path, monkeypatch):
    path = tmp_path / "ticks.db"
    writer = create(path)
    try:
        writer.execute("INSERT INTO raw_trades VALUES ('090000', '005930')")
        writer.commit()
        def write_between_samples(_):
            writer.execute("INSERT INTO raw_trades VALUES ('090001', '005930')")
            writer.execute("INSERT INTO raw_quotes VALUES ('090001', '000660')")
            writer.commit()
        monkeypatch.setattr(check.time, "sleep", write_between_samples)
        report = check.inspect(path, 0)
        assert report["observed_progress"]
        assert report["streams"]["trades"]["rowid_advance"] == 1
        assert report["streams"]["quotes"]["last_recorded_time"] == "090001"
    finally:
        writer.close()


def test_empty_database_is_observation_not_an_outage(tmp_path):
    path = tmp_path / "ticks.db"
    writer = create(path)
    writer.close()
    report = check.inspect(path, 0)
    assert not report["observed_progress"]
    assert report["streams"]["trades"]["state"] == "unchanged"
    assert report["streams"]["trades"]["last_recorded_time"] is None


def test_reader_cannot_write_and_does_not_scan_whole_tables(tmp_path, monkeypatch):
    path = tmp_path / "ticks.db"
    writer = create(path)
    writer.close()
    original = sqlite3.connect
    queries = []
    def connect(database_uri, **kwargs):
        assert database_uri.endswith("?mode=ro")
        conn = original(database_uri, **kwargs)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO raw_trades VALUES ('090000', '005930')")
        conn.set_trace_callback(queries.append)
        return conn
    monkeypatch.setattr(check.sqlite3, "connect", connect)
    check.read_snapshot(path)
    assert not any("COUNT(" in q.upper() or "CHECKPOINT" in q.upper() for q in queries)
    assert all("LIMIT 1" in q for q in queries if q.startswith("SELECT"))


def test_rowid_gaps_are_not_reported_as_exact_counts(tmp_path):
    path = tmp_path / "ticks.db"
    writer = create(path)
    try:
        before = check.read_snapshot(path)
        writer.execute("INSERT INTO raw_trades(rowid,t_time,code) VALUES (100,'090001','005930')")
        writer.commit()
        report = check.compare(before, check.read_snapshot(path))
        assert report["streams"]["trades"]["rowid_advance"] == 100
        assert "count" not in report["streams"]["trades"]
    finally:
        writer.close()


def test_reset_is_not_negative_throughput(tmp_path):
    path = tmp_path / "ticks.db"
    writer = create(path)
    try:
        writer.execute("INSERT INTO raw_trades VALUES ('090000','005930')")
        writer.commit()
        before = check.read_snapshot(path)
        writer.execute("DELETE FROM raw_trades")
        writer.commit()
        report = check.compare(before, check.read_snapshot(path))
        assert report["reset_or_replaced"]
        assert report["streams"]["trades"]["rowid_advance"] is None
    finally:
        writer.close()


def test_wrong_schema_is_unavailable(tmp_path):
    path = tmp_path / "wrong.db"
    sqlite3.connect(path).close()
    assert check.main(["--db", str(path), "--interval", "0", "--json"]) == 2


def test_interval_is_bounded(tmp_path):
    with pytest.raises(ValueError, match="interval"):
        check.inspect(tmp_path / "unused.db", 61)
