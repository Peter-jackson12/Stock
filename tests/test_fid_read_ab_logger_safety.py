"""실제 logger 경로를 fake Qt/OCX와 작은 임시 저장으로 검증한다."""
import json
from pathlib import Path
import sqlite3

import pytest

from collector.kiwoom.capture_telemetry import CaptureTelemetry
from collector.kiwoom.fid_read_ab_diagnostic import (
    FEED_SCOPE_DIAGNOSTIC, PHASE_PRE, PHASE_A1, PHASE_B, PHASE_A2,
    SIDECAR_NAME, FidReadAbController,
)
from collector.kiwoom.live_capture import TRADE_FIDS
from collector.raw_v2 import read_raw_v2
from tests.test_live_collector import live
from tests.test_tick_collector_shutdown import collector


@pytest.fixture
def diagnostic(live):
    logger, values, messages = live
    clock = [100.0]
    logger.fid_read_ab_test = True
    logger.duration_seconds = 90
    logger._monotonic = lambda: clock[0]
    logger._fid_ab = FidReadAbController(monotonic=logger._monotonic)
    logger.telemetry = CaptureTelemetry()
    calls = []
    original = logger.ocx.dynamicCall
    def call(method, *args):
        calls.append((method, args))
        if method.startswith("KOA_Functions"):
            return "1"
        return original(method, *args)
    logger.ocx.dynamicCall = call
    try:
        yield logger, values, messages, clock, calls
    finally:
        if not logger._shutdown_done:
            logger._shutdown("synthetic cleanup")
        logger._finish_process_resources()


@pytest.mark.parametrize("server_flag", ["", "0", "unknown"])
def test_non_mock_login_rejected_before_backend_and_subscription(diagnostic, monkeypatch, server_flag):
    logger, _, _, _, calls = diagnostic
    original = logger.ocx.dynamicCall
    def call(method, *args):
        if method.startswith("KOA_Functions"):
            return server_flag
        return original(method, *args)
    logger.ocx.dynamicCall = call
    def forbidden(*args, **kwargs):
        raise AssertionError("diagnostic rejection must precede subscription/backend")
    monkeypatch.setattr(logger, "_register_all_universe", forbidden)
    monkeypatch.setitem(logger._on_login.__globals__, "LiveRawCapture", forbidden)
    logger._on_login(0)
    assert logger._shutdown_done and logger.exit_code == 2 and logger.raw_capture is None
    assert not any(method.startswith("SetRealReg") for method, _ in calls)


def test_actual_registration_is_not_repeated_at_phase_switches(diagnostic, monkeypatch):
    logger, _, _, clock, calls = diagnostic
    original = logger.ocx.dynamicCall
    def call(method, *args):
        if method.startswith("GetCodeListByMarket"):
            return "005930;" if args[0] == "0" else "000660;"
        if method.startswith("GetMasterCodeName"):
            return "합성 보통주"
        return original(method, *args)
    logger.ocx.dynamicCall = call
    real_register = type(logger)._register_all_universe.__get__(logger)
    def register():
        real_register()
        logger._on_receive_real_data("005930", "주식체결", "")
    monkeypatch.setattr(logger, "_register_all_universe", register)
    logger._on_login(0)
    assert logger.raw_capture is not None and not logger._shutdown_done
    registrations = [entry for entry in calls if entry[0].startswith("SetRealReg")]
    assert len(registrations) == 1
    assert registrations[0][1][1] == "005930;000660"
    for elapsed, expected in [(1, list(TRADE_FIDS)), (30, [20, 10, 15]), (60, list(TRADE_FIDS))]:
        clock[0] = 100 + elapsed
        start = len(calls)
        logger._on_receive_real_data("005930", "주식체결", "")
        reads = [args[1] for method, args in calls[start:] if method.startswith("GetCommRealData")]
        assert reads == expected
        assert [entry for entry in calls if entry[0].startswith("SetRealReg")] == registrations
    logger._shutdown("synthetic A-B-A completion")
    with read_raw_v2(logger.db_path) as (manifest, rows):
        records = list(rows)
    assert manifest["feed_scope"] == FEED_SCOPE_DIAGNOSTIC
    assert len(records) == 5
    assert records[3]["raw_fields"]["fids"]["14"] is None
    assert records[4]["raw_fields"]["fids"]["14"] is not None
    sidecar = json.loads((logger.raw_capture.directory / SIDECAR_NAME).read_text(encoding="utf-8"))
    for phase in (PHASE_PRE, PHASE_A1, PHASE_B, PHASE_A2):
        assert sidecar["phase_counters"]["trade_callbacks_by_phase"][phase] == 1
    assert sidecar["research_eligible"] is False
    assert logger.raw_capture.queue.snapshot()["state"] == "closed"
    assert logger.raw_capture.queue.done.is_set()


