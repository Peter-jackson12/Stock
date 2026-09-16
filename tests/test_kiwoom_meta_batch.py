from datetime import datetime
import json

import pytest

from collector.kiwoom.meta_batch import BatchController, BatchStore, validate_job
from collector.kiwoom.stock_meta import KST
from collector.daily_snapshot import read_all_snapshots
from scripts.kiwoom_metadata import import_results
from tests.test_kiwoom_stock_meta import RAW


JOB = {"version": 1, "date": "20260916", "server": "mock", "shares_multiplier": 1000,
       "codes": ["005930", "000660"]}
START = datetime(2026, 9, 16, 9, tzinfo=KST).timestamp()


class Clock:
    def __init__(self):
        self.t = START
    def __call__(self):
        return self.t


def test_complete_codes_are_skipped_after_restart_and_rate_gap_persists(tmp_path):
    path = tmp_path / "state.db"
    clock = Clock()
    sent = []
    store = BatchStore(path, JOB)
    controller = BatchController(store, lambda rq, c: sent.append((rq, c)) or 0, clock=clock)
    controller.pump()
    controller.receive(sent[-1][0], RAW)
    store.close()

    store = BatchStore(path, JOB)
    controller = BatchController(store, lambda rq, c: sent.append((rq, c)) or 0, clock=clock)
    controller.pump()
    assert len(sent) == 1  # 재시작해도 4초 간격
    clock.t += 4
    controller.pump()
    assert sent[-1][1] == "000660"
    controller.receive(sent[-1][0], {**RAW, "종목코드": "000660"})
    controller.pump()
    assert controller.stopped == "complete"
    store.close()

    count, output = import_results(JOB, state=path, output=tmp_path / "pilot")
    assert count == 2
    assert set(read_all_snapshots(output).code) == {"005930", "000660"}
    before = (output / "snapshots/20260916.csv").read_bytes()
    import_results(JOB, state=path, output=output)
    assert (output / "snapshots/20260916.csv").read_bytes() == before
    assert not (output / "open.csv").exists()


def test_late_response_does_not_populate_new_request(tmp_path):
    clock = Clock()
    sent = []
    store = BatchStore(tmp_path / "state.db", JOB)
    controller = BatchController(store, lambda rq, c: sent.append((rq, c)) or 0, clock=clock)
    controller.pump()
    old_rq = sent[0][0]
    clock.t += 21
    controller.pump()
    assert sent[-1][0] != old_rq
    assert not controller.receive(old_rq, RAW)
    assert store.complete_codes() == set()
    controller.receive(sent[-1][0], RAW)
    assert store.complete_codes() == {"005930"}
    store.close()


def test_missing_response_is_retained_and_attempts_are_bounded(tmp_path):
    clock = Clock()
    sent = []
    store = BatchStore(tmp_path / "state.db", {**JOB, "codes": ["005930"]})
    controller = BatchController(store, lambda rq, c: sent.append(rq) or 0, clock=clock)
    for _ in range(2):
        controller.pump()
        controller.receive(sent[-1], {**RAW, "유통비율": ""})
        clock.t += 4
    controller.pump()
    assert controller.stopped == "incomplete"
    assert store.complete_codes() == set()
    rows = store.conn.execute("SELECT status,observation FROM attempts").fetchall()
    assert len(rows) == 2
    assert all(r[0] == "missing" and json.loads(r[1])["snapshot"]["float"] is None for r in rows)
    store.close()


def test_request_rejection_stops_batch_without_retry_storm(tmp_path):
    store = BatchStore(tmp_path / "state.db", JOB)
    controller = BatchController(store, lambda *_: -200, clock=Clock())
    controller.pump()
    for _ in range(10):
        controller.pump()
    assert controller.stopped == "request_rejected: -200"
    assert store.conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
    store.close()


