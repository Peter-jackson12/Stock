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

시계(clock)는 주입 가능하다. 키움 API 없이 테스트하기 위한 것이다.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
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
    recovered: bool   # False = 끝내 회복되지 않고 세션이 종료됨

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_ts - self.start_ts)

    def describe(self) -> str:
        tail = "" if self.recovered else ", 미회복"
        return (
            f"{format_clock(self.start_ts)} ~ {format_clock(self.end_ts)} "
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
    extra: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_events(self) -> int:
        return self.trade_count + self.quote_count

    @property
    def headline(self) -> str:
        if self.total_events == 0:
            return (
                "⚠️ 비정상 종료 — 수신 이벤트 0건 "
                f"(세션 {format_clock(self.started_at)}~{format_clock(self.ended_at)} 동안 단 1건도 없음)"
            )

        if not self.healthy:
            # 종료가 장 마감 뒤라면 벽시계 간격에는 '장이 끝나서 조용한 시간'이
            # 섞여 있다. 결손으로 의심하는 실제 길이를 따로 밝힌다.
            detail = "결손 의심"
            if abs(self.trailing_wall_sec - self.trailing_market_sec) >= 1.0:
                detail = f"장중 결손 {format_duration(self.trailing_market_sec)}, 결손 의심"
            return (
                f"⚠️ 비정상 종료 — 마지막 수신 {format_clock(self.last_event_ts)}, "
                f"이후 {format_duration(self.trailing_wall_sec)} 무이벤트 ({detail})"
            )

        if self.gaps:
            return (
                f"✅ 정상 종료 — 마지막 수신 {format_clock(self.last_event_ts)} "
                f"(단, 장중 침묵 {len(self.gaps)}건 감지 — 아래 세션 요약 확인)"
            )

        return f"✅ 정상 종료 — 마지막 수신 {format_clock(self.last_event_ts)}"

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
            f"  체결 총건수   : {self.trade_count:,} 건",
            f"  호가 총건수   : {self.quote_count:,} 건",
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


class SessionMonitor:
    """
    수신 상태를 추적하고 침묵을 판정한다.

    호출 규약:
      start()                  — 세션 시작 시 1회
      on_trade() / on_quote()  — 큐에 넣은 직후, 이벤트 1건마다 (핫패스)
      tick()                   — 기존 주기 루프에서 1초마다. 알릴 말이 있으면 문자열
      finish(reason)           — 종료 시 1회. SessionReport 반환
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        gap_threshold_sec: float = DEFAULT_GAP_THRESHOLD_SEC,
        warn_repeat_sec: float = DEFAULT_WARN_REPEAT_SEC,
        market_open: dtime = MARKET_OPEN,
        market_close: dtime = MARKET_CLOSE,
    ) -> None:
        self._clock = clock
        self.gap_threshold_sec = float(gap_threshold_sec)
        self.warn_repeat_sec = float(warn_repeat_sec)
        self._market_open = market_open
        self._market_close = market_close

        self.started_at: float | None = None
        self.ended_at: float | None = None

        # 핫패스가 만지는 필드들. 쓰는 스레드는 하나(키움 이벤트 스레드)이고
        # 읽는 쪽(1초 주기 루프)은 최신값을 놓쳐도 1초 뒤에 다시 본다.
        self.first_event_ts: float | None = None
        self.last_event_ts: float | None = None
        self.trade_count: int = 0
        self.quote_count: int = 0

        self._gaps: list[SilenceGap] = []
        self._open_gap_start: float | None = None
        self._last_warn_ts: float | None = None
        self._market_open_ts: float = 0.0
        self._market_close_ts: float = 0.0

    # ── 수명주기 ──────────────────────────────────────────────────────────

    def start(self) -> None:
        self.started_at = self._clock()
        session_day = datetime.fromtimestamp(self.started_at).date()
        self._market_open_ts = datetime.combine(session_day, self._market_open).timestamp()
        self._market_close_ts = datetime.combine(session_day, self._market_close).timestamp()

    # ── 핫패스 (이벤트 1건당 호출) ────────────────────────────────────────

    def on_trade(self) -> None:
        ts = self._clock()
        self.trade_count += 1
        self.last_event_ts = ts
        if self.first_event_ts is None:
            self.first_event_ts = ts

    def on_quote(self) -> None:
        ts = self._clock()
        self.quote_count += 1
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

        # 1) 침묵 중이었는데 새 이벤트가 들어왔다면 구간을 닫는다.
        if self._open_gap_start is not None:
            last = self.last_event_ts
            if last is not None and last > self._open_gap_start:
                gap = SilenceGap(start_ts=self._open_gap_start, end_ts=last, recovered=True)
                self._gaps.append(gap)
                self._open_gap_start = None
                self._last_warn_ts = None
                return f"✅ [{format_clock(now)}] 수신 재개 — 침묵 구간 {gap.describe()} 기록"

        # 2) 장중이 아니면 판정하지 않는다. 장 시작 전/장 마감 후 침묵은 정상이다.
        if not (self._market_open_ts <= now <= self._market_close_ts):
            return None

        # 3) 침묵 길이는 '장중 구간' 기준으로 잰다. 09:00 이전 시간은 세지 않는다.
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

        reference = self.last_event_ts if self.last_event_ts is not None else self.started_at
        trailing_wall = max(0.0, now - reference)

        # 판정용 길이: 마지막 수신 ~ 종료 구간 중 [09:00, 15:30] 과 겹치는 부분.
        window_start = max(reference, self._market_open_ts)
        window_end = min(now, self._market_close_ts)
        trailing_market = max(0.0, window_end - window_start)

        has_events = (self.trade_count + self.quote_count) > 0
        healthy = has_events and trailing_market < self.gap_threshold_sec

        gaps = list(self._gaps)
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
            extra=list(extra or []),
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
