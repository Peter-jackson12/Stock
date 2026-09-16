"""Tiny control journals and fake peers only; no processes, OCX or production data."""
from contextlib import contextmanager
from dataclasses import replace
import sqlite3

import pytest

from control_tower.capture_history import CaptureHistory, StaleManagerError
from control_tower.lifecycle import CaptureReport, Finalization, ProcessIdentity, StopReceiver


def identity(**changes):
    return replace(ProcessIdentity("session-a", 123, "2026-09-16T00:00:00Z",
        "C:/fixture/python.exe", "test", 32, "fixture", "synthetic", "C:/fixture/a.db"), **changes)


def running(tmp_path):
    history = CaptureHistory(tmp_path)
    manager = history.register(identity(), heartbeat_timeout_ns=10)
    for revision, state in enumerate(("starting", "login_required", "subscribed", "receiving"), 1):
        manager.receive(CaptureReport(identity(), revision, state), now_ns=revision + 100)
    return history, manager


def acknowledge(manager, command, *, now_ns=105):
    report = replace(manager.report, revision=manager.report.revision + 1,
                     state="draining", input_stopped=True, stop_request_id=command.request_id)
    manager.receive(report, now_ns=now_ns)


def close(manager, *, now_ns=106):
    manager.receive(replace(manager.report, revision=manager.report.revision + 1,
        state="closed", writer_closed=True, finalization=Finalization(0, 1, "a" * 64)), now_ns=now_ns)


def stop(manager):
    return manager.request_stop("stop-a", identity(), now_ns=104, timeout_ns=20)


def test_recovery_requires_new_contiguous_evidence_and_new_clock(tmp_path):
    history, old = running(tmp_path)
    recovered = CaptureHistory(tmp_path).recover(identity())
    view = recovered.view(now_ns=0)
    assert view["state"] == "unknown" and view["age_ns"] is None
    assert view["last_reported_state"] == "receiving"
    assert not recovered.receive(old.report, now_ns=1)
    assert recovered.view(now_ns=1)["state"] == "unknown"
    with pytest.raises(ValueError, match="fresh"):
        recovered.request_stop("s", identity(), now_ns=1, timeout_ns=20)
    recovered.receive(replace(old.report, revision=5), now_ns=2)
    assert recovered.view(now_ns=2)["state"] == "receiving"
    assert recovered.view(now_ns=12)["state"] == "unknown"
    # A second restart replays both clock epochs without comparing their origins.
    assert history.recover(identity()).view(now_ns=0)["state"] == "unknown"


@pytest.mark.parametrize("delivered", [False, True])
def test_crash_around_dispatch_never_resends_stop(tmp_path, delivered):
    history, manager = running(tmp_path)
    command = stop(manager)
    receiver = StopReceiver(identity())
    if delivered:
        assert receiver.accept(command, manager.report)
    recovered = history.recover(identity())
    assert recovered.stop_command == command
    assert recovered.view(now_ns=0)["stop_status"] == "unknown"
    with pytest.raises(ValueError, match="audit-only"):
        recovered.request_stop(command.request_id, identity(), now_ns=0, timeout_ns=1000)
    recovered.receive(replace(manager.report, revision=5), now_ns=1)
    assert recovered.view(now_ns=1)["stop_status"] == "unknown"
    if delivered:
        acknowledge(recovered, command, now_ns=2)
        assert recovered.view(now_ns=2)["stop_status"] == "unknown"
        close(recovered, now_ns=3)
        assert recovered.view(now_ns=3)["stop_status"] == "completed"


def test_crash_after_ack_before_close_recovers_outcome(tmp_path):
    history, manager = running(tmp_path)
    acknowledge(manager, stop(manager))
    recovered = history.recover(identity())
    close(recovered, now_ns=0)
    assert recovered.view(now_ns=0)["stop_status"] == "completed"
    again = history.recover(identity())
    assert again.view(now_ns=0)["state"] == "closed"
    assert again.view(now_ns=999)["stop_status"] == "completed"
    assert again.view(now_ns=999)["data_quality"] == "unverified"


def test_recovered_error_is_not_success(tmp_path):
    history, manager = running(tmp_path)
    command = stop(manager)
    recovered = history.recover(identity())
    recovered.receive(replace(manager.report, revision=5, state="failed", write_failures=1,
                             stop_request_id=command.request_id), now_ns=0)
    assert recovered.view(now_ns=0)["stop_status"] == "failed"
    assert history.recover(identity()).view(now_ns=0)["stop_status"] == "failed"


@pytest.mark.parametrize("operation", ["view", "report", "stop", "loss"])
def test_recovery_fences_old_manager(tmp_path, operation):
    history, old = running(tmp_path)
    history.recover(identity())
    with pytest.raises(StaleManagerError):
        if operation == "view":
            old.view(now_ns=104)
        elif operation == "report":
            old.receive(replace(old.report, revision=5), now_ns=105)
        elif operation == "stop":
            stop(old)
        else:
            old.lost_contact(identity(), reason="disconnected", now_ns=105)
    assert history.recover(identity()).report.revision == 4