def test_crash_after_request_is_retryable(tmp_path):
    path = tmp_path / "state.db"
    clock = Clock()
    store = BatchStore(path, JOB)
    BatchController(store, lambda *_: 0, clock=clock).pump()
    store.close()  # finish 없이 프로세스 종료된 상태
    clock.t += 4
    store = BatchStore(path, JOB)
    sent = []
    controller = BatchController(store, lambda rq, c: sent.append(c) or 0, clock=clock)
    controller.pump()
    assert sent == ["005930"]
    assert store.conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 2
    store.close()


def test_date_rollover_does_not_backdate_response(tmp_path):
    clock = Clock()
    sent = []
    store = BatchStore(tmp_path / "state.db", JOB)
    controller = BatchController(store, lambda rq, c: sent.append(rq) or 0, clock=clock)
    controller.pump()
    clock.t += 86400
    controller.receive(sent[-1], RAW)
    assert store.complete_codes() == set()
    assert store.conn.execute("SELECT error FROM attempts").fetchone()[0] in ("code_or_date_mismatch", "late_response")
    controller.pump()
    assert controller.stopped == "date_changed"
    store.close()


def test_wrong_code_cannot_complete_requested_code(tmp_path):
    sent = []
    store = BatchStore(tmp_path / "state.db", JOB)
    controller = BatchController(store, lambda rq, c: sent.append(rq) or 0, clock=Clock())
    controller.pump()
    controller.receive(sent[-1], {**RAW, "종목코드": "000660"})
    assert store.complete_codes() == set()
    store.close()


def test_mock_and_live_jobs_have_separate_completion(tmp_path):
    path = tmp_path / "state.db"
    store = BatchStore(path, JOB)
    a = store.begin("005930", START)
    store.finish(a, "complete")
    store.close()
    store = BatchStore(path, {**JOB, "server": "live"})
    assert not store.complete_codes()
    assert store.last_request() == START  # 호출 간격은 공통 상태 DB 전체에서 유지
    store.close()


def test_another_process_lock_is_rejected(tmp_path, monkeypatch):
    from collector.kiwoom import run_meta_batch as runner
    monkeypatch.setattr(runner, "STATE", tmp_path / "state.db")
    with runner.ProcessLock():
        with pytest.raises(RuntimeError, match="running"):
            with runner.ProcessLock():
                pass
    with runner.ProcessLock():
        pass


@pytest.mark.parametrize("changes", [{"codes": []}, {"codes": ["005930"] * 2},
                                     {"server": "unknown"}, {"shares_multiplier": None}])
def test_invalid_plan_is_rejected(changes):
    with pytest.raises(ValueError):
        validate_job({**JOB, **changes})


def test_import_rejects_server_mismatch(tmp_path):
    path = tmp_path / "state.db"
    store = BatchStore(path, JOB)
    attempt = store.begin("005930", START)
    store.finish(attempt, "complete", {"server": "live", "received_at": "2026-09-16T09:00:00+09:00", "raw": RAW})
    store.close()
    with pytest.raises(ValueError, match="server/date"):
        import_results(JOB, state=path, output=tmp_path / "pilot")
    assert not (tmp_path / "pilot").exists()


def test_prepare_reuses_core_universe():
    from scripts.kiwoom_metadata import prepare
    result = prepare("blue_chips", "mock", 1000, 2)
    assert result["universe"]["name"] == "blue_chips"
    assert result["codes"] == ["000270", "000660"]
    assert result["resolved_total"] == 3


def test_late_response_before_timer_tick_cannot_complete(tmp_path):
    clock = Clock()
    sent = []
    store = BatchStore(tmp_path / "state.db", JOB)
    controller = BatchController(store, lambda rq, c: sent.append(rq) or 0, clock=clock)
    controller.pump()
    clock.t += 20.1
    controller.receive(sent[-1], RAW)  # 타이머의 pump가 아직 호출되지 않은 상황
    assert store.complete_codes() == set()
    assert store.conn.execute("SELECT error FROM attempts").fetchone()[0] == "late_response"
    controller.pump()
    assert len(sent) == 2
    store.close()
