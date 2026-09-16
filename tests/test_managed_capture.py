from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest

from control_tower.managed_capture import ManagedCaptures, validate_plan, start_managed_capture, TOKEN_ENV
from control_tower.windows_process import ProcessFacts
from collector.kiwoom.live_capture import LiveRawCapture
from tests.test_live_collector import live
from tests.test_tick_collector_shutdown import collector


@pytest.fixture
def managed(tmp_path):
    store = ManagedCaptures(tmp_path)
    facts = ProcessFacts(123, "2026-09-16T00:00:00Z", "C:/fixture/python.exe", 32)
    launch, token = store.create(["005930"], 60, "live", [facts.executable])
    return store, facts, launch, token


def test_capability_once_and_single_active(managed):
    store, facts, launch, token = managed
    with pytest.raises(ValueError):
        store.claim(launch, "wrong", facts)
    with pytest.raises(ValueError):
        store.claim(launch, token, replace(facts, python_bits=64))
    assert store.claim(launch, token, facts)["codes"] == ["005930"]
    with pytest.raises(ValueError):
        store.claim(launch, token, facts)
    with pytest.raises(sqlite3.IntegrityError):
        store.create(["005930"], 60, "live", [facts.executable])
    assert "token_hash" not in store.get(launch)


def test_cancel_unclaimed_revokes_late_child(managed):
    store, facts, launch, token = managed
    store.request_stop(launch)
    with pytest.raises(ValueError):
        store.claim(launch, token, facts)
    assert store.get(launch)["state"] == "cancelled"
    store.create(["005930"], 60, "live", [facts.executable])


def test_stop_persisted_once_and_closed_ack(managed, tmp_path):
    store, facts, launch, token = managed
    store.claim(launch, token, facts)
    runtime = LiveRawCapture(tmp_path, facts=facts, server="live", code_revision="test")
    try:
        store.bind(launch, facts, runtime.report)
        request = store.request_stop(launch)
        assert store.request_stop(launch) == request
        with pytest.raises(ValueError):
            store.poll(launch, replace(facts, started_at_utc="2026-09-17T00:00:00Z"))
        stop_id, command = store.poll(launch, facts)
        assert stop_id == request and store.get(launch)["stop_accepted"] == 1
        assert ManagedCaptures(tmp_path).poll(launch, facts) is None
        assert runtime.finish("managed stop", command)
        store.finish(launch, facts, runtime.report)
        assert store.get(launch)["state"] == "closed"
    finally:
        runtime.finish("cleanup")


def test_cancel_during_login_has_no_fake_closed_report(managed):
    store, facts, launch, token = managed
    store.claim(launch, token, facts)
    store.request_stop(launch)
    _, command = store.poll(launch, facts)
    assert command is None
    store.finish(launch, facts)
    assert store.get(launch)["state"] == "cancelled"
    assert store.get(launch)["final_report"] is None


@pytest.mark.parametrize("codes,duration,server", [([[]], 60, "live"), (["005930"] * 2, 60, "live"),
    (["005930"], True, "live"), (["005930"], 301, "live"), (["005930"], 60, "unknown")])
def test_invalid_plan(codes, duration, server):
    with pytest.raises(ValueError):
        validate_plan(codes, duration, server)


class FakePeer:
    def __init__(self, store, facts, launch):
        self.store, self.facts, self.launch = store, facts, launch
        self.plan = store.get(launch)["plan"]
        self.stop = None

    def poll(self):
        self.stop = self.store.poll(self.launch, self.facts) or self.stop
        return bool(self.stop)

    def bind(self, report):
        self.store.bind(self.launch, self.facts, report)


def test_qt_stop_drains_and_acknowledges(live, managed):
    logger, _, _ = live
    store, facts, launch, token = managed
    store.claim(launch, token, facts)
    logger.managed = FakePeer(store, facts, launch)
    logger._on_login(0)
    logger._on_receive_real_data("005930", "주식체결", "")
    request = store.request_stop(launch)
    logger._poll_control()
    assert logger.exit_code == 0 and logger._shutdown_done
    assert logger.raw_capture.report.stop_request_id == request
    store.finish(launch, facts, logger.raw_capture.report)


def test_wrong_server_does_not_create_raw(live, managed):
    logger, _, _ = live
    store, facts, launch, token = managed
    store.claim(launch, token, facts)
    logger.managed = FakePeer(store, facts, launch)
    logger.managed.plan["server"] = "mock"
    logger._on_login(0)
    assert logger.raw_capture is None and logger.exit_code == 2


def test_cancelled_login_never_creates_raw(live, managed):
    logger, _, _ = live
    store, facts, launch, token = managed
    store.claim(launch, token, facts)
    logger.managed = FakePeer(store, facts, launch)
    store.request_stop(launch)
    logger._on_login(0)
    assert logger.raw_capture is None and logger._shutdown_done


