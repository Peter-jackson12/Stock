"""Exercise the real collector's shutdown wiring with Qt/OCX replaced offline."""
import importlib.util
import signal
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_tick_writer import TRADE, QUOTE, counts


class Signal:
    def connect(self, fn):
        self.fn = fn
    def emit(self, *args):
        return self.fn(*args)


@pytest.fixture
def collector(monkeypatch, tmp_path):
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda sig, fn: handlers.update({sig: fn}))
    class App:
        def __init__(self, _):
            self.aboutToQuit = Signal()
            self.quits = 0
        def quit(self):
            self.quits += 1
            self.aboutToQuit.emit()
    class Timer:
        def __init__(self):
            self.timeout = Signal()
        def start(self, _):
            pass
        def stop(self):
            pass
    class OCX:
        def __init__(self, _):
            self.OnEventConnect, self.OnReceiveRealData = Signal(), Signal()
            self.calls = []
        def dynamicCall(self, method, *args):
            self.calls.append((threading.get_ident(), method, args))
            return "1"
    for module, value in {
        "PyQt5": SimpleNamespace(),
        "PyQt5.QtWidgets": SimpleNamespace(QApplication=App),
        "PyQt5.QtCore": SimpleNamespace(QTimer=Timer),
        "PyQt5.QAxContainer": SimpleNamespace(QAxWidget=OCX),
    }.items():
        monkeypatch.setitem(sys.modules, module, value)
    path = Path(__file__).resolve().parents[1] / "collector/kiwoom/kiwoom_universe_logger.py"
    spec = importlib.util.spec_from_file_location("offline_tick_logger", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "RAW_DIR", tmp_path)
    monkeypatch.setattr(module, "LOG_DIR", tmp_path)
    messages = []
    class Log:
        def __init__(self, _):
            self.closed = False
        def emit(self, msg):
            assert not self.closed
            messages.append(msg)
        def emit_all(self, lines):
            for line in lines:
                self.emit(line)
        def close(self):
            self.closed = True
        def end_status_line(self):
            pass
    monkeypatch.setattr(module, "SessionLog", Log)
    logger = module.KiwoomUniverseLogger(storage="raw-v1", code_revision="fixture")
    logger.monitor.start()
    logger.monitor.on_trade()
    logger.monitor.on_quote()
    logger.trade_queue.put(TRADE)
    logger.quote_queue.put(QUOTE)
    yield logger, messages, handlers
    # Clean up even when an assertion failed, without touching the live process.
    logger.writer.stop.set()
    if hasattr(logger, "db_thread"):
        logger.db_thread.join(5)


def start_writer(logger):
    logger.db_thread = threading.Thread(target=logger.writer.run)
    logger.db_thread.start()


def test_ctrl_c_defers_shutdown_until_callback_boundary_and_drains(collector):
    logger, messages, handlers = collector
    start_writer(logger)
    handlers[signal.SIGINT](signal.SIGINT, None)
    assert not logger._shutdown_done and logger.accepting_events
    # An already executing callback can finish enqueueing before the timer polls.
    logger.trade_queue.put(TRADE)
    logger.monitor.on_trade()
    logger._poll_control()
    assert logger._shutdown_done and not logger.accepting_events
    assert not logger.db_thread.is_alive()
    assert counts(logger.db_path) == (2, 1)
    assert logger.writer.pending == 0 and logger.exit_code == 0
    assert any("종료 후 미커밋 : 0 건" in msg for msg in messages)
    before = list(logger.ocx.calls)
    logger._on_receive_real_data("005930", "주식체결", "")
    assert logger.ocx.calls == before and logger.trade_queue.empty()
    logger._shutdown("repeat")
    assert logger.app.quits == 1


def test_storage_error_stops_collection_without_healthy_headline(collector):
    logger, messages, _ = collector
    def fail(*a, **kw):
        raise OSError("disk unavailable")
    logger.writer.connect = fail
    start_writer(logger)
    assert logger.writer.done.wait(5)
    logger._poll_control()
    assert logger.exit_code == 2
    assert logger.writer.pending == 2
    assert any("미커밋 2건" in msg for msg in messages)
    assert any("disk unavailable" in msg for msg in messages)
    assert not any("✅ 정상 종료" in msg for msg in messages)


def test_market_close_request_uses_main_thread_for_unregister(collector, monkeypatch):
    logger, _, _ = collector
    # Module is deliberately not installed globally; inspect method globals.
    from datetime import datetime
    class ClosingTime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 9, 16, 15, 35)
    monkeypatch.setitem(logger._stats_worker.__globals__, "datetime", ClosingTime)
    start_writer(logger)
    logger.stats_thread = threading.Thread(target=logger._stats_worker)
    logger.stats_thread.start()
    logger.stats_thread.join(5)
    assert logger._shutdown_requested == "장 마감 (15:35)"
    assert logger.ocx.calls == []
    logger._poll_control()
    assert logger.ocx.calls[0][0] == threading.get_ident()
    assert counts(logger.db_path) == (1, 1)


def test_qt_quit_drains_and_shutdown_is_idempotent(collector):
    logger, _, _ = collector
    start_writer(logger)
    logger.app.aboutToQuit.emit()
    assert logger.writer.pending == 0
    assert counts(logger.db_path) == (1, 1)
    assert logger.app.quits == 1


def test_disk_drain_time_does_not_become_a_feed_gap(collector):
    import sqlite3
    from tests.test_tick_writer import ConnectionProxy
    logger, messages, _ = collector
    clock = [logger.monitor.last_event_ts]
    logger.monitor._clock = lambda: clock[0]
    ended = clock[0]
    class SlowCommit(ConnectionProxy):
        def commit(self):
            if self.commits == 1:
                assert logger.writer.stop.wait(5)
                clock[0] += 600
            return super().commit()
    logger.writer.connect = lambda *a, **kw: SlowCommit(sqlite3.connect(*a, **kw))
    start_writer(logger)
    logger._shutdown("test drain")
    assert logger.monitor.ended_at == ended
    assert clock[0] == ended + 600
    assert any("✅ 정상 종료" in msg for msg in messages)
    assert counts(logger.db_path) == (1, 1)
