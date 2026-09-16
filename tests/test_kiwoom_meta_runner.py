"""실제 Qt/로그인 없이 실행기의 이벤트 배선과 종료 코드를 검증한다."""
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from collector.kiwoom import run_meta_batch as runner
from collector.kiwoom.meta_batch import BatchController
from collector.daily_snapshot import read_all_snapshots
from scripts.kiwoom_metadata import import_results
from tests.test_kiwoom_stock_meta import RAW
from tests.test_kiwoom_meta_batch import JOB, Clock


class Signal:
    def connect(self, fn):
        self.fn = fn
    def emit(self, *args):
        self.fn(*args)


class FakeQt:
    def __init__(self, monkeypatch, tmp_path, *, server="1", login_error=0,
                 lookup_error=False, data_error=False, disconnect=False, window_close=False):
        self.clock = Clock()
        self.server, self.login_error = server, login_error
        self.lookup_error, self.data_error = lookup_error, data_error
        self.disconnect, self.window_close = disconnect, window_close
        self.calls = []
        self.pending = None
        self.exit_code = None
        self.timer_active = False
        env = self

        class App:
            def __init__(self, _):
                pass
            def exit(self, code):
                env.exit_code = code
            def exec_(self):
                env.ocx.OnEventConnect.emit(env.login_error)
                for _ in range(100):
                    env.timer.timeout.emit()
                    if env.exit_code is not None:
                        return env.exit_code
                    if env.window_close:
                        return 0
                    if env.pending:
                        rq, tr, _, screen = env.pending
                        env.pending = None
                        if env.disconnect:
                            env.timer.timeout.emit()
                        else:
                            env.ocx.OnReceiveTrData.emit(screen, rq, tr, "주식기본정보", "0", 0, "", "", "")
                    if env.exit_code is not None:
                        return env.exit_code
                    env.clock.t += 4
                raise AssertionError("runner did not terminate")

        class Timer:
            def __init__(self):
                self.timeout = Signal()
                env.timer = self
            def start(self, _):
                env.timer_active = True
            def stop(self):
                env.timer_active = False

        class OCX:
            def __init__(self, _):
                self.OnEventConnect = Signal()
                self.OnReceiveTrData = Signal()
                self.OnReceiveMsg = Signal()
                env.ocx = self
            def isNull(self):
                return False
            def dynamicCall(self, method, *args):
                env.calls.append((method, args))
                if method.startswith("CommConnect"):
                    return 0
                if method.startswith("KOA_Functions"):
                    if env.lookup_error:
                        raise RuntimeError("lookup failure")
                    return env.server
                if method.startswith("GetConnectState"):
                    requested = any(m.startswith("CommRqData") for m, _ in env.calls)
                    return 0 if env.disconnect and requested else 1
                if method.startswith("SetInputValue"):
                    env.code = args[1]
                    return None
                if method.startswith("CommRqData"):
                    env.pending = args
                    return 0
                if method.startswith("GetCommData"):
                    if env.data_error:
                        raise RuntimeError("read failure")
                    assert args[:3] == ("opt10001", "주식기본정보", 0)
                    return {**RAW, "종목코드": env.code}.get(args[3], "")
                raise AssertionError(f"unexpected API call: {method}")

        monkeypatch.setitem(sys.modules, "PyQt5.QtWidgets", SimpleNamespace(QApplication=App))
        monkeypatch.setitem(sys.modules, "PyQt5.QAxContainer", SimpleNamespace(QAxWidget=OCX))
        monkeypatch.setitem(sys.modules, "PyQt5.QtCore", SimpleNamespace(QTimer=Timer))
        monkeypatch.setattr(runner, "STATE", tmp_path / "state.db")
        monkeypatch.setattr(runner, "BatchController", lambda store, send: BatchController(store, send, clock=self.clock))

    def rows(self):
        with sqlite3.connect(runner.STATE) as conn:
            return conn.execute("SELECT status, observation, error FROM attempts ORDER BY id").fetchall()


def test_event_path_to_pilot_and_resume_without_new_tr(monkeypatch, tmp_path):
    env = FakeQt(monkeypatch, tmp_path)
    assert runner.run(JOB) == 0
    assert not env.timer_active
    assert [r[0] for r in env.rows()] == ["complete", "complete"]
    count, output = import_results(JOB, state=runner.STATE, output=tmp_path / "pilot")
    assert count == 2
    assert set(read_all_snapshots(output).code) == set(JOB["codes"])
    env = FakeQt(monkeypatch, tmp_path)
    assert runner.run(JOB) == 0
    assert not any(m.startswith("CommRqData") for m, _ in env.calls)


@pytest.mark.parametrize("options", [{"login_error": -100}, {"server": "0"}, {"server": ""}, {"lookup_error": True}])
def test_login_failure_or_unknown_server_stops_before_tr(monkeypatch, tmp_path, options):
    env = FakeQt(monkeypatch, tmp_path, **options)
    assert runner.run(JOB) == 2
    assert not any(m.startswith("CommRqData") for m, _ in env.calls)
    assert not env.timer_active


@pytest.mark.parametrize("options", [{"disconnect": True}, {"data_error": True}, {"window_close": True}])
def test_interruption_cannot_report_success(monkeypatch, tmp_path, options):
    env = FakeQt(monkeypatch, tmp_path, **options)
    assert runner.run(JOB) == 2
    assert env.rows()[0][0] == "failed"
    assert not env.timer_active
