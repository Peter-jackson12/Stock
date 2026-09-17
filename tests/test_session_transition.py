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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.kiwoom.session_transition import (  # noqa: E402
    STEP_FINALIZE,
    STEP_OPEN,
    STEP_SUBSCRIBE,
    STEP_UNSUBSCRIBE,
    STEPS,
    SessionTransition,
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
    assert payload["schema"] == "session_transition_v1"


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
