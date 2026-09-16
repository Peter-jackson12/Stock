from datetime import datetime, timezone
import json
import sqlite3

import pytest

from collector.kiwoom.live_capture import LiveRawCapture
from collector.raw_v2 import read_raw_v2
from control_tower.report_journal import ReplayRequest
from control_tower.windows_process import ProcessFacts
from tests.test_tick_collector_shutdown import collector  # Reuse offline Qt/OCX harness.


@pytest.fixture
def live(collector, monkeypatch, tmp_path):
    old, messages, _ = collector
    facts = ProcessFacts(123, "2026-09-16T00:00:00Z", "C:/fixture/python.exe", 32)
    monkeypatch.setitem(old._on_login.__globals__, "LiveRawCapture",
        lambda root, **kwargs: LiveRawCapture(tmp_path, facts=facts, **kwargs))
    logger = type(old)(code_revision="fixture")  # Exercise the operational default.
    monkeypatch.setattr(logger, "_stats_worker", lambda: None)
    monkeypatch.setattr(logger, "_register_all_universe", lambda: None)
    values = {20: "090000", 10: " -10000 ", 15: "+2", 14: "100000", 27: "10001", 28: "10000", 21: "090001"}
    values.update({i: "10001" for i in range(41, 51)})
    values.update({i: "10000" for i in range(51, 61)})
    values.update({i: "3" for i in range(61, 81)})
    def call(method, *args):
        if method.startswith("KOA_Functions"):
            return "0"
        if method.startswith("GetConnectState"):
            return 1
        if method.startswith("GetCommRealData"):
            return values[args[1]]
        return "0"
    logger.ocx.dynamicCall = call
    yield logger, values, messages
    if not logger._shutdown_done:
        logger._shutdown("test cleanup")


def test_operational_default_preserves_raw_and_closes_new_file(live):
    logger, _, messages = live
    assert logger.storage == "raw-v2"
    logger._on_login(0)
    logger._on_receive_real_data("005930", "주식체결", "")
    logger._on_receive_real_data("005930", "주식호가잔량", "")
    logger._shutdown("test stop")
    assert logger.exit_code == 0
    with read_raw_v2(logger.db_path) as (meta, rows):
        records = list(rows)
    assert len(records) == 4  # start + trade + direction-unverified + quote
    trade = records[1]
    assert trade["raw_fields"]["fids"]["10"] == " -10000 "
    assert trade["raw_fields"]["fids"]["14"] == "100000"
    assert trade["event"].is_buy is None and trade["event"].venue == "unknown"
    assert meta["payload_sha256"] == logger.raw_capture.report.finalization.payload_sha256
    page = logger.raw_capture.journal.page(ReplayRequest(logger.raw_capture.identity, 0))
    assert [r.state for r in page.reports] == ["starting", "draining", "closed"]
    assert all(r.stop_request_id is None for r in page.reports)  # Local close is not a remote acknowledgement.
    assert not list(logger.db_path.parents[3].glob("raw_ticks/*"))
    status = json.loads((logger.raw_capture.directory / "status.json").read_text(encoding="utf-8"))
    assert status["snapshot"]["state"] == "closed" and status["control_heartbeat"] is False


def test_bad_fid_keeps_original_and_quality_issue(live):
    logger, values, _ = live
    values[41] = "bad"
    logger._on_login(0)
    logger._on_receive_real_data("005930", "주식호가잔량", "")
    logger._shutdown("test")
    with read_raw_v2(logger.db_path) as (_, rows):
        rows = list(rows)
    assert rows[1]["raw_fields"]["fids"]["41"] == "bad"
    assert rows[1]["event"].ask is None
    assert rows[2]["event"].control_type == "parse_error"


