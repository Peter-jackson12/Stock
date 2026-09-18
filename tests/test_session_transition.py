"""tests/test_session_transition.py — 정규장에서 애프터마켓으로의 저장 세션 전환

순서가 곧 안전장치다. 정규장 저장이 닫혔다고 확인된 뒤에만 다음 세션을 연다.
어느 단계든 실패하면 다음 단계로 넘어가지 않고 실패 위치를 남긴다.

여기서 가장 중요한 확인은 **실패가 전파되지 않는가** 다. 2단계에서 마무리가 확인되지
않았는데 3단계가 실행되면, 닫혔는지 모르는 정규장 파일 옆에 새 파일이 생긴다.

실행:
    uv run pytest tests/test_session_transition.py -v
"""
from __future__ import annotations

import sys
import json
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.kiwoom.session_transition import (  # noqa: E402
    STEP_CALLBACK_CAPACITY,
    STEP_FINALIZE,
    STEP_OPEN,
    STEP_SUBSCRIBE,
    STEP_UNSUBSCRIBE,
    STEPS,
    SessionTransition,
    write_transition_record,
)


class Clock:
    def __init__(self, start=1000.0): self.now = start
    def __call__(self): self.now += 0.5; return self.now


def _transition(*, fail_at=None, finalize_result=True, clock=None, calls=None):
    calls = [] if calls is None else calls
    def step(name, result=None):
        def run():
            calls.append(name)
            if fail_at == name:
                raise RuntimeError(f"{name} 실패")
            return result
        return run
    return SessionTransition(
        unsubscribe_regular=step(STEP_UNSUBSCRIBE),
        finalize_regular=step(STEP_FINALIZE, finalize_result),
        open_aftermarket=step(STEP_OPEN),
        subscribe_nxt=step(STEP_SUBSCRIBE),
        clock=clock or Clock()), calls


# ── 정상 전환 ───────────────────────────────────────────────────────────────

def test_네_단계를_순서대로_실행한다():
    transition, calls = _transition()
    record = transition.run()
    assert calls == list(STEPS)
    assert record.succeeded and record.failed_step is None
    assert record.steps_completed == STEPS
    assert record.elapsed_sec > 0


def test_전환_시간과_수신_공백을_기록한다():
    transition, _ = _transition()
    record = transition.run(last_event_before=100.0)
    transition.note_first_event_after(at=140.0)
    assert record.reception_gap_sec == 40.0
    assert "무누락을 보장하지 않는다" in record.describe()["note"]


def test_수신_공백은_양쪽이_있어야_계산된다():
    transition, _ = _transition()
    record = transition.run()                    # 이전 수신 없음
    assert record.reception_gap_sec is None


# ── 단계별 실패가 전파되지 않는다 ───────────────────────────────────────────

def test_구독_해제_실패면_아무것도_더_하지_않는다():
    transition, calls = _transition(fail_at=STEP_UNSUBSCRIBE)
    record = transition.run()
    assert calls == [STEP_UNSUBSCRIBE]
    assert record.failed_step == STEP_UNSUBSCRIBE and not record.succeeded
    assert record.steps_completed == ()


def test_마무리_실패면_새_세션을_열지_않는다():
    transition, calls = _transition(fail_at=STEP_FINALIZE)
    record = transition.run()
    assert STEP_OPEN not in calls and STEP_SUBSCRIBE not in calls
    assert record.failed_step == STEP_FINALIZE


def test_마무리가_확인되지_않으면_예외가_없어도_실패다():
    # 닫혔는지 모르는 채로 다음 파일을 열면 안 된다. 거짓 반환도 실패로 본다.
    transition, calls = _transition(finalize_result=False)
    record = transition.run()
    assert record.failed_step == STEP_FINALIZE
    assert "마무리가 확인되지 않았다" in record.error
    assert STEP_OPEN not in calls
    assert record.steps_completed == (STEP_UNSUBSCRIBE,)


