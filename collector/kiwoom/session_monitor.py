"""
collector/kiwoom/session_monitor.py — 수집 세션 감시기 (키움 API 의존성 없음)

2026-09-14 전 종목 수집에서 PC 절전으로 14:41:18 이후 이벤트가 끊겼는데도
콘솔은 54분 뒤인 15:35:00 에 "정상 종료" 를 출력했다. 결손 54분치를
'정상' 이라고 보고한 것이다. 무인 운영에서는 결손 자체보다 **결손을 정상이라고
말하는 것**이 더 위험하다 — 파일을 직접 열어보기 전까지 아무도 모른다.

그래서 이 모듈이 고정하는 것은 "수집이 잘 됐는가" 가 아니라
**"수집이 끊겼을 때 그것이 종료 보고에 드러나는가"** 다.

[핫패스 보호 — 설계 제약]
- 이벤트 1건당 하는 일은 카운터 증가와 float 대입뿐이다. I/O 도 락도 없다.
- 침묵 판정은 호출자의 기존 1초 주기 루프에서 tick() 한 번으로 끝난다.
- 파일 기록은 SessionLog 가 전담하고, 1초 주기 상태줄은 기본 60초에 한 번만
  파일로 내린다 (콘솔 상태줄은 CR 덮어쓰기라 스크롤백에도 남지 않는
  휘발성 출력이다. 메시지성 출력은 전부 즉시 파일로 간다).

[결손 판정을 '장중 구간'으로 자르는 이유]
장 마감은 15:30 인데 데몬은 15:35 에 내려간다. 마지막 체결과 종료 시각의
단순 차이로 판정하면 아무 문제 없는 날도 매번 5분 결손으로 잡힌다. 그래서
판정은 [09:00, 15:30] 과 겹치는 구간의 길이로 하고, 사람이 읽는 문구에는
실제 벽시계 간격도 같이 적는다.

[대기큐 적체 판정을 절대값이 아니라 추세로 하는 이유] (발견 #13)
장 시작 직후엔 큐가 수만~수십만 건까지 쌓였다 빠지는 게 정상 패턴이다
(2026-09-15 실측: 09:16:55 에 203,692건까지 쌓였다가 09:51 에 0에 가깝게
빠졌다). 절대값으로 임계를 걸면 개장 직후엔 매일 울려서 경보가 무의미해진다.
그래서 "얼마나 쌓였는가"가 아니라 "따라잡고 있는가"를 본다 — 큐가 바닥
(queue_backlog_floor) 이상인 상태가 stuck_window 이상 지속되면서, 그 구간의
시작 시점보다 줄어들지 않았다면 적체로 본다. 개장 폭주처럼 쌓였다가도 계속
빠지고 있으면 알리지 않고, 다 빠지지 않은 채 그대로거나 계속 느는 경우에만
알린다.

바닥은 실제 큐가 도달할 수 있는 값이어야 한다. raw-v2 큐는 capacity 에서 넘침을
fail-closed 중단으로 처리하므로, 그보다 큰 바닥은 경보가 영원히 울리지 않는다는 뜻이다.
그래서 bounded 큐에서는 capacity 로부터 바닥을 정하고(queue_backlog_floor_for_capacity),
capacity 를 넘는 바닥은 설정 오류로 거부한다. 지속 여부는 '바닥 이상이 처음 관측된 시각'
을 명시 상태로 들고, 비교 기준점은 시간 창 경계 직전 표본을 남겨 잡는다 — 표본 간격이
불규칙해도 창 길이를 정확히 채운 뒤에 판정한다.

[명시적 시장 구간 프로필 — 경고·회복·종료가 같은 의미를 쓴다]
sessions 프로필을 주면 판정은 종류별 시계(체결은 체결 수신만, 호가는 호가 수신만)와
market_sessions 의 구간 정의 하나로 한다. 침묵 경고, 회복(구간 닫기), 종료 보고의 결손
판정이 모두 같은 구간/임계/시계를 쓴다. 호가만 들어와도 체결 침묵은 회복되지 않는다.
NOT_EXPECTED/UNJUDGED 구간의 시간은 어느 쪽에서도 결손으로 세지 않는다.
sessions=None 인 기본(legacy) 경로는 기존대로 정규장 단일 창과 공통 last_event_ts 를 쓴다.

시계(clock)는 주입 가능하다. 키움 API 없이 테스트하기 위한 것이다.
"""

from __future__ import annotations

import logging
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Sequence

# 정규장 구간. 이 밖의 침묵은 결손이 아니라 그냥 장이 안 선 것이다.
MARKET_OPEN = dtime(9, 0, 0)
MARKET_CLOSE = dtime(15, 30, 0)

# 이 초 이상 신규 이벤트가 없으면 결손을 의심한다.
DEFAULT_GAP_THRESHOLD_SEC = 120.0

# 침묵이 계속되는 동안 경고를 되풀이하는 간격 (한 번 찍고 말면 묻힌다).
DEFAULT_WARN_REPEAT_SEC = 60.0