def test_sidecar_initial_write_failure_prevents_subscription_and_drains(diagnostic, monkeypatch):
    logger, _, messages, _, _ = diagnostic
    original = Path.write_text
    def write(path, *args, **kwargs):
        if path.name == SIDECAR_NAME:
            raise OSError("sidecar initial write sentinel")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", write)
    registered = []
    monkeypatch.setattr(logger, "_register_all_universe", lambda: registered.append(True))
    logger._on_login(0)
    assert logger._shutdown_done and logger.exit_code == 2 and registered == []
    assert logger.raw_capture is not None and logger.raw_capture.queue.done.is_set()
    assert any("sidecar initial write sentinel" in message for message in messages)
    assert logger.db_path is None or Path(logger.db_path).is_file()
    assert logger.raw_capture.path.is_file()


def test_sidecar_final_replace_failure_preserves_old_evidence_and_storage_completion(diagnostic, monkeypatch):
    logger, _, messages, _, _ = diagnostic
    logger._on_login(0)
    sidecar = logger.raw_capture.directory / SIDECAR_NAME
    before = sidecar.read_bytes()
    original = Path.replace
    def replace(path, target):
        if path == sidecar.with_suffix(".tmp"):
            raise OSError("sidecar final replace sentinel")
        return original(path, target)
    monkeypatch.setattr(Path, "replace", replace)
    logger._on_receive_real_data("005930", "주식체결", "")
    logger._shutdown("synthetic sidecar failure")
    assert sidecar.read_bytes() == before
    assert any("sidecar counter flush failed" in message for message in messages)
    assert logger.raw_capture.queue.snapshot()["state"] == "closed"
    assert logger.raw_capture.queue.done.is_set()
    with read_raw_v2(logger.db_path) as (_, rows):
        assert len(list(rows)) == 2


def test_diagnostic_read_failure_survives_in_stored_callback_error(diagnostic):
    logger, values, _, clock, _ = diagnostic
    logger._on_login(0)
    clock[0] = 140.0
    del values[15]
    logger._on_receive_real_data("005930", "주식체결", "")
    logger._poll_control()
    if not logger._shutdown_done:
        logger._shutdown("synthetic FID failure")
    assert logger.exit_code == 2 and logger.raw_capture.queue.done.is_set()
    conn = sqlite3.connect(logger.raw_capture.path)
    try:
        rows = [json.loads(row[0]) for row in conn.execute("SELECT payload FROM events ORDER BY seq")]
    finally:
        conn.close()
    failures = [row for row in rows if row["event"].get("control_type") == "callback_error"]
    assert len(failures) == 1
    assert failures[0]["raw_fields"]["fids"]["20"] == "090000"
    assert failures[0]["raw_fields"]["fids"]["10"] == " -10000 "
    assert "_read_error" in failures[0]["raw_fields"]["fids"]
    sidecar = json.loads((logger.raw_capture.directory / SIDECAR_NAME).read_text(encoding="utf-8"))
    assert sidecar["phase_counters"]["fid_attempts_by_phase"][PHASE_B] == 3
    assert sidecar["phase_counters"]["fid_calls_by_phase"][PHASE_B] == 2
    assert sidecar["phase_counters"]["fid_read_failures_by_phase"][PHASE_B] == 1