def test_fid_read_exception_preserves_partial_callback_and_interrupts(live):
    logger, values, _ = live
    logger._on_login(0)
    del values[15]  # Failure after 20 and 10 were read.
    logger._on_receive_real_data("005930", "주식체결", "")
    logger._poll_control()
    assert logger.exit_code == 2 and logger._shutdown_done
    with sqlite3.connect(logger.db_path) as conn:
        meta = json.loads(conn.execute("SELECT value FROM metadata").fetchone()[0])
        rows = [json.loads(row[0]) for row in conn.execute("SELECT payload FROM events ORDER BY seq")]
    assert meta["state"] == "incomplete"
    assert rows[-1]["event"]["control_type"] == "callback_error"
    assert rows[-1]["event"]["details"]["fids"]["10"] == " -10000 "
    assert logger.raw_capture.report.state != "closed"


def test_connection_loss_is_not_clean_finalization(live):
    logger, _, _ = live
    logger._on_login(0)
    logger.ocx.dynamicCall = lambda *args: 0
    logger._poll_control()
    assert logger.exit_code == 2 and logger._shutdown_done
    assert logger.raw_capture.queue.snapshot()["state"] == "interrupted"


def test_second_login_does_not_replace_active_session(live):
    logger, _, _ = live
    logger._on_login(0)
    identity = logger.raw_capture.identity
    logger._on_login(0)
    assert logger.exit_code == 2 and logger.raw_capture.identity == identity
    assert logger.raw_capture.queue.snapshot()["state"] == "interrupted"


def test_unknown_server_stops_before_creating_raw(live):
    logger, _, _ = live
    logger.ocx.dynamicCall = lambda *args: "unrecognized"
    logger._on_login(0)
    assert logger.exit_code == 2 and logger.raw_capture is None


def test_subscription_rejection_interrupts_backend(live, monkeypatch):
    logger, _, _ = live
    original = logger.ocx.dynamicCall
    def call(method, *args):
        if method.startswith("GetCodeListByMarket"):
            return "005930;" if args[0] == "0" else ""
        if method.startswith("GetMasterCodeName"):
            return "삼성전자"
        if method.startswith("SetRealReg"):
            return -200
        return original(method, *args)
    logger.ocx.dynamicCall = call
    monkeypatch.setattr(logger, "_register_all_universe", type(logger)._register_all_universe.__get__(logger))
    logger._on_login(0)
    assert logger.exit_code == 2 and logger._shutdown_done
    assert logger.raw_capture.queue.snapshot()["state"] == "interrupted"


def test_status_reader_distinguishes_counts_and_stale_closed(live):
    from datetime import timedelta
    from control_tower.status import observe_raw_capture
    logger, _, _ = live
    logger._on_login(0)
    logger._on_receive_real_data("005930", "주식체결", "")
    logger._shutdown("test")
    root = logger.raw_capture.latest_status_path.parents[1]
    observed = observe_raw_capture(root)
    assert observed["status"] == "recent" and observed["process_state"] == "unverified"
    assert observed["payload"]["snapshot"]["accepted_callbacks"] == 1
    assert observed["payload"]["snapshot"]["committed_seq"] == 3
    stale = observe_raw_capture(root, now=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert stale["status"] == "stale" and stale["payload"]["snapshot"]["state"] == "closed"
    path = logger.raw_capture.latest_status_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["snapshot"]["pending_callbacks"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert observe_raw_capture(root)["status"] == "unavailable"


def test_collector_lease_blocks_second_instance_and_releases(tmp_path):
    import os
    from collector.kiwoom.collector_lease import CollectorLease
    if os.name != "nt":
        pytest.skip("Windows collector lease")
    with CollectorLease(tmp_path):
        with pytest.raises(RuntimeError, match="another collector"):
            with CollectorLease(tmp_path):
                pytest.fail("second collector was admitted")
    with CollectorLease(tmp_path):
        pass  # A remaining lock file is not evidence of a live owner.


def test_status_reader_is_bounded_and_does_not_create_files(tmp_path):
    from control_tower.status import observe_raw_capture, MAX_LOG_BYTES
    assert observe_raw_capture(tmp_path)["status"] == "unavailable"
    assert not list(tmp_path.iterdir())
    path = tmp_path / "operations_state" / "capture_status.json"
    path.parent.mkdir()
    path.write_bytes(b"x" * (MAX_LOG_BYTES + 1))
    observed = observe_raw_capture(tmp_path)
    assert observed["status"] == "unavailable" and "size limit" in observed["reason"]