# 무한 큐(raw-v1 의 queue.Queue) 경로의 바닥. 이 건수 미만이면 적체로 보지 않는다
# (개장 직후 정상 변동 범위를 덮는다). 2026-09-15 실측 최대 203,692건 대비 여유를 두되,
# 점심시간대 통상 변동(수백~1천대)은 확실히 걸러지도록 잡은 값이다.
# bounded 큐(raw-v2)에는 쓰지 않는다 — capacity 보다 커서 도달할 수 없다.
DEFAULT_QUEUE_BACKLOG_FLOOR = 20_000


def queue_backlog_floor_for_capacity(capacity: int, batch_size: int) -> int:
    """bounded 큐의 적체 바닥 = capacity 의 절반.

    넘침(capacity 도달)은 fail-closed 중단이므로 경보는 그 전에 도달 가능해야 하고,
    writer 가 한 번에 들고 있는 batch 한 개 분량의 정상 변동보다는 커야 한다.
    절반은 그 사이의 구조적 중간값이며, 실측으로 조정한 값이 아니다(임시 정책값).
    """
    for name, value in (("capacity", capacity), ("batch_size", batch_size)):
        if type(value) is not int or value < 1:
            raise ValueError(f"positive integer {name} required")
    floor = capacity // 2
    if floor <= batch_size:
        raise ValueError("queue capacity must exceed two writer batches for a meaningful backlog floor")
    return floor

# 이 시간 동안 바닥 이상을 유지하면서 줄지 않으면 "적체가 안 빠진다"고 본다.
DEFAULT_QUEUE_STUCK_WINDOW_SEC = 300.0

#: 장중 침묵이 이만큼 이어지면 '안전한 종료 시도' 를 권고한다. None 이면 권고하지 않는다.
#: 2026-09-17 오전에는 10:31 수신 정체 뒤 65분간 경고만 반복하며 수집 잠금을 쥐고 있었고,
#: 그 사이 프로세스가 네이티브 충돌로 죽어 파일이 닫히지 않았다. 경고만으로는 부족하다.
#: 이 권고는 종료 '시도' 를 부르는 신호일 뿐, 파일 닫힘을 보장하지 않는다 —
#: Qt/OCX 가 멈춘 경우에는 기존 종료 경로 자체가 실행되지 않을 수 있다.
DEFAULT_SILENCE_STOP_SEC = 600.0


#: market_sessions.TRADE / QUOTE 의 사람용 이름. 모듈 import 를 늦추려고 문자열로 둔다.
_KIND_LABELS = {"trade": "체결", "quote": "호가"}


def format_clock(ts: float | None) -> str:
    """epoch 초를 HH:MM:SS 로. None 이면 '없음'."""
    if ts is None:
        return "없음"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def format_stamp(ts: float | None) -> str:
    """epoch 초를 YYYY-MM-DD HH:MM:SS 로. None 이면 '없음'."""
    if ts is None:
        return "없음"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds: float) -> str:
    """초를 '1시간 3분 5초' / '48분 42초' / '7초' 로."""
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}시간 {minutes}분 {secs}초"
    if minutes:
        return f"{minutes}분 {secs}초"
    return f"{secs}초"


@dataclass
class SilenceGap:
    """임계를 넘긴 무이벤트 구간 하나."""

    start_ts: float   # 침묵 직전 마지막 수신 시각 (이벤트가 0건이면 장 시작 시각)
    end_ts: float     # 수신이 재개된 시각, 또는 세션이 끝난 시각
    recovered: bool   # False = 끝내 회복되지 않고 세션(또는 판정 구간)이 종료됨
    kind: str | None = None   # 구간 프로필 경로의 이벤트 종류. None = legacy 공통 시계

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_ts - self.start_ts)

    def describe(self) -> str:
        tail = "" if self.recovered else ", 미회복"
        label = "" if self.kind is None else f"{_KIND_LABELS.get(self.kind, self.kind)} "
        return (
            f"{label}{format_clock(self.start_ts)} ~ {format_clock(self.end_ts)} "
            f"({format_duration(self.duration_sec)}{tail})"
        )


