"""Missing report replay must not turn stale peer history into current liveness."""
from dataclasses import replace
import sqlite3

import pytest

from control_tower.capture_history import CaptureHistory, StaleManagerError
from control_tower.lifecycle import CaptureReport, Finalization, ProcessIdentity


def setup(tmp_path):
    identity = ProcessIdentity("s", 1, "2026-09-16T00:00:00Z", "C:/fixture/python.exe",
                               "fixture", 32, "fixture", "fixture", "C:/fixture/raw.db")
    history = CaptureHistory(tmp_path)
    manager = history.register(identity, heartbeat_timeout_ns=10)
    first = CaptureReport(identity, 1, "starting")
    manager.receive(first, now_ns=100)
    return history, manager, first


def test_missing_history_is_atomic_and_requires_live_heartbeat(tmp_path):
    history, old, first = setup(tmp_path)
    manager = history.recover(first.identity)
    reports = [replace(first, revision=2, state="login_required"),
               replace(first, revision=3, state="subscribed"),
               replace(first, revision=4, state="receiving")]
    assert manager.reconcile(reports, now_ns=0) == 3
    assert manager.report == reports[-1]
    assert manager.view(now_ns=0)["state"] == "unknown"
    assert not manager.receive(reports[-1], now_ns=1)
    with pytest.raises(ValueError, match="fresh"):
        manager.request_stop("stop", first.identity, now_ns=1, timeout_ns=10)
    assert history.recover(first.identity).report == reports[-1]
    # Use the new owner and a new revision to establish current peer liveness.
    current = history.recover(first.identity)
    current.receive(replace(reports[-1], revision=5), now_ns=0)
    assert current.view(now_ns=0)["state"] == "receiving"
    with pytest.raises(StaleManagerError):
        old.reconcile(reports, now_ns=101)


@pytest.mark.parametrize("bad", ["gap", "duplicate", "transition", "identity"])
def test_bad_tail_rolls_back_entire_page(tmp_path, bad):
    history, manager, first = setup(tmp_path)
    second = replace(first, revision=2, state="login_required")
    tail = replace(first, revision=3, state="subscribed")
    tail = {"gap": replace(tail, revision=4), "duplicate": second,
            "transition": replace(tail, state="receiving"),
            "identity": replace(tail, identity=replace(first.identity, pid=2))}[bad]
    with pytest.raises(ValueError):
        manager.reconcile([second, tail], now_ns=101)
    assert manager.report == first
    assert history.recover(first.identity).report == first


def test_matching_closed_history_resolves_stop_without_resending(tmp_path):
    history, manager, first = setup(tmp_path)
    command = manager.request_stop("stop", first.identity, now_ns=100, timeout_ns=10)
    recovered = history.recover(first.identity)
    draining = replace(first, revision=2, state="draining", input_stopped=True, stop_request_id=command.request_id)
    closed = replace(draining, revision=3, state="closed", writer_closed=True,
                     finalization=Finalization(0, 1, "a" * 64))
    recovered.reconcile([draining, closed], now_ns=0)
    assert recovered.view(now_ns=0)["stop_status"] == "completed"
    assert history.recover(first.identity).view(now_ns=0)["stop_status"] == "completed"
    with pytest.raises(ValueError, match="audit-only"):
        recovered.request_stop("stop", first.identity, now_ns=0, timeout_ns=20)


@pytest.mark.parametrize("size", [0, 129])
def test_batch_is_bounded(tmp_path, size):
    _, manager, first = setup(tmp_path)
    with pytest.raises(ValueError, match="bounded"):
        manager.reconcile([first] * size, now_ns=101)


def test_database_write_error_does_not_publish_partial_reconciliation(tmp_path, monkeypatch):
    history, manager, first = setup(tmp_path)
    original = history._append
    def fail(*args):
        original(*args)
        raise sqlite3.OperationalError("journal write failed")
    with monkeypatch.context() as patch:
        patch.setattr(history, "_append", fail)
        with pytest.raises(sqlite3.OperationalError):
            manager.reconcile([replace(first, revision=2, state="login_required")], now_ns=101)
    assert manager.report == first
    assert history.recover(first.identity).report == first
