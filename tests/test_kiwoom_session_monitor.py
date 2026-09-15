"""
tests/test_kiwoom_session_monitor.py — 수집 세션 감시기 (키움 API 없이 검증)

2026-09-14 전 종목 수집에서 실제로 일어난 일:
  raw_trades/raw_quotes 의 마지막 행은 14:41:18 인데, 콘솔은 54분 뒤인
  15:35:00 에 "🔔 [15:35:00 장 마감 감지] 전 종목 수집을 정상 종료합니다." 를
  찍었다. 원인(PC 절전)은 따로 조치했다. 남은 문제는 **코드가 54분치 결손을
  '정상 종료' 라고 보고했다**는 것이다.

그래서 여기서 고정하는 것은 "수집이 잘 되는가" 가 아니라
**"수집이 끊겼을 때 그 사실이 종료 보고에 드러나는가"** 다.
그리고 그 반대편 — 아무 문제 없는 날을 결손이라고 떠들지 않는가 — 도 같이 고정한다.
장 마감(15:30)과 데몬 종료(15:35) 사이 5분은 원래 조용하기 때문이다.

키움 OCX / PyQt5 는 전혀 필요 없다. 시계를 주입하고 가짜 이벤트를 넣는다.

실행:
    uv run pytest tests/test_kiwoom_session_monitor.py -v
"""

from __future__ import annotations

import io
import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collector.kiwoom.session_monitor import (                         # noqa: E402
    SessionLog,
    SessionMonitor,
    format_duration,
)

# 사건 당일. 이 날짜 위에서 09:00~15:30 장 구간이 잡힌다.
SESSION_DAY = datetime(2026, 9, 14)


def at(hour: int, minute: int, second: int = 0) -> float:
    """당일 HH:MM:SS 의 epoch 초."""
    return SESSION_DAY.replace(hour=hour, minute=minute, second=second).timestamp()


class FakeClock:
    """주입 가능한 시계. 테스트가 시간을 직접 옮긴다."""

    def __init__(self, start_ts: float) -> None:
        self.now = start_ts

    def __call__(self) -> float:
        return self.now

    def set(self, ts: float) -> None:
        self.now = ts

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeFeed:
    """
    가짜 이벤트 소스 + 가짜 주기 루프.

    실제 수집기는 키움 콜백에서 on_trade/on_quote 를 부르고, 별도 스레드의
    1초 루프에서 tick() 을 부른다. 여기서는 그 두 경로를 한 스레드에서
    시간 순서대로 재현한다 — 감시기가 보는 것은 어차피 시계와 카운터뿐이다.
    """

    def __init__(self, monitor: SessionMonitor, clock: FakeClock) -> None:
        self.monitor = monitor
        self.clock = clock
        self.notices: list[str] = []

    def emit_event(self, ts: float, kind: str = "trade") -> None:
        """ts 시각에 이벤트 1건이 큐에 들어간 것으로 친다."""
        self.clock.set(ts)
        if kind == "trade":
            self.monitor.on_trade()
        else:
            self.monitor.on_quote()

    def run_until(self, end_ts: float, *, step: float = 1.0, events: list[float] | None = None) -> None:
        """
        1초 주기 루프를 end_ts 까지 돌린다. events 에 들어 있는 시각에는
        그 초의 tick 직전에 이벤트가 1건 도착한 것으로 처리한다.
        """
        pending = sorted(events or [])
        while self.clock.now <= end_ts:
            while pending and pending[0] <= self.clock.now:
                ts = pending.pop(0)
                saved = self.clock.now
                self.emit_event(ts)
                self.clock.set(saved)
            notice = self.monitor.tick()
            if notice:
                self.notices.append(notice)
            self.clock.advance(step)

    @property
    def warnings(self) -> list[str]:
        return [n for n in self.notices if n.startswith("⚠️")]

    @property
    def recoveries(self) -> list[str]:
        return [n for n in self.notices if "수신 재개" in n]


def build(start_hour: int = 8, start_minute: int = 50, **kwargs) -> tuple[SessionMonitor, FakeClock, FakeFeed]:
    clock = FakeClock(at(start_hour, start_minute))
    monitor = SessionMonitor(clock=clock, **kwargs)
    monitor.start()
    return monitor, clock, FakeFeed(monitor, clock)


# ── 1. 정상 종료 ─────────────────────────────────────────────────────────