@pytest.mark.parametrize("change", [{"started_at_utc": "2026-09-16T01:00:00Z"},
    {"session_id": "foreign"}, {"dataset_path": "C:/fixture/other.db"}, {"code_revision": "other"}])
def test_recovery_never_adopts_foreign_identity(tmp_path, change):
    history, manager = running(tmp_path)
    with pytest.raises(ValueError):
        history.recover(identity(**change))
    assert manager.view(now_ns=104)["state"] == "receiving"


@pytest.mark.parametrize("operation", ["stop", "report", "recovery"])
def test_commit_failure_rolls_back_memory_and_disk(tmp_path, monkeypatch, operation):
    history, manager = running(tmp_path)
    original_write = history._write

    @contextmanager
    def failed_commit():
        with original_write() as conn:
            yield conn
            raise sqlite3.OperationalError("injected commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(history, "_write", failed_commit)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            if operation == "stop":
                stop(manager)
            elif operation == "report":
                manager.receive(replace(manager.report, revision=5), now_ns=105)
            else:
                history.recover(identity())
    assert manager.stop_command is None and manager.report.revision == 4
    assert manager.view(now_ns=104)["state"] == "receiving"
    recovered = history.recover(identity())
    assert recovered.stop_command is None and recovered.report.revision == 4


@pytest.mark.parametrize("sql", [
    "DELETE FROM events WHERE ordinal=2",
    "DELETE FROM events WHERE ordinal=4",
    "UPDATE events SET kind='future' WHERE ordinal=4",
    "UPDATE events SET payload='{}' WHERE ordinal=4",
])
def test_corrupt_history_fails_closed(tmp_path, sql):
    history, manager = running(tmp_path)
    with sqlite3.connect(history.path) as conn:
        conn.execute(sql)
    with pytest.raises(ValueError):
        history.recover(identity())
    with sqlite3.connect(history.path) as conn:
        assert conn.execute("SELECT generation FROM sessions").fetchone()[0] == 1


@pytest.mark.parametrize("change", [{"revision": 6}, {"identity": identity(pid=999)},
                                    {"state": "starting", "revision": 5}])
def test_invalid_reconciliation_preserves_history(tmp_path, change):
    history, manager = running(tmp_path)
    recovered = history.recover(identity())
    with pytest.raises(ValueError):
        recovered.receive(replace(manager.report, **change), now_ns=0)
    assert history.recover(identity()).report == manager.report


def test_duplicate_ids_loss_and_sessions_survive_restart(tmp_path):
    history, manager = running(tmp_path)
    command = stop(manager)
    assert manager.request_stop("stop-a", identity(), now_ns=105, timeout_ns=999) == command
    assert manager.view(now_ns=124)["stop_status"] == "unknown"
    manager.lost_contact(identity(), reason="lost", now_ns=125)
    with pytest.raises(sqlite3.IntegrityError):
        history.register(identity(), heartbeat_timeout_ns=10)
    other = identity(session_id="session-b", dataset_path="C:/fixture/b.db")
    second = history.register(other, heartbeat_timeout_ns=10)
    second.receive(CaptureReport(other, 1, "starting"), now_ns=0)
    assert history.recover(identity()).stop_command == command
    assert history.recover(other).stop_command is None
    with sqlite3.connect(history.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2


def test_unregistered_session_cannot_recover(tmp_path):
    with pytest.raises(ValueError, match="not registered"):
        CaptureHistory(tmp_path).recover(identity())


def test_commit_succeeded_but_caller_never_received_command(tmp_path, monkeypatch):
    history, manager = running(tmp_path)
    original_write = history._write

    @contextmanager
    def lost_commit_result():
        with original_write() as conn:
            yield conn
        raise sqlite3.OperationalError("commit result lost")

    with monkeypatch.context() as patch:
        patch.setattr(history, "_write", lost_commit_result)
        with pytest.raises(sqlite3.OperationalError):
            stop(manager)
    assert manager.stop_command is None
    with pytest.raises(StaleManagerError):
        manager.view(now_ns=104)
    recovered = history.recover(identity())
    assert recovered.stop_command.request_id == "stop-a"
    assert recovered.view(now_ns=0)["stop_status"] == "unknown"
    with pytest.raises(ValueError, match="audit-only"):
        recovered.request_stop("stop-a", identity(), now_ns=0, timeout_ns=20)


def test_new_stop_after_recovery_requires_fresh_report(tmp_path):
    history, manager = running(tmp_path)
    recovered = history.recover(identity())
    recovered.receive(replace(manager.report, revision=5), now_ns=0)
    command = recovered.request_stop("new-stop", identity(), now_ns=0, timeout_ns=20)
    assert command.expected_revision == 5
    acknowledge(recovered, command, now_ns=1)
    close(recovered, now_ns=2)
    assert history.recover(identity()).view(now_ns=0)["stop_status"] == "completed"


def test_unknown_storage_schema_is_not_overwritten(tmp_path):
    history, _ = running(tmp_path)
    with sqlite3.connect(history.path) as conn:
        conn.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="schema"):
        history.recover(identity())
    with sqlite3.connect(history.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 99
