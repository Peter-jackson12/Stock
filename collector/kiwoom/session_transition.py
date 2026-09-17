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
구간이라 저장 파일에는 들어갈 자리가 없지만, 왔다는 사실과 원문은 남겨야 한다. 진단
보존에도 상한이 있다 — 한도를 넘으면 누락을 숨기지 않고 전환 자체를 실패로 남긴다.

전환 시간과 수신 공백을 기록하되 **무누락을 보장하지 않는다.** 해제와 등록 사이에는
반드시 공백이 생기고 그 길이를 약속할 수 없다. 경과 시간은 단조 시계로 재고, 사람이
대조할 UTC/KST 시각도 함께 남긴다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import time
from uuid import uuid4

STEP_UNSUBSCRIBE = "unsubscribe_regular"
STEP_FINALIZE = "finalize_regular"
STEP_OPEN = "open_aftermarket"
STEP_SUBSCRIBE = "subscribe_nxt"
STEPS = (STEP_UNSUBSCRIBE, STEP_FINALIZE, STEP_OPEN, STEP_SUBSCRIBE)

#: 4단계와 별개인 실패 사유 — 전환 중 콜백 진단 보존이 한도를 넘었다.
STEP_CALLBACK_CAPACITY = "callback_capacity_exceeded"

#: 전환 중 콜백 진단 보존 상한. 실측으로 정해진 값이 아니라 무한정 누적을 막는 임시 상한이다.
DEFAULT_MAX_PENDING_CALLBACKS = 2000

KST = timezone(timedelta(hours=9))


def _wall_clock(now: datetime):
    return now.astimezone(timezone.utc).isoformat(), now.astimezone(KST).isoformat()


@dataclass
class StepTiming:
    """단계 하나의 시작·완료 시각과 결과. 실패한 단계는 outcome 대신 error 를 남긴다."""
    step: str
    started_at: float
    started_at_utc: str
    started_at_kst: str
    completed_at: float | None = None
    completed_at_utc: str | None = None
    completed_at_kst: str | None = None
    outcome: str | None = None
    error: str | None = None

    def describe(self):
        return dict(
            step=self.step,
            started_at=self.started_at, started_at_utc=self.started_at_utc, started_at_kst=self.started_at_kst,
            completed_at=self.completed_at, completed_at_utc=self.completed_at_utc,
            completed_at_kst=self.completed_at_kst,
            duration_sec=(None if self.completed_at is None else self.completed_at - self.started_at),
            outcome=self.outcome, error=self.error)


@dataclass
class TransitionRecord:
    started_at: float
    started_at_utc: str
    started_at_kst: str
    transition_id: str = field(default_factory=lambda: uuid4().hex)
    finished_at: float | None = None
    finished_at_utc: str | None = None
    finished_at_kst: str | None = None
    prev_session_id: str | None = None
    next_session_id: str | None = None
    code_revision: str | None = None
    plan_revision: dict | None = None
    steps_completed: tuple = ()
    step_timing: tuple = ()
    failed_step: str | None = None
    error: str | None = None
    callbacks_during: tuple = ()
    callbacks_dropped: int = 0
    callback_capacity: int = DEFAULT_MAX_PENDING_CALLBACKS
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
            schema="session_transition_v2",
            transition_id=self.transition_id,
            prev_session_id=self.prev_session_id,
            next_session_id=self.next_session_id,
            code_revision=self.code_revision,
            plan_revision=self.plan_revision,
            started_at=self.started_at, started_at_utc=self.started_at_utc, started_at_kst=self.started_at_kst,
            finished_at=self.finished_at, finished_at_utc=self.finished_at_utc,
            finished_at_kst=self.finished_at_kst,
            elapsed_sec=self.elapsed_sec,
            steps_completed=list(self.steps_completed),
            step_timing=[t.describe() for t in self.step_timing],
            failed_step=self.failed_step, error=self.error,
            succeeded=self.succeeded,
            callbacks_during_transition=len(self.callbacks_during),
            callbacks_preserved=[dict(c) for c in self.callbacks_during],
            callbacks_dropped=self.callbacks_dropped,
            callback_capacity=self.callback_capacity,
            last_event_before=self.last_event_before,
            first_event_after=self.first_event_after,
            reception_gap_sec=self.reception_gap_sec,
            note="전환 시간과 수신 공백은 기록이며 무누락을 보장하지 않는다",
            callback_note="활성 구독 문맥은 참고 정보이며 접미사나 도착 시점만으로 실제 출처를 "
                          "확정하지 않는다. 보존된 FID 는 등록된 필드만 포함하며 서버가 제공하는 "
                          "전체 필드가 아니다")