def test_갭이_없으면_정상_종료로_보고한다():
    """마지막 체결이 장 마감 직전이면, 15:35 종료까지의 5분 공백은 결손이 아니다."""
    monitor, clock, feed = build()

    feed.emit_event(at(9, 0, 1))
    feed.emit_event(at(15, 29, 58), kind="quote")

    clock.set(at(15, 35, 0))
    report = monitor.finish("장 마감 (15:35)")

    assert report.healthy is True
    assert report.headline.startswith("✅ 정상 종료")
    assert "비정상" not in report.headline
    assert report.gaps == []
    # 벽시계로는 5분 2초가 비었지만, 그중 장중 구간은 2초뿐이다.
    assert report.trailing_wall_sec == pytest.approx(302.0)
    assert report.trailing_market_sec == pytest.approx(2.0)

    # 판정 결과("정상 종료")만 찍고 그 근거(갭이 실제로 얼마였는지)를 안 찍으면,
    # 시각 파싱이 깨져 갭이 늘 0으로 나와도 로그는 영원히 정상이라고만 말한다.
    # 정상 종료에도 갭 수치·임계값이 실제로 문자열에 박혀야 한다.
    assert format_duration(302.0) in report.headline   # "5분 2초" — 벽시계 갭
    assert format_duration(2.0) in report.headline     # "2초" — 장중 결손 판정 갭
    assert "120초" in report.headline                  # 무엇과 비교해 정상인지(임계값)

    summary = "\n".join(report.lines())
    assert format_duration(302.0) in summary
    assert format_duration(2.0) in summary
    assert "120초" in summary


# ── 2. 결손을 정상이라고 말하지 않는다 (사건 재현) ───────────────────────


def test_마지막_이벤트_이후_임계_초과면_정상_종료라고_말하지_않는다():
    """2026-09-14 재현: 14:41:18 마지막 수신, 15:35:00 종료."""
    monitor, clock, feed = build()

    feed.emit_event(at(9, 0, 1))
    feed.emit_event(at(14, 41, 18))

    clock.set(at(15, 35, 0))
    report = monitor.finish("장 마감 (15:35)")

    assert report.healthy is False
    # 이것이 이 테스트의 존재 이유다.
    # ("비정상 종료" 가 "정상 종료" 를 부분 문자열로 품으므로 ✅ 머리표로 가른다)
    assert "✅" not in report.headline
    assert "비정상 종료" in report.headline

    # 마지막 수신 시각과 결손 길이가 문구에 그대로 드러나야 한다.
    assert "14:41:18" in report.headline
    assert format_duration(at(15, 35, 0) - at(14, 41, 18)) in report.headline   # 53분 42초
    assert format_duration(at(15, 30, 0) - at(14, 41, 18)) in report.headline   # 48분 42초 (장중)

    assert report.trailing_wall_sec == pytest.approx(3222.0)
    assert report.trailing_market_sec == pytest.approx(2922.0)

    # 회복되지 못한 구간이 요약의 침묵 목록에도 남는다.
    assert len(report.gaps) == 1
    gap = report.gaps[0]
    assert gap.recovered is False
    assert gap.start_ts == at(14, 41, 18)
    assert gap.end_ts == at(15, 30, 0)     # 판정 구간은 장 마감에서 끊는다


def test_임계_직전과_직후의_경계():
    """119초는 정상, 120초는 결손. 임계는 말 그대로 임계다."""
    for silence, expected_healthy in ((119, True), (120, False)):
        monitor, clock, feed = build(gap_threshold_sec=120)
        feed.emit_event(at(10, 0, 0))
        clock.set(at(10, 0, 0) + silence)
        report = monitor.finish("사용자 중단 (Ctrl+C)")
        assert report.healthy is expected_healthy, f"침묵 {silence}초"


# ── 3. 장중 침묵 경고 ────────────────────────────────────────────────────


def test_장중_임계_초과_침묵은_경고하고_회복되면_구간으로_남는다():
    monitor, clock, feed = build(gap_threshold_sec=120, warn_repeat_sec=60)

    feed.emit_event(at(9, 59, 0))
    clock.set(at(9, 59, 1))
    # 09:59:00 이후 5분간 침묵하다가 10:04:00 에 수신 재개
    feed.run_until(at(10, 6, 0), events=[at(10, 4, 0)])

    assert feed.warnings, "장중 5분 침묵인데 경고가 한 번도 나오지 않았다"

    # 첫 경고는 임계(120초)를 넘긴 직후여야 한다 — 09:59:00 + 120초
    first = feed.warnings[0]
    assert "[10:01:00]" in first
    assert "09:59:00" in first          # 마지막 수신 시각
    assert "결손 의심" in first

    # 침묵이 이어지는 동안 반복 경고가 나온다 (한 번 찍고 묻히지 않는다)
    assert len(feed.warnings) >= 2
    assert any("(계속)" in w for w in feed.warnings)

    # 수신이 재개되면 구간이 닫히고 알림이 나간다
    assert len(feed.recoveries) == 1
    assert "09:59:00 ~ 10:04:00" in feed.recoveries[0]

    report = monitor.finish("장 마감 (15:35)")
    recovered = [g for g in report.gaps if g.recovered]
    assert len(recovered) == 1
    assert recovered[0].start_ts == at(9, 59, 0)
    assert recovered[0].end_ts == at(10, 4, 0)
    assert recovered[0].duration_sec == pytest.approx(300.0)