def nonexistent(pid):
    exc = OSError("process missing")
    exc.winerror = 87
    raise exc


def test_crash_after_journal_close_reconciles_without_repeating_stop(managed, tmp_path):
    store, facts, launch, token = managed
    store.claim(launch, token, facts)
    runtime = LiveRawCapture(tmp_path, facts=facts, server="live", code_revision="test")
    store.bind(launch, facts, runtime.report)
    store.request_stop(launch)
    _, command = store.poll(launch, facts)
    assert runtime.finish("test", command)
    assert store.get(launch)["state"] == "stopping"
    recovered = ManagedCaptures(tmp_path)
    assert recovered.reconcile(launch, process_factory=nonexistent) == "closed"
    assert recovered.get(launch)["stop_accepted"] == 1


def test_dead_process_without_final_report_is_failed(managed):
    store, facts, launch, token = managed
    store.claim(launch, token, facts)
    assert store.reconcile(launch, process_factory=nonexistent) == "failed"


def test_access_denied_is_not_proof_of_exit(managed):
    store, facts, launch, token = managed
    store.claim(launch, token, facts)
    def denied(pid):
        exc = OSError("access denied")
        exc.winerror = 5
        raise exc
    with pytest.raises(OSError):
        store.reconcile(launch, process_factory=denied)
    assert store.get(launch)["state"] == "launching"


def test_real_32bit_child_claim_stop_and_journal_recovery(tmp_path):
    root = Path(__file__).resolve().parents[1]
    python = root / ".venv32/Scripts/python.exe"
    if not python.exists():
        pytest.skip("32-bit environment unavailable")
    config = dict(line.split(" = ", 1) for line in (root / ".venv32/pyvenv.cfg").read_text().splitlines() if " = " in line)
    store = ManagedCaptures(tmp_path)
    launch, token = store.create(["005930"], 1, "mock", [str(python), str(Path(config["home"]) / "python.exe")])
    program = '''
import os, sys, time
from collector.kiwoom.collector_lease import CollectorLease
from collector.kiwoom.live_capture import LiveRawCapture
from control_tower.managed_capture import ManagedCapturePeer, TOKEN_ENV
with CollectorLease(sys.argv[1]):
    peer = ManagedCapturePeer(sys.argv[1], sys.argv[2], os.environ.pop(TOKEN_ENV))
    capture = LiveRawCapture(sys.argv[1], server="mock", code_revision="synthetic-child")
    try:
        peer.bind(capture.report)
        deadline = time.monotonic() + 10
        while not peer.poll():
            if time.monotonic() > deadline: raise RuntimeError("no stop")
            time.sleep(.02)
        assert capture.finish("synthetic remote stop", peer.stop[1])
        # Deliberately omit peer.finish: simulate interruption after durable close.
    finally:
        capture.finish("cleanup")
'''
    env = os.environ.copy()
    env["STOCK_CAPTURE_LAUNCH_TOKEN"] = token
    child = subprocess.Popen([str(python), "-c", program, str(tmp_path), launch], cwd=root,
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + 10
    while store.get(launch)["identity"] is None and child.poll() is None and time.monotonic() < deadline:
        time.sleep(.02)
    assert store.get(launch)["identity"] is not None
    store.request_stop(launch)
    stdout, stderr = child.communicate(timeout=15)
    assert child.returncode == 0, stdout + stderr
    assert store.reconcile(launch) == "closed"
    assert store.get(launch)["stop_accepted"] == 1


def test_spawn_uses_fixed_script_and_environment_capability(tmp_path):
    for relative in (".venv32/Scripts/python.exe", "collector/kiwoom/kiwoom_universe_logger.py", "base/python.exe"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (tmp_path / ".venv32/pyvenv.cfg").write_text(f"home = {tmp_path / 'base'}")
    calls = []
    launch = start_managed_capture(tmp_path, ["005930"], 60, "mock", popen=lambda args, **kw: calls.append((args, kw)))
    args, kw = calls[0]
    assert args[-2:] == ["--managed-launch", launch]
    assert kw["env"][TOKEN_ENV] not in " ".join(args)
    assert kw["cwd"] == tmp_path and kw["stdin"] == subprocess.DEVNULL
    assert ManagedCaptures(tmp_path).get(launch)["state"] == "launching"


def test_uncertain_spawn_revokes_late_claim_only_on_explicit_cancel(managed):
    store, facts, launch, token = managed
    store.spawn_uncertain(launch, "spawn failed")
    assert store.get(launch)["state"] == "unknown"
    with pytest.raises(ValueError):
        store.claim(launch, token, facts)
    store.request_stop(launch)
    assert store.get(launch)["state"] == "cancelled"
