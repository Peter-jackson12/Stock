"""Synchronous fake sender only; no live process control or network transport."""
import sqlite3
import threading

import pytest

from control_tower.capture_history import CaptureHistory, StaleManagerError
from control_tower.lifecycle import CaptureReport, ProcessIdentity, StopReceiver


def setup(tmp_path):
    identity = ProcessIdentity("s", 1, "2026-09-16T00:00:00Z", "C:/fixture/python.exe",
                               "fixture", 32, "fixture", "fixture", "C:/fixture/raw.db")
    history = CaptureHistory(tmp_path)
    manager = history.register(identity, heartbeat_timeout_ns=10)
    report = CaptureReport(identity, 1, "starting")
    manager.receive(report, now_ns=0)
    manager.request_stop("stop", identity, now_ns=0, timeout_ns=10)
    return history, manager, report


def test_dispatch_calls_sender_once_and_does_not_claim_completion(tmp_path):
    history, manager, report = setup(tmp_path)
    peer, calls = StopReceiver(report.identity), []
    def send(command):
        calls.append(command)
        assert peer.accept(command, report)
    manager.dispatch_stop(send, now_ns=1)
    assert len(calls) == 1
    assert manager.view(now_ns=1)["stop_status"] == "pending"
    with pytest.raises(ValueError):
        manager.dispatch_stop(send, now_ns=2)
    with pytest.raises(ValueError, match="audit-only"):
        history.recover(report.identity).dispatch_stop(send, now_ns=0)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [TimeoutError, KeyboardInterrupt])
def test_sender_failure_is_unknown_and_never_retried(tmp_path, failure):
    history, manager, report = setup(tmp_path)
    calls = []
    def send(command):
        calls.append(command)
        raise failure("reply lost after possible delivery")
    with pytest.raises(failure):
        manager.dispatch_stop(send, now_ns=1)
    assert manager.view(now_ns=1)["stop_status"] == "unknown"
    with pytest.raises(ValueError):
        manager.dispatch_stop(send, now_ns=2)
    assert history.recover(report.identity).view(now_ns=0)["stop_status"] == "unknown"
    assert len(calls) == 1


def test_takeover_between_intent_and_send_blocks_old_sender(tmp_path, monkeypatch):
    history, manager, report = setup(tmp_path)
    mutate = manager._mutate
    def takeover(*args):
        result = mutate(*args)
        history.recover(report.identity)
        return result
    monkeypatch.setattr(manager, "_mutate", takeover)
    calls = []
    with pytest.raises(StaleManagerError):
        manager.dispatch_stop(calls.append, now_ns=1)
    assert calls == []


def test_intent_write_failure_prevents_delivery(tmp_path, monkeypatch):
    history, manager, _ = setup(tmp_path)
    calls = []
    def fail(*args):
        raise sqlite3.OperationalError("disk error")
    monkeypatch.setattr(history, "_append", fail)
    with pytest.raises(sqlite3.OperationalError):
        manager.dispatch_stop(calls.append, now_ns=1)
    assert calls == []


def test_recovery_waits_until_local_sender_acceptance_finishes(tmp_path):
    history, manager, report = setup(tmp_path)
    recovery_started, recovery_done = threading.Event(), threading.Event()
    failures = []
    def recover():
        recovery_started.set()
        try:
            history.recover(report.identity)
        except Exception as exc:
            failures.append(exc)
        finally:
            recovery_done.set()
    thread = threading.Thread(target=recover)
    def send(command):
        thread.start()
        assert recovery_started.wait(1)
        assert not recovery_done.wait(.05)
        assert command == manager.stop_command
    manager.dispatch_stop(send, now_ns=1)
    thread.join(5)
    assert recovery_done.is_set() and not failures
    with pytest.raises(StaleManagerError):
        manager.view(now_ns=2)