def test_임계_미만_침묵은_경고도_구간기록도_아니다():
    monitor, clock, feed = build(gap_threshold_sec=120)

    feed.emit_event(at(10, 0, 0))
    clock.set(at(10, 0, 1))
    feed.run_until(at(10, 5, 0), events=[at(10, 1, 30), at(10, 3, 0), at(10, 4, 30)])

    assert feed.warnings == []
    assert monitor.finish("장 마감 (15:35)").gaps == []


# ── 4. 장 밖의 침묵은 경고 대상이 아니다 ─────────────────────────────────


def test_장_시작_전_침묵은_경고하지_않는다():
    """08:50 에 떠서 09:00 까지 이벤트가 없는 것은 결손이 아니라 그냥 장 전이다."""
    monitor, clock, feed = build(start_hour=8, start_minute=50)

    feed.run_until(at(8, 59, 59))

    assert feed.notices == [], f"장 시작 전인데 알림이 나왔다: {feed.notices}"


def test_장_마감_후_침묵은_경고하지_않는다():
    monitor, clock, feed = build()

    feed.emit_event(at(15, 29, 59))
    clock.set(at(15, 30, 1))
    feed.run_until(at(15, 35, 0))

    assert feed.warnings == [], f"장 마감 후인데 경고가 나왔다: {feed.warnings}"


def test_장_시작_직후_침묵은_장_시작_시각부터_센다():
    """08:50 부터 조용했어도 09:02 에 '12시간째 침묵' 같은 소리를 하면 안 된다."""
    monitor, clock, feed = build(start_hour=8, start_minute=50, gap_threshold_sec=120)

    feed.run_until(at(9, 3, 0))

    assert feed.warnings, "장 시작 후 3분간 이벤트 0건인데 경고가 없다"
    first = feed.warnings[0]
    assert "[09:02:00]" in first        # 09:00 + 120초
    assert "2분 0초" in first
    assert "이벤트 0건" in first


# ── 5. 이벤트가 아예 0건인 날 ────────────────────────────────────────────


def test_이벤트가_0건이면_정상_종료가_아니다():
    monitor, clock, feed = build()

    clock.set(at(15, 35, 0))
    report = monitor.finish("장 마감 (15:35)")

    assert report.total_events == 0
    assert report.healthy is False
    assert "✅" not in report.headline
    assert "비정상 종료" in report.headline
    assert "0건" in report.headline

    # 첫/마지막 이벤트가 없어도 요약이 터지지 않고 '없음' 으로 나온다
    text = "\n".join(report.lines())
    assert "첫 이벤트     : 없음" in text
    assert "마지막 이벤트 : 없음" in text
    assert "체결 총건수   : 0 건" in text


def test_장_시작_전에_중단한_0건_세션도_보고는_된다():
    """08:55 Ctrl+C. 결손은 아니지만 0건은 0건이라고 말해야 한다."""
    monitor, clock, feed = build(start_hour=8, start_minute=50)

    clock.set(at(8, 55, 0))
    report = monitor.finish("사용자 중단 (Ctrl+C)")

    assert report.healthy is False
    assert "✅" not in report.headline
    assert "0건" in report.headline
    assert report.gaps == []           # 장이 열리지도 않았으니 결손 구간은 없다


# ── 6. 세션 요약 ─────────────────────────────────────────────────────────


