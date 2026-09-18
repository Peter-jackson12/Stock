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
import json
import math
import os
from pathlib import Path
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


def write_transition_record(path, payload):
    """raw와 분리된 최신 진단을 원자적으로 교체한다. 동기화 실패도 호출자에게 알린다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class TransitionPersistenceError(RuntimeError):
    pass


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
    last_event_before_utc: str | None = None
    last_event_before_kst: str | None = None
    first_event_after_utc: str | None = None
    first_event_after_kst: str | None = None
    phase: str = "started"
    recording_error: str | None = None
    cleanup: tuple = ()

    @property
    def succeeded(self) -> bool:
        return (self.failed_step is None and self.recording_error is None
                and self.phase == "completed" and self.finished_at is not None
                and len(self.steps_completed) == len(STEPS))

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
        gap = self.first_event_after - self.last_event_before
        return gap if math.isfinite(gap) and gap >= 0 else None

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
            reception_clock="monotonic",
            last_event_before_utc=self.last_event_before_utc,
            last_event_before_kst=self.last_event_before_kst,
            first_event_after_utc=self.first_event_after_utc,
            first_event_after_kst=self.first_event_after_kst,
            phase=self.phase, recording_error=self.recording_error,
            cleanup=list(self.cleanup),
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
                 max_pending_callbacks=DEFAULT_MAX_PENDING_CALLBACKS,
                 persist=None, cancelled=None, timeout_seconds=30):
        if (isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 30):
            raise ValueError("전환 제한 시간은 0초 초과 30초 이하여야 한다")
        self._steps = {
            STEP_UNSUBSCRIBE: unsubscribe_regular,
            STEP_FINALIZE: finalize_regular,
            STEP_OPEN: open_aftermarket,
            STEP_SUBSCRIBE: subscribe_nxt,
        }
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._max_pending_callbacks = max_pending_callbacks
        self._persist_record = persist
        self._cancelled = cancelled or (lambda: False)
        self._timeout_seconds = timeout_seconds
        self.record: TransitionRecord | None = None
        self._in_progress = False
        self._current_step = None
        self._pending_callbacks = []
        self._callbacks_dropped = 0

    def persist(self):
        """기록 실패를 메모리에도 보존한다. 실패 기록 자체의 저장은 보장할 수 없다."""
        self.record.callbacks_during = tuple(self._pending_callbacks)
        self.record.callbacks_dropped = self._callbacks_dropped
        if self._persist_record is not None:
            try:
                self._persist_record()
            except Exception as exc:
                self.record.recording_error = f"{type(exc).__name__}: {exc}"
                raise TransitionPersistenceError(self.record.recording_error) from exc

    def _check(self):
        if self.record.recording_error:
            raise TransitionPersistenceError(self.record.recording_error)
        if self._cancelled():
            raise RuntimeError("전환 취소 요청")
        if self._clock() - self.record.started_at >= self._timeout_seconds:
            raise TimeoutError("전환 제한 시간 초과")
        if self._callbacks_dropped:
            raise RuntimeError("전환 중 콜백 진단 보존 한도 초과")

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
            try:
                self.persist()
            except TransitionPersistenceError:
                pass
            return False
        payload.setdefault("transition_step", self._current_step)
        utc, kst = _wall_clock(self._wall_clock())
        self._pending_callbacks.append(dict(payload, at=self._clock(), at_utc=utc, at_kst=kst))
        try:
            self.persist()
        except TransitionPersistenceError:
            return False
        return True

    def run(self, *, last_event_before=None, prev_session_id=None,
            code_revision=None, plan_revision=None, last_event_before_utc=None):
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
        timing = None
        try:
            if last_event_before_utc is not None:
                record.last_event_before_utc, record.last_event_before_kst = _wall_clock(
                    datetime.fromisoformat(last_event_before_utc))
            self.persist()
            for name in STEPS:
                self._current_step = name
                self._check()
                step_start_at = self._clock()
                step_start_utc, step_start_kst = _wall_clock(self._wall_clock())
                timing = StepTiming(step=name, started_at=step_start_at,
                                    started_at_utc=step_start_utc, started_at_kst=step_start_kst)
                record.step_timing += (timing,)
                record.phase = "step_started"
                self.persist()  # 단계 시작 기록에 성공해야 부작용을 실행한다.
                self._check()
                outcome = self._steps[name]()
                timing.completed_at = self._clock()
                timing.completed_at_utc, timing.completed_at_kst = _wall_clock(self._wall_clock())
                if name == STEP_FINALIZE and not outcome:
                    # 닫혔는지 확인되지 않았다. 다음 파일을 열지 않는다.
                    raise RuntimeError("정규장 저장 마무리가 확인되지 않았다 (닫힘/마무리 정보 없음)")
                timing.outcome = repr(outcome)[:500]
                record.steps_completed += (name,)
                record.phase = "step_completed"
                self.persist()
                self._check()
                timing = None
            record.finished_at = self._clock()
            record.finished_at_utc, record.finished_at_kst = _wall_clock(self._wall_clock())
            record.phase = "completed"
            self.persist()
            self._check()
        except (Exception, KeyboardInterrupt) as exc:
            record.failed_step = ("diagnostic_write" if isinstance(exc, TransitionPersistenceError)
                                  else STEP_CALLBACK_CAPACITY if self._callbacks_dropped
                                  else self._current_step or "transition_start")
            record.error = f"{type(exc).__name__}: {exc}"
            if timing is not None:
                timing.error = record.error
                timing.completed_at = self._clock()
                timing.completed_at_utc, timing.completed_at_kst = _wall_clock(self._wall_clock())
            record.finished_at = self._clock()
            record.finished_at_utc, record.finished_at_kst = _wall_clock(self._wall_clock())
            record.phase = "failed"
            try:
                self.persist()
            except TransitionPersistenceError:
                pass
        finally:
            record.callbacks_during = tuple(self._pending_callbacks)
            record.callbacks_dropped = self._callbacks_dropped
            self._in_progress = False
        return record

    def note_first_event_after(self, at=None, at_utc=None) -> bool:
        """전환 뒤 첫 수신 시각을 한 번만 채운다. 방금 채웠으면 True."""
        if (self.record is not None and self.record.succeeded and not self.in_progress
                and self.record.first_event_after is None):
            self.record.first_event_after = self._clock() if at is None else at
            self.record.first_event_after_utc, self.record.first_event_after_kst = _wall_clock(
                self._wall_clock() if at_utc is None else datetime.fromisoformat(at_utc))
            try:
                self.persist()
            except TransitionPersistenceError as exc:
                self.record.failed_step = "diagnostic_write"
                self.record.error = str(exc)
                self.record.phase = "failed"
                raise
            return True
        return False
