"""Qt/OCX 대역과 작은 합성 raw만 사용한다. 네이티브 실측이 아니다."""
import json
import signal
import threading
from types import SimpleNamespace

import pytest

from collector.kiwoom.capture_diagnostics import CaptureDiagnostics
from collector.kiwoom.ocx_teardown import OcxTeardown, record_phase
from tests.test_capture_diagnostics import Handler
from tests.test_tick_collector_shutdown import collector, start_writer
from tests.test_live_collector import live


class Control:
    def __init__(self, hook=lambda: None, *, fail=False, stays_nonnull=False):
        self.calls = 0
        self.null = False
        self.hook = hook
        self.fail = fail
        self.stays_nonnull = stays_nonnull

    def isNull(self):
        return self.null

    def clear(self):
        self.calls += 1
        self.hook()
        if self.fail:
            raise RuntimeError("synthetic clear failure")
        self.null = not self.stays_nonnull


def enabled(logger, *, hook=lambda: None, fail=False):
    logger._ocx_teardown = OcxTeardown(owner_thread=threading.get_ident(), enabled=True)
    control = Control(hook, fail=fail)
    logger.ocx.clear = control.clear
    logger.ocx.isNull = control.isNull
    return control


def phases(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith('{') and '"collector_phase"' in line]


@pytest.mark.parametrize("enabled_flag", [False, True])
def test_no_control_and_repeated_release(enabled_flag):
    lifecycle = OcxTeardown(owner_thread=threading.get_ident(), enabled=enabled_flag)
    assert lifecycle.release(None, writer_stopped=True)
    assert lifecycle.release(None, writer_stopped=True)
    assert lifecycle.state == ("absent" if enabled_flag else "disabled")


def test_default_does_not_touch_native_methods():
    lifecycle = OcxTeardown(owner_thread=threading.get_ident())
    assert lifecycle.release(object(), writer_stopped=False)
    assert lifecycle.state == "disabled"


@pytest.mark.parametrize("fail,nonnull", [(False, False), (True, False), (False, True)])
def test_clear_attempt_is_once_even_after_failure(fail, nonnull):
    lifecycle = OcxTeardown(owner_thread=threading.get_ident(), enabled=True)
    control = Control(fail=fail, stays_nonnull=nonnull)
    expected = not (fail or nonnull)
    assert lifecycle.release(control, writer_stopped=True) is expected
    assert lifecycle.release(control, writer_stopped=True) is expected
    assert control.calls == 1
    assert lifecycle.state == ("cleared" if expected else "failed")


def test_null_control_is_not_cleared_again():
    lifecycle = OcxTeardown(owner_thread=threading.get_ident(), enabled=True)
    control = Control()
    control.null = True
    assert lifecycle.release(control, writer_stopped=True)
    assert control.calls == 0


def test_deferred_until_worker_completion():
    lifecycle = OcxTeardown(owner_thread=threading.get_ident(), enabled=True)
    control = Control()
    assert not lifecycle.release(control, writer_stopped=False)
    assert control.calls == 0 and lifecycle.state == "deferred"
    assert lifecycle.release(control, writer_stopped=True)
    assert control.calls == 1


def test_reentry_during_clear_does_not_repeat():
    lifecycle = OcxTeardown(owner_thread=threading.get_ident(), enabled=True)
    seen = []
    control = Control(lambda: seen.append(lifecycle.release(control, writer_stopped=True)))
    assert lifecycle.release(control, writer_stopped=True)
    assert seen == [False] and control.calls == 1


def test_wrong_thread_rejected_before_any_change():
    lifecycle = OcxTeardown(owner_thread=threading.get_ident(), enabled=True)
    control = Control()
    errors = []
    def other():
        try:
            lifecycle.release(control, writer_stopped=True)
        except RuntimeError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=other)
    thread.start()
    thread.join(2)
    assert not thread.is_alive() and errors
    assert lifecycle.state == "not_attempted" and control.calls == 0


@pytest.mark.parametrize("value", [1, 0, None, "yes"])
def test_invalid_enable_flag(value):
    with pytest.raises(ValueError):
        OcxTeardown(owner_thread=threading.get_ident(), enabled=value)


@pytest.mark.parametrize("value", [1, None, "closed"])
def test_invalid_worker_proof(value):
    lifecycle = OcxTeardown(owner_thread=threading.get_ident(), enabled=True)
    with pytest.raises(ValueError):
        lifecycle.release(Control(), writer_stopped=value)
    assert lifecycle.state == "not_attempted"