def test_세션_요약에_요구된_항목이_모두_들어간다():
    monitor, clock, feed = build()

    feed.emit_event(at(9, 0, 1))                       # 체결 1
    feed.emit_event(at(9, 0, 2), kind="quote")         # 호가 1
    clock.set(at(9, 0, 3))
    feed.run_until(at(9, 10, 0), events=[at(9, 6, 0)])  # 중간에 5분 침묵 1건
    feed.emit_event(at(14, 41, 18))

    clock.set(at(15, 35, 0))
    report = monitor.finish(
        "장 마감 (15:35)",
        extra=[("DB 반영 건수  ", "체결 3 건 / 호가 1 건")],
    )
    text = "\n".join(report.lines())

    assert "세션 시작     : 2026-09-14 08:50:00" in text
    assert "세션 종료     : 2026-09-14 15:35:00" in text
    assert "첫 이벤트     : 09:00:01" in text
    assert "마지막 이벤트 : 14:41:18" in text
    assert "체결 총건수   : 3 건" in text
    assert "호가 총건수   : 1 건" in text
    assert "종료 사유     : 장 마감 (15:35)" in text
    assert "DB 반영 건수   : 체결 3 건 / 호가 1 건" in text

    # 침묵 구간 목록: 회복된 09:00:02~09:06:00 과 미회복 14:41:18~15:30:00
    assert "임계(120초) 초과 침묵 구간 : 2건" in text
    assert "09:00:02 ~ 09:06:00" in text
    assert "14:41:18 ~ 15:30:00" in text
    assert "미회복" in text


def test_회복된_침묵이_있으면_정상_종료에도_꼬리표가_붙는다():
    monitor, clock, feed = build()

    feed.emit_event(at(10, 0, 0))
    clock.set(at(10, 0, 1))
    feed.run_until(at(10, 10, 0), events=[at(10, 5, 0)])
    feed.emit_event(at(15, 29, 59))

    clock.set(at(15, 35, 0))
    report = monitor.finish("장 마감 (15:35)")

    assert report.healthy is True
    assert report.headline.startswith("✅ 정상 종료")
    assert "장중 침묵 1건 감지" in report.headline


def test_format_duration():
    assert format_duration(0) == "0초"
    assert format_duration(42) == "42초"
    assert format_duration(2922) == "48분 42초"
    assert format_duration(3222) == "53분 42초"
    assert format_duration(3785) == "1시간 3분 5초"
    assert format_duration(-5) == "0초"


# ── 7. 회전 파일 로그 ────────────────────────────────────────────────────


def test_콘솔에_나간_메시지는_파일에도_남는다(tmp_path):
    stream = io.StringIO()
    log = SessionLog(tmp_path / "sess.log", stream=stream, clock=FakeClock(at(9, 0)))
    try:
        log.emit("🔑 접속 시도")
        log.emit("⚠️ 비정상 종료 — 마지막 수신 14:41:18")
        log.emit_all(["📋 세션 요약", "  체결 총건수 : 1,234 건"])
    finally:
        log.close()

    console = stream.getvalue()
    file_text = (tmp_path / "sess.log").read_text(encoding="utf-8")

    for line in ("🔑 접속 시도", "⚠️ 비정상 종료 — 마지막 수신 14:41:18",
                 "📋 세션 요약", "  체결 총건수 : 1,234 건"):
        assert line in console
        assert line in file_text