def test_세션_생성_실패면_구독하지_않는다():
    transition, calls = _transition(fail_at=STEP_OPEN)
    record = transition.run()
    assert STEP_SUBSCRIBE not in calls
    assert record.failed_step == STEP_OPEN
    assert record.steps_completed == (STEP_UNSUBSCRIBE, STEP_FINALIZE)


def test_구독_실패는_마지막_단계에서_기록된다():
    transition, calls = _transition(fail_at=STEP_SUBSCRIBE)
    record = transition.run()
    assert calls == list(STEPS)
    assert record.failed_step == STEP_SUBSCRIBE
    assert not record.succeeded
    assert record.steps_completed == (STEP_UNSUBSCRIBE, STEP_FINALIZE, STEP_OPEN)


def test_실패해도_경과_시간과_기록은_남는다():
    transition, _ = _transition(fail_at=STEP_OPEN)
    record = transition.run(last_event_before=100.0)
    payload = record.describe()
    assert payload["failed_step"] == STEP_OPEN and payload["succeeded"] is False
    assert payload["elapsed_sec"] is not None
    assert payload["schema"] == "session_transition_v2"


# ── 식별자·리비전·단계별 시각 ────────────────────────────────────────────────

def test_전환_식별자와_세션_리비전이_기록된다():
    transition, _ = _transition()
    record = transition.run(prev_session_id="regular-1", code_revision="deadbeef",
                            plan_revision={"mode": "nxt"})
    assert record.transition_id and isinstance(record.transition_id, str)
    assert record.prev_session_id == "regular-1"
    assert record.code_revision == "deadbeef"
    assert record.plan_revision == {"mode": "nxt"}
    assert record.next_session_id is None            # 호출부가 이후 채운다
    payload = record.describe()
    assert payload["transition_id"] == record.transition_id


def test_전환_식별자는_매번_다르다():
    a, _ = _transition()
    b, _ = _transition()
    assert a.run().transition_id != b.run().transition_id


def test_단계별_시작_완료_시각과_UTC_KST가_남는다():
    transition, _ = _transition()
    record = transition.run()
    assert len(record.step_timing) == len(STEPS)
    for entry in record.step_timing:
        assert entry.started_at is not None and entry.completed_at is not None
        assert entry.started_at_utc and entry.completed_at_utc
        assert entry.started_at_kst and entry.completed_at_kst
        assert entry.completed_at >= entry.started_at
    assert record.started_at_utc and record.started_at_kst
    assert record.finished_at_utc and record.finished_at_kst


def test_실패한_단계의_시각과_오류가_단계별_기록에도_남는다():
    transition, _ = _transition(fail_at=STEP_OPEN)
    record = transition.run()
    timing_by_step = {t.step: t for t in record.step_timing}
    assert STEP_OPEN in timing_by_step
    assert timing_by_step[STEP_OPEN].error is not None
    assert STEP_SUBSCRIBE not in timing_by_step        # 실행되지 않은 단계는 없다


def test_성공한_단계의_반환값이_기록된다():
    transition, _ = _transition(finalize_result=True)
    record = transition.run()
    timing_by_step = {t.step: t for t in record.step_timing}
    assert timing_by_step[STEP_FINALIZE].outcome == repr(True)


# ── 전환 중 콜백 진단 보존 한도 ──────────────────────────────────────────────

def test_콜백_보존_한도를_넘으면_전환을_실패로_남긴다():
    calls = []
    def finalize():
        calls.append(STEP_FINALIZE)
        transition.accept_callback(code="005930")
        transition.accept_callback(code="000660")     # 한도(1) 초과
        return True
    transition = SessionTransition(
        unsubscribe_regular=lambda: calls.append(STEP_UNSUBSCRIBE),
        finalize_regular=finalize,
        open_aftermarket=lambda: calls.append(STEP_OPEN),
        subscribe_nxt=lambda: calls.append(STEP_SUBSCRIBE),
        clock=Clock(), max_pending_callbacks=1)
    record = transition.run()
    assert STEP_OPEN not in calls and STEP_SUBSCRIBE not in calls   # 한도 초과 뒤 진행하지 않는다
    assert record.failed_step == STEP_CALLBACK_CAPACITY
    assert record.callbacks_dropped == 1
    assert len(record.callbacks_during) == 1                        # 첫 건은 보존됐다
    assert not record.succeeded
    payload = record.describe()
    assert payload["callbacks_dropped"] == 1 and payload["callback_capacity"] == 1