class SessionTransition:
    """각 단계를 호출 가능한 형태로 받아 순서대로 실행한다.

    finalize_regular 은 정규장 저장이 닫혔는지를 돌려줘야 한다. 참이 아니면 실패로 본다.
    """

    def __init__(self, *, unsubscribe_regular, finalize_regular, open_aftermarket,
                 subscribe_nxt, clock=time.monotonic, wall_clock=None,
                 max_pending_callbacks=DEFAULT_MAX_PENDING_CALLBACKS):
        self._steps = {
            STEP_UNSUBSCRIBE: unsubscribe_regular,
            STEP_FINALIZE: finalize_regular,
            STEP_OPEN: open_aftermarket,
            STEP_SUBSCRIBE: subscribe_nxt,
        }
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._max_pending_callbacks = max_pending_callbacks
        self.record: TransitionRecord | None = None
        self._in_progress = False
        self._current_step = None
        self._pending_callbacks = []
        self._callbacks_dropped = 0

    @property
    def in_progress(self) -> bool:
        return self._in_progress

    @property
    def current_step(self) -> str | None:
        """지금 실행 중인 단계 이름. 전환이 끝나면 마지막으로 실행된 단계를 그대로 둔다."""
        return self._current_step

    def accept_callback(self, **payload):
        """전환 중 도착한 콜백을 보존한다. 저장 파일에는 넣지 않는다.

        한도를 넘으면 보존하지 않고 누락 건수만 센다 — 무한정 쌓아 두는 대신,
        전환 실행부가 이를 보고 전환을 실패로 남길 수 있게 조용히 버리지 않는다.
        """
        if not self._in_progress:
            return False
        if len(self._pending_callbacks) >= self._max_pending_callbacks:
            self._callbacks_dropped += 1
            return False
        payload.setdefault("transition_step", self._current_step)
        utc, kst = _wall_clock(self._wall_clock())
        self._pending_callbacks.append(dict(payload, at=self._clock(), at_utc=utc, at_kst=kst))
        return True

    def run(self, *, last_event_before=None, prev_session_id=None,
            code_revision=None, plan_revision=None):
        if self.record is not None:
            raise ValueError("전환은 한 번만 실행한다. 새 전환은 새 객체로 한다")
        self._in_progress = True
        started_utc, started_kst = _wall_clock(self._wall_clock())
        record = TransitionRecord(
            started_at=self._clock(), started_at_utc=started_utc, started_at_kst=started_kst,
            last_event_before=last_event_before, prev_session_id=prev_session_id,
            code_revision=code_revision, plan_revision=plan_revision,
            callback_capacity=self._max_pending_callbacks)
        self.record = record
        completed = []
        timings = []
        try:
            for name in STEPS:
                self._current_step = name
                step_start_at = self._clock()
                step_start_utc, step_start_kst = _wall_clock(self._wall_clock())
                timing = StepTiming(step=name, started_at=step_start_at,
                                    started_at_utc=step_start_utc, started_at_kst=step_start_kst)
                timings.append(timing)
                try:
                    outcome = self._steps[name]()
                except Exception as exc:
                    record.failed_step = name
                    record.error = f"{type(exc).__name__}: {exc}"
                    timing.error = record.error
                    timing.completed_at = self._clock()
                    timing.completed_at_utc, timing.completed_at_kst = _wall_clock(self._wall_clock())
                    break
                timing.completed_at = self._clock()
                timing.completed_at_utc, timing.completed_at_kst = _wall_clock(self._wall_clock())
                if name == STEP_FINALIZE and not outcome:
                    # 닫혔는지 확인되지 않았다. 다음 파일을 열지 않는다.
                    record.failed_step = name
                    record.error = "정규장 저장 마무리가 확인되지 않았다 (닫힘/마무리 정보 없음)"
                    timing.error = record.error
                    break
                timing.outcome = repr(outcome)[:500]
                completed.append(name)
                if self._callbacks_dropped and record.failed_step is None:
                    # 진단 보존이 한도를 넘었다 — 누락을 숨기지 않고 전환을 여기서 멈춘다.
                    record.failed_step = STEP_CALLBACK_CAPACITY
                    record.error = (f"전환 중 콜백 진단 보존 한도 초과 "
                                    f"({self._callbacks_dropped}건 누락, 한도 {self._max_pending_callbacks})")
                    break
        finally:
            record.steps_completed = tuple(completed)
            record.step_timing = tuple(timings)
            record.finished_at = self._clock()
            record.finished_at_utc, record.finished_at_kst = _wall_clock(self._wall_clock())
            record.callbacks_during = tuple(self._pending_callbacks)
            record.callbacks_dropped = self._callbacks_dropped
            self._in_progress = False
        return record

    def note_first_event_after(self, at=None) -> bool:
        """전환 뒤 첫 수신 시각을 한 번만 채운다. 방금 채웠으면 True."""
        if self.record is not None and self.record.first_event_after is None:
            self.record.first_event_after = self._clock() if at is None else at
            return True
        return False