@dataclass
class SessionReport:
    """종료 보고서. headline 은 한 줄 판정, lines() 는 세션 요약 블록."""

    reason: str
    started_at: float
    ended_at: float
    first_event_ts: float | None
    last_event_ts: float | None
    trade_count: int
    quote_count: int
    gaps: list[SilenceGap]
    gap_threshold_sec: float
    trailing_wall_sec: float      # 마지막 수신 ~ 종료 (벽시계)
    trailing_market_sec: float    # 그중 정규장과 겹치는 길이 (판정 기준)
    healthy: bool
    queue_max_depth: int = 0            # 장중 관측된 대기큐 최대값 (발견 #13)
    queue_max_ts: float | None = None   # 그 최대값이 찍힌 시각
    extra: list[tuple[str, str]] = field(default_factory=list)
    # 구간 프로필 경로에서만 채운다. None 이면 legacy 공통 시계(마지막 '수신') 기준이다.
    trailing_kind: str | None = None             # 종료 판정에 쓴 종류 (trade/quote)
    trailing_last_ts: float | None = None        # 그 종류의 마지막 수신 시각
    trailing_threshold_sec: float | None = None  # 그 판정에 쓴 구간 임계

    @property
    def total_events(self) -> int:
        return self.trade_count + self.quote_count

    @property
    def _trailing_label(self) -> str:
        return "수신" if self.trailing_kind is None else _KIND_LABELS.get(self.trailing_kind, self.trailing_kind)

    @property
    def _trailing_ts(self) -> float | None:
        return self.last_event_ts if self.trailing_kind is None else self.trailing_last_ts

    @property
    def _trailing_threshold(self) -> float:
        return self.gap_threshold_sec if self.trailing_threshold_sec is None else self.trailing_threshold_sec

    def _gap_detail(self) -> str:
        """
        마지막 수신 ~ 종료 간격을 사람이 읽을 문장으로.

        healthy 여부와 무관하게 항상 호출된다 — '정상 종료' 라는 결과만 찍고
        그 근거(갭이 실제로 얼마였는지)를 안 찍으면, 시각 파싱이 깨져 갭이
        늘 0으로 나와도 로그는 영원히 정상이라고만 말한다. 판정 결과와 판정
        근거를 같이 보여줘야 이 헬스체크 자체가 살아 있는지 매일 확인할 수 있다.
        """
        threshold = int(self._trailing_threshold)
        label = self._trailing_label
        wall = format_duration(self.trailing_wall_sec)
        if abs(self.trailing_wall_sec - self.trailing_market_sec) >= 1.0:
            market = format_duration(self.trailing_market_sec)
            return f"마지막 {label} 후 {wall} 경과, 장중 결손 판정 {market} (임계 {threshold}초)"
        return f"마지막 {label} 후 {wall} 경과 (임계 {threshold}초)"

    @property
    def headline(self) -> str:
        if self.total_events == 0:
            return (
                "⚠️ 비정상 종료 — 수신 이벤트 0건 "
                f"(세션 {format_clock(self.started_at)}~{format_clock(self.ended_at)} 동안 단 1건도 없음)"
            )

        threshold = int(self._trailing_threshold)
        label = self._trailing_label
        last = format_clock(self._trailing_ts)
        if not self.healthy:
            # 종료가 장 마감 뒤라면 벽시계 간격에는 '장이 끝나서 조용한 시간'이
            # 섞여 있다. 결손으로 의심하는 실제 길이를 따로 밝힌다.
            detail = "결손 의심"
            if abs(self.trailing_wall_sec - self.trailing_market_sec) >= 1.0:
                detail = f"장중 결손 {format_duration(self.trailing_market_sec)}, 결손 의심"
            silent = "무이벤트" if self.trailing_kind is None else f"{label} 없음"
            return (
                f"⚠️ 비정상 종료 — 마지막 {label} {last}, "
                f"이후 {format_duration(self.trailing_wall_sec)} {silent} ({detail}, 임계 {threshold}초 초과)"
            )

        if self.gaps:
            return (
                f"✅ 정상 종료 — 마지막 {label} {last} "
                f"({self._gap_detail()}; 장중 침묵 {len(self.gaps)}건 감지 — 아래 세션 요약 확인)"
            )

        return (
            f"✅ 정상 종료 — 마지막 {label} {last} "
            f"({self._gap_detail()})"
        )

    def lines(self) -> list[str]:
        """세션 요약 블록. 콘솔과 파일에 같은 내용이 나간다."""
        threshold = int(self.gap_threshold_sec)
        elapsed = format_duration(self.ended_at - self.started_at)
        out = [
            "─" * 65,
            "📋 세션 요약",
            f"  종료 사유     : {self.reason}",
            f"  세션 시작     : {format_stamp(self.started_at)}",
            f"  세션 종료     : {format_stamp(self.ended_at)} ({elapsed})",
            f"  첫 이벤트     : {format_clock(self.first_event_ts)}",
            f"  마지막 이벤트 : {format_clock(self.last_event_ts)}",
        ]
        if self.trailing_kind is not None:
            out.append(f"  마지막 {self._trailing_label}   : {format_clock(self.trailing_last_ts)} (구간 프로필 판정 기준)")
        out += [
            f"  마지막~종료 갭 : {self._gap_detail()}",
            f"  체결 총건수   : {self.trade_count:,} 건",
            f"  호가 총건수   : {self.quote_count:,} 건",
            f"  장중 대기큐 최대: {self.queue_max_depth:,} 건 ({format_clock(self.queue_max_ts)})",
        ]
        for label, value in self.extra:
            out.append(f"  {label} : {value}")

        out.append(f"  임계({threshold}초) 초과 침묵 구간 : {len(self.gaps)}건")
        if not self.gaps:
            out.append("    (없음)")
        else:
            for idx, gap in enumerate(self.gaps, start=1):
                out.append(f"    {idx}) {gap.describe()}")
        out.append("─" * 65)
        return out


@dataclass
class _OpenKindGap:
    """구간 프로필 경로에서 아직 닫히지 않은 종류별 침묵 구간."""

    start_ts: float
    close_ts: float        # 이 침묵을 판정한 구간이 닫히는 시각
    last_warn_ts: float