def test_콜백에_전환_단계와_구독_문맥이_남는다():
    calls = []
    def finalize():
        transition.accept_callback(code="005930", real_type="주식체결", fids={"10": "70000"},
                                   subscription_context={"aftermarket_plan_codes": ["005930_NX"]})
        return True
    transition = SessionTransition(
        unsubscribe_regular=lambda: None, finalize_regular=finalize,
        open_aftermarket=lambda: None, subscribe_nxt=lambda: None, clock=Clock())
    record = transition.run()
    preserved = record.describe()["callbacks_preserved"][0]
    assert preserved["transition_step"] == STEP_FINALIZE
    assert preserved["subscription_context"] == {"aftermarket_plan_codes": ["005930_NX"]}
    assert preserved["fids"] == {"10": "70000"}


# ── 전환 중 콜백 ────────────────────────────────────────────────────────────

def test_전환_중_도착한_콜백을_버리지_않는다():
    calls = []
    clock = Clock()
    captured = {}
    def finalize():
        calls.append(STEP_FINALIZE)
        # 전환이 진행되는 동안 콜백이 도착한다
        captured["accepted"] = transition.accept_callback(
            code="005930", real_type="주식체결", fids={"10": "70000"})
        return True
    transition = SessionTransition(
        unsubscribe_regular=lambda: calls.append(STEP_UNSUBSCRIBE),
        finalize_regular=finalize,
        open_aftermarket=lambda: calls.append(STEP_OPEN),
        subscribe_nxt=lambda: calls.append(STEP_SUBSCRIBE),
        clock=clock)
    record = transition.run()
    assert captured["accepted"] is True
    assert len(record.callbacks_during) == 1
    preserved = record.describe()["callbacks_preserved"][0]
    assert preserved["code"] == "005930"                 # 원문이 남는다
    assert preserved["fids"] == {"10": "70000"}
    assert record.describe()["callbacks_during_transition"] == 1


def test_전환_전후의_콜백은_전환_기록이_받지_않는다():
    transition, _ = _transition()
    assert transition.accept_callback(code="005930") is False    # 시작 전
    transition.run()
    assert transition.accept_callback(code="005930") is False    # 끝난 뒤


# ── 재사용 금지 ─────────────────────────────────────────────────────────────

def test_전환은_한_번만_실행한다():
    transition, _ = _transition()
    transition.run()
    try:
        transition.run()
    except ValueError as exc:
        assert "한 번만" in str(exc)
        return
    raise AssertionError("두 번째 실행을 허용했다")


def test_각_단계는_디스크의_시작_기록과_이전_완료를_확인한_뒤_실행한다(tmp_path):
    path = tmp_path / "transition.json"
    calls, saved = [], []
    def persist():
        payload = transition.record.describe()
        write_transition_record(path, payload)
        saved.append(json.loads(path.read_text(encoding="utf-8")))
    def step(name):
        def run():
            disk = json.loads(path.read_text(encoding="utf-8"))
            assert disk["phase"] == "step_started"
            assert disk["steps_completed"] == calls
            assert disk["step_timing"][-1]["step"] == name
            assert disk["step_timing"][-1]["completed_at"] is None
            assert not disk["succeeded"]
            calls.append(name)
            return True
        return run
    transition = SessionTransition(**dict(zip(STEPS, map(step, STEPS))), persist=persist)
    assert transition.run().succeeded
    assert [p["phase"] for p in saved] == ["started"] + [
        "step_started", "step_completed"] * 4 + ["completed"]
    assert saved[-1]["succeeded"]