def test_주기_상태줄은_콘솔에만_매초_찍히고_파일에는_간격을_두고_남는다(tmp_path):
    """이벤트/초당 동기 I/O 를 피하기 위한 절충. 메시지는 전부 남지만 상태줄은 샘플만."""
    clock = FakeClock(at(9, 0))
    stream = io.StringIO()
    log = SessionLog(tmp_path / "sess.log", stream=stream, clock=clock,
                     status_file_interval_sec=60.0)
    try:
        for _ in range(180):                      # 3분치 1초 상태줄
            log.status(f"⏱️ 적재 현황 {clock.now:.0f}")
            clock.advance(1.0)
    finally:
        log.close()

    assert stream.getvalue().count("\r") == 180   # 콘솔은 매초 갱신
    file_lines = [l for l in (tmp_path / "sess.log").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(file_lines) == 3                   # 파일은 60초에 한 줄


def test_파일_로그는_회전한다(tmp_path):
    path = tmp_path / "sess.log"
    log = SessionLog(path, stream=None, max_bytes=2048, backup_count=2)
    try:
        for i in range(400):
            log.emit(f"⏱️ 적재 현황 라인 {i:04d} — 회전 확인용 패딩 문자열")
    finally:
        log.close()

    assert path.exists()
    backups = sorted(p.name for p in tmp_path.glob("sess.log.*"))
    assert backups == ["sess.log.1", "sess.log.2"], f"회전 파일이 없다: {backups}"
    assert "라인 0399" in path.read_text(encoding="utf-8")   # 최신 내용은 본 파일에


def test_세션_보고서를_그대로_파일에_남길_수_있다(tmp_path):
    """수집기의 종료 경로가 하는 일 그대로: headline + lines() 를 파일에."""
    monitor, clock, feed = build()
    feed.emit_event(at(14, 41, 18))
    clock.set(at(15, 35, 0))
    report = monitor.finish("장 마감 (15:35)")

    log = SessionLog(tmp_path / "sess.log", stream=None)
    try:
        log.emit(report.headline)
        log.emit_all(report.lines())
    finally:
        log.close()

    file_text = (tmp_path / "sess.log").read_text(encoding="utf-8")
    assert "비정상 종료" in file_text
    assert "14:41:18" in file_text
    assert "임계(120초) 초과 침묵 구간 : 1건" in file_text


# ── 8. 대기큐 적체 감시 (발견 #13) ────────────────────────────────────────
#
# 2026-09-15 실측: 09:16:55 에 대기큐 203,692건까지 쌓였다가 09:51 에 빠졌다.
# 절대값으로 임계를 걸면 이 정상적인 개장 폭주도 매일 걸린다. 그래서 "바닥
# 이상을 일정 시간 유지하며 줄지 않는가"로 판정한다 — 아래 테스트는 빠르게
# 돌리려고 floor/window 를 작게 잡아 주입한다 (기본값은 20,000건/300초).


def test_대기큐가_바닥_아래면_적체_경보가_없다():
    monitor, clock, feed = build(queue_backlog_floor=100, queue_stuck_window_sec=10)
    notices = []
    for depth in [0, 20, 50, 80, 99, 50, 10]:
        clock.advance(1.0)
        notice = monitor.sample_queue_depth(depth)
        if notice:
            notices.append(notice)
    assert notices == []


def test_바닥_이상이_줄지_않으면_적체_경보가_뜬다():
    """개장 폭주가 빠지지 않고 그대로 얹혀 있는 상태 — 알려야 한다."""
    monitor, clock, feed = build(queue_backlog_floor=100, queue_stuck_window_sec=10, warn_repeat_sec=5)
    notices = []
    for _ in range(30):
        clock.advance(1.0)
        notice = monitor.sample_queue_depth(500)
        if notice:
            notices.append(notice)

    assert notices, "10초 넘게 안 줄어드는 적체인데 경보가 없다"
    assert "🐢" in notices[0]
    assert "500" in notices[0]
    assert "100" in notices[0]                 # 바닥값도 근거로 남는다
    assert len(notices) >= 3, "warn_repeat_sec(5초)마다 되풀이돼야 한다"


def test_적체가_계속_줄고_있으면_경보가_없다():
    """2026-09-15 09:00~09:51 패턴 재현 — 쌓였다가도 계속 빠지면 정상이다."""
    monitor, clock, feed = build(queue_backlog_floor=100, queue_stuck_window_sec=10)
    notices = []
    depth = 1000
    for _ in range(40):
        clock.advance(1.0)
        depth = max(0, depth - 30)      # 꾸준히 감소(드레인 중)
        notice = monitor.sample_queue_depth(depth)
        if notice:
            notices.append(notice)
    assert notices == [], f"계속 빠지는 중인데 경보가 떴다: {notices}"


def test_적체가_풀렸다_다시_쌓이면_경보가_다시_뜬다():
    monitor, clock, feed = build(queue_backlog_floor=100, queue_stuck_window_sec=10, warn_repeat_sec=100)

    first_round = []
    for _ in range(15):
        clock.advance(1.0)
        n = monitor.sample_queue_depth(500)
        if n:
            first_round.append(n)
    assert len(first_round) == 1

    clock.advance(1.0)
    assert monitor.sample_queue_depth(10) is None   # 바닥 아래로 빠짐 → 상태 리셋

    second_round = []
    for _ in range(15):
        clock.advance(1.0)
        n = monitor.sample_queue_depth(700)
        if n:
            second_round.append(n)
    assert len(second_round) == 1, "리셋 후 다시 적체되면 반복 간격과 무관하게 새로 떠야 한다"


def test_장중_대기큐_최대값과_시각이_세션_요약에_남는다():
    """종료 시점 0건만 보고 '적체 없었다' 고 결론 낸 것(ARCHITECTURE_V2.md 진행로그
    6차)이 틀렸다는 정정 — 장중 최대값을 별도로 추적해 보고서에 남긴다."""
    monitor, clock, feed = build()

    clock.advance(1.0)
    monitor.sample_queue_depth(500)
    clock.advance(1.0)
    monitor.sample_queue_depth(203_692)
    clock.advance(1.0)
    monitor.sample_queue_depth(0)               # 종료 시점엔 0으로 빠짐

    clock.set(at(15, 35, 0))
    report = monitor.finish("장 마감 (15:35)")

    assert report.queue_max_depth == 203_692
    assert report.queue_max_ts is not None

    text = "\n".join(report.lines())
    assert "장중 대기큐 최대" in text
    assert "203,692" in text