class SessionMonitor:
    """
    수신 상태를 추적하고 침묵을 판정한다.

    호출 규약:
      start()                    — 세션 시작 시 1회
      on_trade() / on_quote()    — 큐에 넣은 직후, 이벤트 1건마다 (핫패스)
      tick()                     — 기존 주기 루프에서 1초마다. 알릴 말이 있으면 문자열
      sample_queue_depth(depth)  — tick() 과 같은 주기 루프에서 1초마다. 마찬가지
      finish(reason)             — 종료 시 1회. SessionReport 반환
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        gap_threshold_sec: float = DEFAULT_GAP_THRESHOLD_SEC,
        warn_repeat_sec: float = DEFAULT_WARN_REPEAT_SEC,
        market_open: dtime = MARKET_OPEN,
        market_close: dtime = MARKET_CLOSE,
        sessions=None,
        queue_backlog_floor: int | None = None,
        queue_stuck_window_sec: float = DEFAULT_QUEUE_STUCK_WINDOW_SEC,
        silence_stop_sec: float | None = DEFAULT_SILENCE_STOP_SEC,
        queue_capacity: int | None = None,
        queue_batch_size: int | None = None,
    ) -> None:
        self._clock = clock
        self.gap_threshold_sec = float(gap_threshold_sec)
        self.warn_repeat_sec = float(warn_repeat_sec)
        self._market_open = market_open
        self._market_close = market_close
        # 거래소별 활동 구간. None 이면 기존 단일 창(정규장)을 그대로 쓴다 —
        # 명시적으로 프로필을 넘겼을 때만 구간별 판정으로 바뀐다.
        self._sessions = tuple(sessions) if sessions else None
        # 적체 바닥. bounded 큐면 capacity 에서 정하고, 도달할 수 없는 바닥은 거부한다.
        # capacity 를 모르면(무한 큐) 기존 바닥을 쓴다.
        if (queue_capacity is None) != (queue_batch_size is None):
            raise ValueError("queue_capacity and queue_batch_size must be given together")
        if queue_capacity is not None:
            derived = queue_backlog_floor_for_capacity(queue_capacity, queue_batch_size)
            if queue_backlog_floor is None:
                queue_backlog_floor = derived
            elif not 0 < int(queue_backlog_floor) <= queue_capacity:
                raise ValueError(
                    f"queue_backlog_floor {queue_backlog_floor} is unreachable for queue capacity {queue_capacity}")
        elif queue_backlog_floor is None:
            queue_backlog_floor = DEFAULT_QUEUE_BACKLOG_FLOOR
        self.queue_capacity = queue_capacity
        self.queue_backlog_floor = int(queue_backlog_floor)
        self.queue_stuck_window_sec = float(queue_stuck_window_sec)
        self.silence_stop_sec = None if silence_stop_sec is None else float(silence_stop_sec)

        # 수신이 기대되는 시점 — 구독 등록이 끝난 뒤에만 침묵 종료를 판정한다.
        # 로그인/등록 전 침묵은 결손이 아니라 정상 대기다.
        self._reception_expected_at: float | None = None
        # 권고를 낸 시점의 기준 시각. 이보다 새 이벤트가 들어오면 권고는 스스로 풀린다.
        # 경고 쪽 구간 기록에 기대지 않는다 — 그 경로를 타지 않아도 회복은 회복이다.
        self._stop_advised_reference: float | None = None

        self.started_at: float | None = None
        self.ended_at: float | None = None

        # 핫패스가 만지는 필드들. 쓰는 스레드는 하나(키움 이벤트 스레드)이고
        # 읽는 쪽(1초 주기 루프)은 최신값을 놓쳐도 1초 뒤에 다시 본다.
        self.first_event_ts: float | None = None
        self.last_event_ts: float | None = None
        # 종류별 마지막 수신. 체결 침묵을 호가 수신이 초기화하면 체결 중단을 놓친다 —
        # 정책만 갈라놓고 시계를 공유하면 판정이 무의미해진다.
        self.last_trade_ts: float | None = None
        self.last_quote_ts: float | None = None
        self.trade_count: int = 0
        self.quote_count: int = 0

        self._gaps: list[SilenceGap] = []
        self._open_gap_start: float | None = None
        self._last_warn_ts: float | None = None
        self._market_open_ts: float = 0.0
        self._market_close_ts: float = 0.0

        # 대기큐 적체 추적(발견 #13). sample_queue_depth() 는 tick() 과 같은
        # 기존 1초 루프에서만 불린다 — 여기도 핫패스가 아니다.
        self._queue_max_depth: int = 0
        self._queue_max_ts: float | None = None
        # 바닥 이상이 연속으로 관측되기 시작한 시각과, 그 연속 구간의 표본들.
        # 표본은 창 경계(now - window) 직전 1개까지만 남긴다 — 그것이 비교 기준점이다.
        self._queue_above_floor_since: float | None = None
        self._queue_history: deque[tuple[float, int]] = deque()
        self._queue_alert_active: bool = False
        self._queue_last_warn_ts: float | None = None

        # 구간 프로필 경로의 종류별 열린 침묵 구간. legacy 는 위 _open_gap_start 를 쓴다.
        self._kind_gaps: dict[str, _OpenKindGap] = {}
        self._judged_kinds: tuple[str, ...] = ()
        if self._sessions is not None:
            from collector.kiwoom.market_sessions import QUOTE, TRADE
            self._judged_kinds = tuple(
                kind for kind in (TRADE, QUOTE) if any(s.judges(kind) for s in self._sessions))

    # ── 수명주기 ──────────────────────────────────────────────────────────

    def start(self) -> None:
        self.started_at = self._clock()
        session_day = datetime.fromtimestamp(self.started_at).date()
        self._market_open_ts = datetime.combine(session_day, self._market_open).timestamp()
        self._market_close_ts = datetime.combine(session_day, self._market_close).timestamp()

    def mark_reception_expected(self) -> None:
        """구독 등록이 끝나 수신이 기대되는 시점을 알린다. 이 뒤부터 침묵 종료를 판정한다."""
        if self._reception_expected_at is None:
            self._reception_expected_at = self._clock()

    def silence_stop_reason(self) -> str | None:
        """장중 침묵이 한도를 넘었으면 종료 사유 한 줄을, 아니면 None.

        한 세션에서 한 번만 돌려준다. 수신이 회복되면 다시 판정할 수 있게 풀린다.
        이 값은 '종료를 시도하라' 는 권고이며, 저장 완료나 파일 닫힘을 뜻하지 않는다.
        """
        if self.silence_stop_sec is None:
            return None
        if self.started_at is None or self._reception_expected_at is None:
            return None
        # 권고 이후 새 이벤트가 들어왔다면 회복된 것이므로 권고를 푼다. 구간 프로필 경로의
        # 권고는 체결 침묵 기준이므로 체결 수신만 회복으로 친다 — 호가만으로는 풀지 않는다.
        recovered_ts = self.last_event_ts if self._sessions is None else self.last_trade_ts
        if (self._stop_advised_reference is not None and recovered_ts is not None
                and recovered_ts > self._stop_advised_reference):
            self._stop_advised_reference = None
        if self._stop_advised_reference is not None:
            return None
        now = self._clock()
        if self._sessions is not None:
            measured = self._session_silence(now)
            if measured is None:
                return None
            silence, _, _ = measured
            # 구독 완료 전 시간은 침묵으로 세지 않는다.
            silence = min(silence, now - self._reception_expected_at)
            reference = now - silence
        else:
            if not (self._market_open_ts <= now <= self._market_close_ts):
                return None
            reference = self.last_event_ts if self.last_event_ts is not None else self._reception_expected_at
            reference = max(reference, self._market_open_ts, self._reception_expected_at)
            silence = now - reference
        if silence < self.silence_stop_sec:
            return None
        self._stop_advised_reference = reference
        if self._sessions is None:
            last = ("수신 이력 없음" if self.last_event_ts is None
                    else f"마지막 수신 {format_clock(self.last_event_ts)}")
        else:
            last = ("체결 수신 이력 없음" if self.last_trade_ts is None
                    else f"마지막 체결 {format_clock(self.last_trade_ts)}")
        return (f"장중 침묵 {format_duration(silence)} — 한도 {format_duration(self.silence_stop_sec)} 초과 "
                f"({last}). 수신 결손 상태로 종료를 시도한다")

    # ── 핫패스 (이벤트 1건당 호출) ────────────────────────────────────────

    def on_trade(self) -> None:
        ts = self._clock()
        self.trade_count += 1
        self.last_trade_ts = ts
        self.last_event_ts = ts
        if self.first_event_ts is None:
            self.first_event_ts = ts

    def on_quote(self) -> None:
        ts = self._clock()
        self.quote_count += 1
        self.last_quote_ts = ts
        self.last_event_ts = ts
        if self.first_event_ts is None:
            self.first_event_ts = ts

    # ── 주기 판정 (기존 1초 루프에서 호출) ────────────────────────────────

    def tick(self) -> str | None:
        """
        침묵 상태를 갱신한다. 새로 알릴 것이 있으면 한 줄로 돌려주고,
        없으면 None. 호출자는 돌려받은 문자열을 그대로 출력하면 된다.
        """
        if self.started_at is None:
            return None

        now = self._clock()

        # 구간 프로필이 있으면 종류별 시계와 구간 정의로만 판정한다(경고·회복 모두).
        if self._sessions is not None:
            notices = [notice for notice in (self._tick_kind(now, kind) for kind in self._judged_kinds)
                       if notice]
            return "\n".join(notices) if notices else None

        # 1) 침묵 중이었는데 새 이벤트가 들어왔다면 구간을 닫는다.
        if self._open_gap_start is not None:
            last = self.last_event_ts
            if last is not None and last > self._open_gap_start:
                gap = SilenceGap(start_ts=self._open_gap_start, end_ts=last, recovered=True)
                self._gaps.append(gap)
                self._open_gap_start = None
                self._last_warn_ts = None
                self._stop_advised_reference = None
                return f"✅ [{format_clock(now)}] 수신 재개 — 침묵 구간 {gap.describe()} 기록"

        # 2) 기존 경로 — 단일 정규장 창. 장 시작 전/마감 후 침묵은 정상이다.
        if not (self._market_open_ts <= now <= self._market_close_ts):
            return None
        reference = self.last_event_ts if self.last_event_ts is not None else self.started_at
        reference = max(reference, self._market_open_ts)
        silence = now - reference
        if silence < self.gap_threshold_sec:
            return None

        if self._open_gap_start is None:
            self._open_gap_start = reference
            self._last_warn_ts = now
            return self._silence_warning(now, silence, first=True)

        if self._last_warn_ts is None or (now - self._last_warn_ts) >= self.warn_repeat_sec:
            self._last_warn_ts = now
            return self._silence_warning(now, silence, first=False)

        return None

    def _last_kind_ts(self, kind) -> float | None:
        from collector.kiwoom.market_sessions import TRADE
        return self.last_trade_ts if kind == TRADE else self.last_quote_ts

    def _tick_kind(self, now: float, kind) -> str | None:
        """구간 프로필 경로에서 한 종류의 침묵을 갱신한다.

        회복은 같은 종류의 수신으로만 인정한다. 침묵을 판정한 구간이 닫히면 그 구간은
        미회복으로 닫는다 — 이어지는 NOT_EXPECTED/UNJUDGED/구간 밖 시간을 침묵 길이에 붙이지 않는다.
        """
        from collector.kiwoom.market_sessions import TRADE
        label = _KIND_LABELS[kind]
        last = self._last_kind_ts(kind)
        gap = self._kind_gaps.get(kind)
        if gap is not None:
            closed = None
            recovered = False
            if last is not None and last > gap.start_ts:
                recovered = last <= gap.close_ts
                closed = SilenceGap(start_ts=gap.start_ts, end_ts=min(last, gap.close_ts),
                                    recovered=recovered, kind=kind)
            elif now >= gap.close_ts:
                closed = SilenceGap(start_ts=gap.start_ts, end_ts=gap.close_ts, recovered=False, kind=kind)
            if closed is not None:
                self._gaps.append(closed)
                del self._kind_gaps[kind]
                if kind == TRADE and recovered:
                    self._stop_advised_reference = None
                if recovered:
                    return f"✅ [{format_clock(now)}] {label} 수신 재개 — 침묵 구간 {closed.describe()} 기록"
                return f"⏹️ [{format_clock(now)}] 판정 구간 종료 — {label} 침묵 구간 {closed.describe()} 기록"

        measured = self._session_silence(now, kind)
        if measured is None:
            return None
        silence, threshold, session = measured
        if silence < threshold:
            return None
        # 구간 기준으로 잰 침묵의 시작 시각. 구간 기록이 이 값을 쓴다.
        reference = now - silence

        if gap is None:
            close_ts = datetime.combine(datetime.fromtimestamp(reference).date(), session.closes).timestamp()
            self._kind_gaps[kind] = _OpenKindGap(start_ts=reference, close_ts=close_ts, last_warn_ts=now)
            return self._kind_silence_warning(now, silence, kind, first=True)

        if (now - gap.last_warn_ts) >= self.warn_repeat_sec:
            gap.last_warn_ts = now
            return self._kind_silence_warning(now, silence, kind, first=False)

        return None

    def _session_silence(self, now: float, kind=None):
        """구간 프로필 기준 (침묵 초, 임계, 구간) 또는 판정하지 않으면 None.

        구간 밖 시간은 세지 않는다. 판정 대상이 아닌 구간(체결을 기대하지 않거나 근거가
        없는 구간)에서는 경고도 정상 판정도 하지 않는다. 감시 시작 전 시간도 세지 않는다.
        """
        from collector.kiwoom.market_sessions import QUOTE, TRADE, silence_seconds
        kind = TRADE if kind is None else kind
        assert kind in (TRADE, QUOTE)
        moment = datetime.fromtimestamp(now)
        # 종류별 시계를 쓴다. 체결 침묵은 체결 수신만으로 초기화된다.
        last_ts = self._last_kind_ts(kind)
        if last_ts is None:
            last_ts = self.started_at
        last = None if last_ts is None else datetime.fromtimestamp(last_ts)
        seconds, session = silence_seconds(self._sessions, last_event_at=last, now=moment, kind=kind)
        if seconds is None or session is None:
            return None
        return seconds, float(session.gap_for(kind)), session

    def _trailing_judged(self, now: float):
        """종료 시점의 종류별 꼬리 침묵을 구간 프로필로 잰다.

        마지막 같은-종류 수신(없으면 감시 시작) ~ 종료 사이에서, 그 종류를 판정하는 구간마다
        겹친 길이를 구간 임계와 비교한다. NOT_EXPECTED/UNJUDGED 구간과 구간 밖 시간은 세지 않고,
        닫힌 구간을 가로질러 누적하지도 않는다 — tick() 의 경고와 같은 의미다.
        (종류, 구간, 시작, 끝, 겹친 길이, 임계) 목록을 돌려준다.
        """
        spans = []
        for kind in self._judged_kinds:
            reference = self._last_kind_ts(kind)
            if reference is None:
                reference = self.started_at
            if reference is None or reference >= now:
                continue
            day = datetime.fromtimestamp(reference).date()
            last_day = datetime.fromtimestamp(now).date()
            while day <= last_day:
                for session in self._sessions:
                    if not session.judges(kind):
                        continue
                    start = max(reference, datetime.combine(day, session.opens).timestamp())
                    end = min(now, datetime.combine(day, session.closes).timestamp())
                    if end > start:
                        spans.append((kind, session, start, end, end - start, float(session.gap_for(kind))))
                day += timedelta(days=1)
        return spans

    def _kind_silence_warning(self, now: float, silence: float, kind, *, first: bool) -> str:
        head = "⚠️" if first else "⚠️ (계속)"
        label = _KIND_LABELS[kind]
        last = self._last_kind_ts(kind)
        if last is None:
            return (
                f"{head} [{format_clock(now)}] 장중 {format_duration(silence)}째 {label} 0건 "
                f"— 아직 {label}을 수신하지 못했습니다 (결손 의심)"
            )
        return (
            f"{head} [{format_clock(now)}] 장중 {format_duration(silence)}째 신규 {label} 없음 "
            f"— 마지막 {label} {format_clock(last)} (결손 의심)"
        )

    def _silence_warning(self, now: float, silence: float, *, first: bool) -> str:
        head = "⚠️" if first else "⚠️ (계속)"
        if self.last_event_ts is None:
            return (
                f"{head} [{format_clock(now)}] 장중 {format_duration(silence)}째 이벤트 0건 "
                "— 아직 단 1건도 수신하지 못했습니다 (구독 등록/회선 확인)"
            )
        return (
            f"{head} [{format_clock(now)}] 장중 {format_duration(silence)}째 신규 이벤트 없음 "
            f"— 마지막 수신 {format_clock(self.last_event_ts)} (결손 의심)"
        )

    # ── 대기큐 적체 판정 (기존 1초 루프에서 tick() 과 함께 호출, 발견 #13) ──

    def sample_queue_depth(self, depth: int) -> str | None:
        """
        대기큐(수신~DB 반영 사이) 잔량 1건을 표본으로 받는다.

        절대값이 아니라 "바닥(queue_backlog_floor) 이상을 stuck_window 동안
        유지하면서 그 구간 시작 시점보다 줄지 않았는가"로 판정한다. 개장 직후
        수십만 건까지 쌓였다가도 계속 빠지고 있으면 정상이고, 쌓인 채 그대로
        거나 계속 늘면 그때 알린다. 새로 알릴 것이 있으면 문자열, 없으면 None.

        지속 시간은 바닥 이상이 처음 관측된 시각(_queue_above_floor_since)부터 잰다.
        비교 기준점은 창 경계(now - stuck_window) 이하 표본 중 가장 최근 것이다 —
        경계 직전 표본을 버리지 않으므로 표본 간격이 창보다 길어도 판정이 빠지지 않는다.
        바닥 아래 표본이 하나라도 오면 연속 구간은 끊긴다.
        """
        now = self._clock()

        if depth > self._queue_max_depth:
            self._queue_max_depth = depth
            self._queue_max_ts = now

        # 시계가 되돌아가면 지속 시간을 믿을 수 없다. 연속 구간을 새로 시작한다.
        if self._queue_history and now < self._queue_history[-1][0]:
            self._queue_history.clear()
            self._queue_above_floor_since = None

        if depth < self.queue_backlog_floor:
            self._queue_history.clear()
            self._queue_above_floor_since = None
            self._queue_alert_active = False
            self._queue_last_warn_ts = None
            return None

        if self._queue_above_floor_since is None:
            self._queue_above_floor_since = now
        self._queue_history.append((now, depth))
        cutoff = now - self.queue_stuck_window_sec
        # 경계 이하 표본은 가장 최근 1개만 기준점으로 남긴다.
        while len(self._queue_history) >= 2 and self._queue_history[1][0] <= cutoff:
            self._queue_history.popleft()

        baseline_ts, baseline_depth = self._queue_history[0]
        sustained = baseline_ts <= cutoff        # 바닥 이상이 창 길이 이상 이어졌다
        not_draining = depth >= baseline_depth

        if not (sustained and not_draining):
            self._queue_alert_active = False
            self._queue_last_warn_ts = None
            return None

        stuck_sec = now - self._queue_above_floor_since
        if not self._queue_alert_active:
            self._queue_alert_active = True
            self._queue_last_warn_ts = now
            return self._queue_warning(now, depth, baseline_depth, stuck_sec, first=True)

        if self._queue_last_warn_ts is None or (now - self._queue_last_warn_ts) >= self.warn_repeat_sec:
            self._queue_last_warn_ts = now
            return self._queue_warning(now, depth, baseline_depth, stuck_sec, first=False)

        return None

    def _queue_warning(self, now: float, depth: int, baseline_depth: int, stuck_sec: float, *,
                       first: bool) -> str:
        head = "🐢" if first else "🐢 (계속)"
        window_min = self.queue_stuck_window_sec / 60.0
        return (
            f"{head} [{format_clock(now)}] 대기큐 적체 — 최근 {window_min:.0f}분간 안 줄어듦 "
            f"({baseline_depth:,}건 → {depth:,}건, 바닥 {self.queue_backlog_floor:,}건 기준, "
            f"바닥 이상 {format_duration(stuck_sec)} 지속)"
        )

    # ── 종료 ──────────────────────────────────────────────────────────────

    def finish(
        self,
        reason: str,
        *,
        extra: Sequence[tuple[str, str]] | None = None,
    ) -> SessionReport:
        """세션을 닫고 보고서를 만든다. 종료 시점의 결손 여부가 여기서 정해진다."""
        if self.started_at is None:
            self.start()
        assert self.started_at is not None

        now = self._clock()
        self.ended_at = now
        has_events = (self.trade_count + self.quote_count) > 0
        gaps = list(self._gaps)
        trailing_fields = {}

        if self._sessions is not None:
            # 구간 프로필 경로 — tick() 의 경고와 같은 구간/임계/종류별 시계로 판정한다.
            spans = self._trailing_judged(now)
            violations = [span for span in spans if span[4] >= span[5]]
            healthy = has_events and not violations
            if has_events:
                # tick() 이 구간 경계에서 이미 미회복으로 닫은 구간은 다시 적지 않는다.
                # 경고 쪽 시작 시각은 (now - 침묵) 계산이라 부동소수 오차가 있어 ms 단위로 맞춘다.
                recorded = {(g.kind, round(g.start_ts, 3), round(g.end_ts, 3))
                            for g in gaps if not g.recovered}
                for kind, _session, start, end, _length, _threshold in violations:
                    if (kind, round(start, 3), round(end, 3)) not in recorded:
                        gaps.append(SilenceGap(start_ts=start, end_ts=end, recovered=False, kind=kind))
            from collector.kiwoom.market_sessions import TRADE
            worst = max(spans, key=lambda span: span[4] / span[5], default=None)
            kind = worst[0] if worst is not None else (self._judged_kinds[0] if self._judged_kinds else TRADE)
            last_kind = self._last_kind_ts(kind)
            trailing_wall = max(0.0, now - (last_kind if last_kind is not None else self.started_at))
            trailing_market = 0.0 if worst is None else worst[4]
            trailing_fields = dict(
                trailing_kind=kind,
                trailing_last_ts=last_kind,
                trailing_threshold_sec=None if worst is None else worst[5],
            )
            self._kind_gaps.clear()
        else:
            reference = self.last_event_ts if self.last_event_ts is not None else self.started_at
            trailing_wall = max(0.0, now - reference)

            # 판정용 길이: 마지막 수신 ~ 종료 구간 중 [09:00, 15:30] 과 겹치는 부분.
            window_start = max(reference, self._market_open_ts)
            window_end = min(now, self._market_close_ts)
            trailing_market = max(0.0, window_end - window_start)

            healthy = has_events and trailing_market < self.gap_threshold_sec

            if has_events and trailing_market >= self.gap_threshold_sec:
                # 끝내 회복되지 않은 마지막 구간도 목록에 남긴다.
                gaps.append(SilenceGap(start_ts=window_start, end_ts=window_end, recovered=False))
        self._open_gap_start = None

        return SessionReport(
            reason=reason,
            started_at=self.started_at,
            ended_at=now,
            first_event_ts=self.first_event_ts,
            last_event_ts=self.last_event_ts,
            trade_count=self.trade_count,
            quote_count=self.quote_count,
            gaps=gaps,
            gap_threshold_sec=self.gap_threshold_sec,
            trailing_wall_sec=trailing_wall,
            trailing_market_sec=trailing_market,
            healthy=healthy,
            queue_max_depth=self._queue_max_depth,
            queue_max_ts=self._queue_max_ts,
            extra=list(extra or []),
            **trailing_fields,
        )


class SessionLog:
    """
    콘솔 출력과 회전 파일 기록을 한 번에 하는 얇은 출력기.

    emit()   : 콘솔에 한 줄 + 파일에 한 줄. 메시지성 출력은 전부 이쪽이다.
    status() : 콘솔은 CR 로 덮어쓰는 휘발성 상태줄. 파일에는
               status_file_interval_sec 마다 한 번만 남긴다. 1초짜리 상태줄을
               그대로 파일에 쏟으면 정작 중요한 메시지가 묻히기 때문이다.

    파일 기록은 이벤트가 아니라 '메시지'와 '주기 샘플' 에만 붙는다.
    이벤트 1건당 I/O 는 어디에도 없다.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        backup_count: int = 5,
        status_file_interval_sec: float = 60.0,
        clock: Callable[[], float] = time.time,
        stream=None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._status_file_interval_sec = float(status_file_interval_sec)
        self._last_status_file_ts: float | None = None
        self._stream = stream if stream is not None else sys.stdout

        self._logger = logging.getLogger(f"kiwoom.session.{self.path.name}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        for old in list(self._logger.handlers):
            self._logger.removeHandler(old)
            old.close()

        handler = RotatingFileHandler(
            self.path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        self._logger.addHandler(handler)

    def emit(self, message: str) -> None:
        """콘솔과 파일 양쪽에 남긴다."""
        text = str(message)
        self._write_console(text + "\n")
        for line in text.splitlines() or [""]:
            self._logger.info(line)

    def emit_all(self, messages: Sequence[str]) -> None:
        for message in messages:
            self.emit(message)

    def status(self, message: str) -> None:
        """콘솔 상태줄(덮어쓰기). 파일에는 간격을 두고 샘플만 남긴다."""
        text = str(message)
        self._write_console("\r" + text)
        now = self._clock()
        if (
            self._last_status_file_ts is None
            or (now - self._last_status_file_ts) >= self._status_file_interval_sec
        ):
            self._last_status_file_ts = now
            self._logger.info(text)

    def end_status_line(self) -> None:
        """상태줄 위에 메시지를 찍기 전 줄을 마무리한다 (콘솔 전용)."""
        self._write_console("\n")

    def _write_console(self, text: str) -> None:
        if self._stream is None:
            return
        try:
            self._stream.write(text)
            self._stream.flush()
        except (ValueError, OSError):
            # 콘솔이 닫혀도 파일 기록은 계속돼야 한다.
            pass

    def close(self) -> None:
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
            handler.close()
