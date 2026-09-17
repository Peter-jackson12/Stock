"""단일 로그인 안에서 정규장 저장을 마치고 애프터마켓 저장으로 넘기는 전환.

순서가 곧 안전장치다. 정규장 저장이 **닫혔다고 확인된 뒤에만** 다음 세션을 연다.

    1. 정규장 구독 해제 · 신규 입력 차단
    2. 기존 큐 저장 완료 · 파일 닫힘 · 마무리 정보 확인
    3. 새 애프터마켓 저장 세션 생성
    4. NXT 구독 등록 · 수신 시작

어느 단계든 실패하면 **다음 단계로 넘어가지 않고** 실패 위치를 기록한다. 2단계가
마무리 정보를 내놓지 못하면 그것도 실패다 — 닫혔는지 모르는 채로 다음 파일을 열지 않는다.
이미 닫힌 정규장 파일은 다시 열거나 고치지 않는다. 이 전환기는 애초에 정규장 저장을
쓰기용으로 들고 있지 않으며, 2단계 이후에는 확인 결과만 읽는다.

전환 중 도착한 콜백은 버리지 않고 별도 진단 기록으로 남긴다. 어느 세션에도 속하지 않는
구간이라 저장 파일에는 들어갈 자리가 없지만, 왔다는 사실과 원문은 남겨야 한다.

전환 시간과 수신 공백을 기록하되 **무누락을 보장하지 않는다.** 해제와 등록 사이에는
반드시 공백이 생기고 그 길이를 약속할 수 없다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time

STEP_UNSUBSCRIBE = "unsubscribe_regular"
STEP_FINALIZE = "finalize_regular"
STEP_OPEN = "open_aftermarket"
STEP_SUBSCRIBE = "subscribe_nxt"
STEPS = (STEP_UNSUBSCRIBE, STEP_FINALIZE, STEP_OPEN, STEP_SUBSCRIBE)


@dataclass
class TransitionRecord:
    started_at: float
    finished_at: float | None = None
    steps_completed: tuple = ()
    failed_step: str | None = None
    error: str | None = None
    callbacks_during: tuple = ()
    last_event_before: float | None = None
    first_event_after: float | None = None

    @property
    def succeeded(self) -> bool:
        return self.failed_step is None and len(self.steps_completed) == len(STEPS)

    @property
    def elapsed_sec(self) -> float | None:
        if self.finished_at is None:
            return None
        return self.finished_at - self.started_at

    @property
    def reception_gap_sec(self) -> float | None:
        """마지막 정규장 수신부터 첫 애프터마켓 수신까지. 둘 중 하나라도 없으면 None."""
        if self.last_event_before is None or self.first_event_after is None:
            return None
        return self.first_event_after - self.last_event_before

    def describe(self):
        return dict(
            schema="session_transition_v1",
            started_at=self.started_at, finished_at=self.finished_at,
            elapsed_sec=self.elapsed_sec,
            steps_completed=list(self.steps_completed),
            failed_step=self.failed_step, error=self.error,
            succeeded=self.succeeded,
            callbacks_during_transition=len(self.callbacks_during),
            callbacks_preserved=[dict(c) for c in self.callbacks_during],
            last_event_before=self.last_event_before,
            first_event_after=self.first_event_after,
            reception_gap_sec=self.reception_gap_sec,
            note="전환 시간과 수신 공백은 기록이며 무누락을 보장하지 않는다")


class SessionTransition:
    """각 단계를 호출 가능한 형태로 받아 순서대로 실행한다.

    finalize_regular 은 정규장 저장이 닫혔는지를 돌려줘야 한다. 참이 아니면 실패로 본다.
    """

    def __init__(self, *, unsubscribe_regular, finalize_regular, open_aftermarket,
                 subscribe_nxt, clock=time.monotonic):
        self._steps = {
            STEP_UNSUBSCRIBE: unsubscribe_regular,
            STEP_FINALIZE: finalize_regular,
            STEP_OPEN: open_aftermarket,
            STEP_SUBSCRIBE: subscribe_nxt,
        }
        self._clock = clock
        self.record: TransitionRecord | None = None
        self._in_progress = False
        self._pending_callbacks = []

    @property
    def in_progress(self) -> bool:
        return self._in_progress

    def accept_callback(self, **payload):
        """전환 중 도착한 콜백을 보존한다. 저장 파일에는 넣지 않는다."""
        if not self._in_progress:
            return False
        self._pending_callbacks.append(dict(payload, at=self._clock()))
        return True

    def run(self, *, last_event_before=None):
        if self.record is not None:
            raise ValueError("전환은 한 번만 실행한다. 새 전환은 새 객체로 한다")
        self._in_progress = True
        record = TransitionRecord(started_at=self._clock(), last_event_before=last_event_before)
        self.record = record
        completed = []
        try:
            for name in STEPS:
                try:
                    outcome = self._steps[name]()
                except Exception as exc:
                    record.failed_step = name
                    record.error = f"{type(exc).__name__}: {exc}"
                    break
                if name == STEP_FINALIZE and not outcome:
                    # 닫혔는지 확인되지 않았다. 다음 파일을 열지 않는다.
                    record.failed_step = name
                    record.error = "정규장 저장 마무리가 확인되지 않았다 (닫힘/마무리 정보 없음)"
                    break
                completed.append(name)
        finally:
            record.steps_completed = tuple(completed)
            record.finished_at = self._clock()
            record.callbacks_during = tuple(self._pending_callbacks)
            self._in_progress = False
        return record

    def note_first_event_after(self, at=None):
        if self.record is not None and self.record.first_event_after is None:
            self.record.first_event_after = self._clock() if at is None else at