@pytest.mark.parametrize("failure_write", range(1, 11))
def test_모든_필수_기록_실패에서_후속_단계가_실행되지_않는다(failure_write):
    calls, writes = [], []
    def persist():
        writes.append(transition.record.phase)
        if len(writes) >= failure_write:
            raise OSError("기록 실패")
    def step(name):
        return lambda: calls.append(name) or True
    transition = SessionTransition(**dict(zip(STEPS, map(step, STEPS))), persist=persist)
    record = transition.run()
    assert calls == list(STEPS[:min(4, (failure_write - 1) // 2)])
    assert record.failed_step == "diagnostic_write"
    assert not record.succeeded and not transition.in_progress
    assert record.recording_error == "OSError: 기록 실패"


@pytest.mark.parametrize("stage", STEPS)
@pytest.mark.parametrize("fault", ["cancel", "timeout", "exception", "interrupt"])
def test_각_단계의_취소_시간초과_예외는_다음_단계를_막고_실패를_저장한다(stage, fault):
    now, cancelled, calls, saved = [10.0], [False], [], []
    def step(name):
        def run():
            calls.append(name)
            if name == stage:
                if fault == "cancel":
                    cancelled[0] = True
                elif fault == "timeout":
                    now[0] += 30
                elif fault == "exception":
                    raise OSError("단계 오류")
                else:
                    raise KeyboardInterrupt()
            return True
        return run
    transition = SessionTransition(**dict(zip(STEPS, map(step, STEPS))),
        clock=lambda: now[0], cancelled=lambda: cancelled[0],
        persist=lambda: saved.append(transition.record.describe()))
    record = transition.run()
    assert calls == list(STEPS[:STEPS.index(stage) + 1])
    assert not record.succeeded and record.failed_step == stage
    assert not transition.in_progress
    assert saved[-1]["phase"] == "failed" and saved[-1]["error"]


def test_콜백_진단_저장_실패도_현재_단계_이후를_막는다():
    calls = []
    def finalize():
        assert not transition.accept_callback(code="005930")
        return True
    def persist():
        if transition.record.callbacks_during:
            raise OSError("콜백 진단 실패")
    transition = SessionTransition(unsubscribe_regular=lambda: None,
        finalize_regular=finalize, open_aftermarket=lambda: calls.append("open"),
        subscribe_nxt=lambda: calls.append("subscribe"), persist=persist)
    record = transition.run()
    assert not calls and not record.succeeded
    assert record.failed_step == "diagnostic_write"
    assert record.callbacks_during[0]["code"] == "005930"


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_원자적_교체_실패는_마지막_성공_기록을_보존한다(tmp_path, monkeypatch, operation):
    import os
    path = tmp_path / "transition.json"
    write_transition_record(path, {"phase": "step_started"})
    def fail(*args):
        raise OSError("교체 실패")
    monkeypatch.setattr(os if operation == "fsync" else Path, operation, fail)
    with pytest.raises(OSError, match="교체 실패"):
        write_transition_record(path, {"phase": "step_completed"})
    assert json.loads(path.read_text()) == {"phase": "step_started"}
    assert not path.with_suffix(".tmp").exists()


def test_벽시계_역행과_미수신은_단조_공백을_왜곡하지_않는다():
    transition, _ = _transition()
    record = transition.run(last_event_before=10, last_event_before_utc="2026-09-18T01:00:00+00:00")
    assert record.reception_gap_sec is None
    transition.note_first_event_after(at=12.5, at_utc="2026-09-18T00:00:00+00:00")
    assert record.reception_gap_sec == 2.5
    assert record.last_event_before_kst == "2026-09-18T10:00:00+09:00"
    assert record.first_event_after_kst == "2026-09-18T09:00:00+09:00"
    assert not transition.note_first_event_after(at=20)
    record.first_event_after = 9
    assert record.reception_gap_sec is None  # 잘못 주입한 역행 단조 값도 음수로 인증하지 않는다.
    no_before, _ = _transition()
    missing = no_before.run()
    no_before.note_first_event_after(at=20)
    assert missing.reception_gap_sec is None
