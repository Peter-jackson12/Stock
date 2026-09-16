from dataclasses import replace
import sqlite3

import pytest

from control_tower.capture_history import CaptureHistory
from control_tower.history_limits import HistoryLimitError
from control_tower.lifecycle import CaptureReport, ProcessIdentity, encode_report
from control_tower.report_journal import ReportJournal, ReplayRequest


def report():
    return CaptureReport(ProcessIdentity("s", 1, "2026-09-16T00:00:00Z", "C:/fixture/python.exe",
        "fixture", 32, "fixture", "fixture", "C:/fixture/raw.db"), 1, "starting")


def test_peer_count_budget_preserves_report_and_allows_identical_retry(tmp_path):
    first = report()
    journal = ReportJournal(tmp_path, first.identity, max_reports=1)
    assert journal.append(first)
    assert not journal.append(first)
    with pytest.raises(HistoryLimitError):
        journal.append(replace(first, revision=2))
    reopened = ReportJournal(tmp_path, first.identity, max_reports=1)
    assert reopened.page(ReplayRequest(first.identity, 0)).reports == (first,)
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT used_bytes FROM quota").fetchone()[0] == len(encode_report(first).encode())


def test_peer_byte_budget_stops_publication_and_cannot_be_silently_raised(tmp_path):
    first = report()
    limit = len(encode_report(first).encode())
    journal = ReportJournal(tmp_path, first.identity, max_bytes=limit)
    journal.append(first)
    class NoSend:
        def send_report(self, value):
            pytest.fail("report exceeding durable budget was sent")
    with pytest.raises(HistoryLimitError):
        journal.publish(replace(first, revision=2), NoSend())
    with pytest.raises(ValueError, match="persisted policy"):
        ReportJournal(tmp_path, first.identity, max_bytes=limit * 2)
    assert journal.page(ReplayRequest(first.identity, 0)).reports == (first,)


def legacy(tmp_path):
    first = report()
    journal = ReportJournal(tmp_path, first.identity)
    for i in (1, 2):
        journal.append(replace(first, revision=i))
    with sqlite3.connect(journal.path) as conn:
        conn.execute("DROP TABLE quota")
        conn.execute("PRAGMA user_version=1")
    return journal.path


def test_bounded_legacy_migration_preserves_payload(tmp_path):
    path = legacy(tmp_path)
    with sqlite3.connect(path) as conn:
        before = conn.execute("SELECT * FROM reports ORDER BY revision").fetchall()
    journal = ReportJournal(tmp_path, report().identity, max_reports=3)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT * FROM reports ORDER BY revision").fetchall() == before
        assert conn.execute("SELECT used_bytes FROM quota").fetchone()[0] == sum(len(r[1].encode()) for r in before)
    assert journal.append(replace(report(), revision=3))
    with pytest.raises(HistoryLimitError):
        journal.append(replace(report(), revision=4))


@pytest.mark.parametrize("limits", [dict(max_reports=1), dict(max_bytes=1)])
def test_over_budget_migration_rolls_back(tmp_path, limits):
    path = legacy(tmp_path)
    with pytest.raises(HistoryLimitError):
        ReportJournal(tmp_path, report().identity, **limits)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 2
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='quota'").fetchall()


def setup_manager(tmp_path):
    history = CaptureHistory(tmp_path)
    manager = history.register(report().identity, heartbeat_timeout_ns=100)
    manager.receive(report(), now_ns=0)
    manager.receive(replace(report(), revision=2), now_ns=1)
    return history, manager


@pytest.mark.parametrize("limits", [dict(replay_max_events=1), dict(replay_max_bytes=1)])
def test_replay_budget_failure_does_not_take_ownership(tmp_path, limits):
    history, manager = setup_manager(tmp_path)
    with sqlite3.connect(history.path) as conn:
        before = conn.execute("SELECT generation,event_count FROM sessions").fetchone()
    with pytest.raises(HistoryLimitError):
        CaptureHistory(tmp_path, **limits).recover(report().identity)
    assert manager.view(now_ns=2)["state"] == "starting"
    with sqlite3.connect(history.path) as conn:
        assert conn.execute("SELECT generation,event_count FROM sessions").fetchone() == before


def test_replay_time_budget_rolls_back_even_after_applying_prefix(tmp_path, monkeypatch):
    import control_tower.capture_history as module
    history, manager = setup_manager(tmp_path)
    calls = [0]
    def clock():
        calls[0] += 1
        return 0 if calls[0] <= 2 else 3
    monkeypatch.setattr(module.time, "monotonic", clock)
    with pytest.raises(HistoryLimitError):
        history.recover(report().identity)
    assert manager.view(now_ns=2)["state"] == "starting"
    with sqlite3.connect(history.path) as conn:
        assert conn.execute("SELECT generation,event_count FROM sessions").fetchone() == (1, 2)


def test_replay_row_count_is_bounded_even_with_false_metadata(tmp_path):
    history, manager = setup_manager(tmp_path)
    with sqlite3.connect(history.path) as conn:
        conn.execute("UPDATE sessions SET event_count=1")
    with pytest.raises(HistoryLimitError):
        CaptureHistory(tmp_path, replay_max_events=1).recover(report().identity)
    with sqlite3.connect(history.path) as conn:
        assert conn.execute("SELECT generation,event_count FROM sessions").fetchone() == (1, 1)


@pytest.mark.parametrize("limits", [dict(replay_max_events=True), dict(replay_max_bytes=0),
    dict(replay_timeout=float("nan")), dict(replay_timeout=31)])
def test_invalid_replay_limits_rejected(tmp_path, limits):
    with pytest.raises(ValueError):
        CaptureHistory(tmp_path, **limits)