def test_live_drain_then_clear_then_quit_with_diagnostics_open(live, tmp_path):
    logger, _, _ = live
    handler = Handler()
    seen = []
    with CaptureDiagnostics(tmp_path / "diag", handler=handler) as diagnostics:
        logger.diagnostics = diagnostics
        logger._on_login(0)
        logger._on_receive_real_data("005930", "주식체결", "")
        def clear_hook():
            state = logger.raw_capture.queue.snapshot()
            assert state["writer_closed"] and state["pending_callbacks"] == 0
            assert logger.raw_capture.queue.wait(0)
            assert handler.enabled and not diagnostics.file.closed
            assert logger.app.quits == 0
            before = state["accepted_callbacks"]
            logger._on_receive_real_data("005930", "주식체결", "")
            logger._on_login(0)
            logger._poll_control()
            logger._shutdown("reentered clear")
            assert logger.raw_capture.queue.snapshot()["accepted_callbacks"] == before
            seen.append("clear")
        control = enabled(logger, hook=clear_hook)
        logger._shutdown("fixture")
        path = diagnostics.path
        assert handler.enabled and logger.app.quits == 1 and control.calls == 1
        logger._shutdown("duplicate")
        assert control.calls == 1
    names = [item["phase"] for item in phases(path)]
    assert names.index("unregister_returned") < names.index("storage_finish_returned")
    assert names.index("storage_finish_returned") < names.index("ocx_clear_enter")
    assert names.index("ocx_clear_returned") < names.index("qt_quit_enter")
    assert names.index("qt_quit_returned") < names.index("diagnostics_closing")
    assert seen == ["clear"] and not handler.enabled


def test_clear_failure_does_not_relabel_stored_data_or_hide_exit_failure(live):
    logger, _, messages = live
    logger._on_login(0)
    logger._on_receive_real_data("005930", "주식체결", "")
    control = enabled(logger, fail=True)
    logger._shutdown("fixture")
    assert logger.exit_code == 2 and logger.app.quits == 1
    assert logger._ocx_teardown.state == "failed" and control.calls == 1
    state = logger.raw_capture.queue.snapshot()
    assert state["writer_closed"] and state["state"] == "closed"
    assert state["data_quality"] == "unverified"
    assert any("저장 완료" in message for message in messages)


def test_log_close_failure_still_requests_qt_quit(live):
    logger, _, _ = live
    logger._on_login(0)
    logger._on_receive_real_data("005930", "주식체결", "")
    control = enabled(logger)
    logger.log.close = lambda: (_ for _ in ()).throw(OSError("synthetic log close failure"))
    logger._shutdown("fixture")
    assert logger.app.quits == 1 and control.calls == 1
    assert logger.exit_code == 2
    state = logger.raw_capture.queue.snapshot()
    assert state["writer_closed"] and state["state"] == "closed"


def test_unfinished_worker_prevents_native_clear(live, monkeypatch):
    logger, _, _ = live
    logger._on_login(0)
    control = enabled(logger)
    # 정상 종료를 마친 뒤에도 단순 closed 문자열만 믿지 않는 경계를 재검증한다.
    logger._shutdown("drain")
    assert control.calls == 1
    control = enabled(logger)
    with monkeypatch.context() as patch:
        patch.setattr(logger.raw_capture.queue, "wait", lambda timeout: False)
        assert not logger._release_ocx_if_stopped()
    assert control.calls == 0 and logger._ocx_teardown.state == "deferred"
    assert logger.exit_code == 0  # 보류는 teardown 실패가 아니다.
    assert logger._release_ocx_if_stopped()
    assert control.calls == 1 and logger.exit_code == 0


def test_active_collector_cannot_be_cleared(live):
    logger, _, _ = live
    logger._on_login(0)
    control = enabled(logger)
    assert not logger._release_ocx_if_stopped()
    assert control.calls == 0 and logger._ocx_teardown.state == "not_attempted"
    assert logger.exit_code == 0  # active 차단도 실패 판정이 아니다.
    assert logger.accepting_events and logger.raw_capture.queue.snapshot()["accepting"]


def test_interrupted_input_stays_interrupted_after_clear(live):
    logger, values, _ = live
    logger._on_login(0)
    control = enabled(logger)
    del values[15]
    logger._on_receive_real_data("005930", "주식체결", "")
    logger._poll_control()
    assert logger.exit_code == 2 and control.calls == 1
    assert logger.raw_capture.queue.snapshot()["state"] == "interrupted"


def test_login_failure_without_capture_still_clears(live):
    logger, _, _ = live
    control = enabled(logger)
    logger._on_login(-100)
    assert logger.raw_capture is None and logger.exit_code == 2
    assert control.calls == 1 and logger.app.quits == 1


def test_wrong_thread_shutdown_has_no_side_effects(live):
    logger, _, _ = live
    control = enabled(logger)
    errors = []
    def wrong():
        try:
            logger._shutdown("wrong thread")
        except RuntimeError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=wrong)
    thread.start()
    thread.join(2)
    assert errors and not thread.is_alive()
    assert not logger._shutdown_done and logger.accepting_events
    assert control.calls == 0 and logger.app.quits == 0


def test_legacy_writer_also_finishes_before_clear(collector):
    logger, _, _ = collector
    control = enabled(logger)
    start_writer(logger)
    logger._shutdown("legacy")
    assert not logger.db_thread.is_alive()
    assert logger.writer.pending == 0 and control.calls == 1


def test_signal_reentry_after_shutdown_does_not_write_closed_log(collector):
    logger, _, handlers = collector
    start_writer(logger)
    logger._shutdown("fixture")
    handlers[signal.SIGINT](signal.SIGINT, None)
    assert logger.app.quits == 1


