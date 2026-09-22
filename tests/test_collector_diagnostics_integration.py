"""PR #16/#17 조합 회귀. Qt/OCX는 대역, raw는 TEMP 합성 파일만 사용한다."""
import json
import threading

import pytest

from collector.kiwoom.capture_telemetry import CaptureTelemetry
from collector.kiwoom.ocx_teardown import OcxTeardown
from tests.test_ocx_teardown import Control
from tests.test_live_collector import live
from tests.test_tick_collector_shutdown import collector


def configure(logger, *, telemetry_on=True, teardown_on=True, clear_fails=False):
    probe = CaptureTelemetry() if telemetry_on else None
    logger.telemetry = probe
    logger._ocx_teardown = OcxTeardown(
        owner_thread=threading.get_ident(), enabled=teardown_on)
    reentries = []

    def during_clear():
        assert logger._shutdown_done and not logger.accepting_events
        assert logger.raw_capture.queue.wait(0)
        before = logger.raw_capture.queue.snapshot()
        assert before["writer_closed"] and before["pending_callbacks"] == 0
        assert logger.app.quits == 0
        # Native clear가 동기적으로 Qt 콜백을 재진입시키는 경우의 대역이다.
        logger._on_receive_real_data("005930", "주식체결", "")
        logger._on_login(0)
        logger._poll_control()
        logger._shutdown("synthetic reentry")
        assert logger.raw_capture.queue.snapshot() == before
        reentries.append(True)

    control = Control(hook=during_clear, fail=clear_fails)
    logger.ocx.clear = control.clear
    logger.ocx.isNull = control.isNull
    return probe, control, reentries


def final_status(logger):
    return json.loads((logger.raw_capture.directory / "status.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("telemetry_on,teardown_on", [
    (False, False), (False, True), (True, False), (True, True),
])
def test_four_option_combinations_keep_storage_and_shutdown_contract(live, telemetry_on, teardown_on):
    logger, _, _ = live
    probe, control, reentries = configure(
        logger, telemetry_on=telemetry_on, teardown_on=teardown_on)
    logger._on_login(0)
    logger._on_receive_real_data("005930", "주식체결", "")
    logger._shutdown_requested = "synthetic combined shutdown"
    logger._poll_control()
    logger._finish_process_resources()
    logger._shutdown("duplicate")
    logger._finish_process_resources()
    assert logger.app.quits == 1 and logger.exit_code == 0
    assert control.calls == int(teardown_on)
    assert reentries == ([True] if teardown_on else [])
    status = final_status(logger)
    state = status["snapshot"]
    assert state["state"] == "closed" and state["writer_closed"]
    assert state["accepted_callbacks"] == state["committed_callbacks"] == 1
    assert state["pending_callbacks"] == state["dropped_callbacks"] == 0
    assert state["data_quality"] == "unverified" and status["control_heartbeat"] is False
    if telemetry_on:
        assert probe.closed and probe.error is None
        records = [json.loads(line) for line in probe.path.read_text(encoding="utf-8").splitlines()]
        last = records[-1]["poll"]
        assert last["entries"] == last["returns"] == 1
        assert last["in_flight"] == 0
        assert sum(len(row.get("samples", [])) for row in records) == 1
    else:
        assert not (logger.raw_capture.directory / "capture_telemetry.jsonl").exists()


@pytest.mark.parametrize("method", ["callback_sample", "poll_enter", "flush"])
def test_observer_failure_cannot_skip_drain_or_native_release(live, monkeypatch, method):
    logger, _, _ = live
    probe, control, _ = configure(logger)
    logger._on_login(0)

    def fail(*args, **kwargs):
        raise OSError("synthetic observation failure")

    monkeypatch.setattr(probe, method, fail)
    logger._on_receive_real_data("005930", "주식체결", "")
    logger._shutdown_requested = "synthetic observer failure"
    logger._poll_control()
    logger._finish_process_resources()
    assert probe.error and probe.closed
    assert control.calls == 1 and logger.exit_code == 0
    state = logger.raw_capture.queue.snapshot()
    assert state["state"] == "closed" and state["writer_closed"]
    assert state["accepted_callbacks"] == state["committed_callbacks"] == 1
    assert logger.raw_capture.error is None


@pytest.mark.parametrize("loop_raises", [False, True])
def test_real_start_finally_closes_telemetry_after_last_poll(live, loop_raises):
    logger, _, _ = live
    probe, control, _ = configure(logger)

    def loop():
        logger._on_login(0)
        logger._on_receive_real_data("005930", "주식체결", "")
        logger._shutdown_requested = "synthetic loop exit"
        logger._poll_control()
        if loop_raises:
            raise RuntimeError("synthetic loop exception after drain")
        return 0

    logger.app.exec_ = loop
    if loop_raises:
        with pytest.raises(RuntimeError, match="synthetic loop exception after drain"):
            logger.start()
    else:
        logger.start()
    assert probe.closed and control.calls == 1 and logger.app.quits == 1
    records = [json.loads(line) for line in probe.path.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["poll"]["entries"] == records[-1]["poll"]["returns"] == 1
    assert final_status(logger)["snapshot"]["state"] == "closed"


@pytest.mark.parametrize("telemetry_on", [False, True])
def test_failed_clear_is_not_retried_or_relabelled_by_telemetry(live, telemetry_on):
    logger, _, _ = live
    probe, control, _ = configure(logger, telemetry_on=telemetry_on, clear_fails=True)
    logger._on_login(0)
    logger._on_receive_real_data("005930", "주식체결", "")
    logger._shutdown_requested = "synthetic clear failure"
    logger._poll_control()
    logger._finish_process_resources()
    logger._finish_process_resources()
    assert logger._ocx_teardown.state == "failed" and control.calls == 1
    assert logger.exit_code == 2 and logger.app.quits == 1
    state = final_status(logger)["snapshot"]
    assert state["state"] == "closed" and state["data_quality"] == "unverified"
    if probe is not None:
        assert probe.closed and probe.error is None