def test_phase_failures_do_not_prevent_native_release(live):
    logger, _, _ = live
    def broken(*args, **kwargs):
        raise OSError("synthetic diagnostics failure")
    logger.diagnostics = SimpleNamespace(record_phase=broken)
    logger._on_login(0)
    control = enabled(logger)
    logger._shutdown("fixture")
    assert control.calls == 1 and logger.exit_code == 0
    assert record_phase(logger.diagnostics, "ignored") is False


def test_phase_budget_is_separate_from_stall_and_stop(tmp_path):
    handler = Handler()
    with CaptureDiagnostics(tmp_path, handler=handler) as diagnostics:
        for n in range(diagnostics.MAX_PHASES):
            assert diagnostics.record_phase("phase", {"n": n})
        assert not diagnostics.record_phase("over")
        assert diagnostics.record_stall(1, {})
        assert diagnostics.record_stop({})
        path = diagnostics.path
    assert len(phases(path)) == CaptureDiagnostics.MAX_PHASES
    assert handler.dumps == 2


@pytest.mark.parametrize("detail", [{"too_large": "x" * 3000}, {"bad": float("nan")}])
def test_invalid_phase_record_disables_only_phase_writes(tmp_path, detail):
    handler = Handler()
    with CaptureDiagnostics(tmp_path, handler=handler) as diagnostics:
        assert not diagnostics.record_phase("bad", detail)
        assert diagnostics.phase_error
        assert diagnostics.record_stop({})
    assert handler.dumps == 1


def test_cli_explicit_teardown_is_opt_in(collector):
    logger, _, _ = collector
    parser = logger.start.__globals__["parse_collector_args"]
    assert parser([])[0].explicit_ocx_teardown is False
    assert parser(["--explicit-ocx-teardown"])[0].explicit_ocx_teardown is True
    with pytest.raises(SystemExit):
        parser(["--explicit-ocx-teardown", "--aftermarket-nxt-codes", "005930_NX"])


@pytest.mark.parametrize("constructor_fails", [False, True])
def test_partial_ocx_initialization_failure_is_preserved(collector, monkeypatch, constructor_fails):
    old, _, _ = collector
    control = Control()
    def failed_connect(fn):
        raise RuntimeError("synthetic signal binding failure")
    control.OnEventConnect = SimpleNamespace(connect=lambda fn: None)
    control.OnReceiveRealData = SimpleNamespace(connect=failed_connect)
    def factory(*args):
        if constructor_fails:
            raise RuntimeError("synthetic constructor failure")
        return control
    monkeypatch.setitem(old.__init__.__globals__, "QAxWidget", factory)
    with pytest.raises(SystemExit) as caught:
        type(old)(code_revision="fixture", explicit_ocx_teardown=True)
    assert caught.value.code == 1
    assert control.calls == (0 if constructor_fails else 1)


@pytest.mark.parametrize("raises", [False, True])
def test_event_loop_markers_distinguish_return_from_exception(live, tmp_path, raises):
    logger, _, _ = live
    control = enabled(logger)
    def loop():
        if raises:
            raise RuntimeError("synthetic event loop failure")
        return 0
    logger.app.exec_ = loop
    with CaptureDiagnostics(tmp_path / "loop_diag", handler=Handler()) as diagnostics:
        logger.diagnostics = diagnostics
        if raises:
            with pytest.raises(RuntimeError, match="event loop failure"):
                logger.start()
        else:
            logger.start()
        path = diagnostics.path
    names = [row["phase"] for row in phases(path)]
    assert ("event_loop_returned" in names) is not raises
    assert "event_loop_unwinding" in names
    assert names.index("ocx_clear_returned") < names.index("diagnostics_closing")
    assert control.calls == 1


def test_exception_cleanup_gates_reentry_before_clear(live):
    logger, _, _ = live
    logger._on_login(0)
    logger._on_receive_real_data("005930", "주식체결", "")
    def reenter():
        assert logger._shutdown_done and not logger.accepting_events
        assert logger.raw_capture.queue.wait(0)
        before = logger.raw_capture.queue.snapshot()["accepted_callbacks"]
        logger._on_receive_real_data("005930", "주식체결", "")
        logger._poll_control()
        assert logger.raw_capture.queue.snapshot()["accepted_callbacks"] == before
    control = enabled(logger, hook=reenter)
    logger._finish_process_resources("synthetic unexpected exit")
    assert control.calls == 1
    assert logger.raw_capture.queue.snapshot()["state"] == "interrupted"
    logger._finish_process_resources("repeat")
    assert control.calls == 1


def test_transition_teardown_rejected_before_qt_construction(collector, monkeypatch):
    old, _, _ = collector
    def forbidden(*args):
        raise AssertionError("Qt must not be constructed")
    monkeypatch.setitem(old.__init__.__globals__, "QApplication", forbidden)
    with pytest.raises(ValueError, match="session transitions"):
        type(old)(code_revision="fixture", explicit_ocx_teardown=True, aftermarket_plan=object())
