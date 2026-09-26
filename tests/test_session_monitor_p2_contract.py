"""tests/test_session_monitor_p2_contract.py — PIPELINE_AUDIT 2026-09-26 collector P2 회귀

키움 OCX/로그인/실제 수집 없이 시계를 주입해 두 감사 발견의 수정 계약을 고정한다.

A. 명시적 시장 구간 프로필의 침묵 판정
   - 체결 침묵은 체결 수신으로만, 호가 침묵은 호가 수신으로만 회복된다.
   - 경고·회복·종료 보고가 같은 구간/임계/종류별 시계를 쓴다.
   - NOT_EXPECTED 구간과 구간 밖 시간은 결손으로 세지 않는다.
   - sessions=None(legacy) 경로는 기존 any-event 의미를 그대로 유지한다.

B. 대기큐 적체 경보
   - bounded raw-v2 큐의 바닥은 실제 capacity 에서 정하고, 도달 불가능한 바닥은 거부한다.
   - 지속 시간은 바닥 이상이 처음 관측된 시각부터 재고, 기준점은 창 경계 직전 표본이다.
   - 불규칙한 표본 간격에서도 정확히 판정하고, 실제로 줄고 있으면 경보하지 않는다.

실행:
    uv run pytest tests/test_session_monitor_p2_contract.py -v
"""
from __future__ import annotations

import inspect
from datetime import date, datetime, time as dtime

import pytest

from collector.kiwoom.market_sessions import (
    PROFILE_NXT_AFTERMARKET,
    PROFILE_NXT_FULL,
    PROFILE_NXT_PREMARKET,
    QUOTE,
    TRADE,
    UNJUDGED,
    MarketSession,
)
from collector.kiwoom.session_monitor import (
    DEFAULT_QUEUE_BACKLOG_FLOOR,
    SessionMonitor,
    queue_backlog_floor_for_capacity,
)


class Clock:
    def __init__(self, ts): self.now = ts
    def __call__(self): return self.now
    def set(self, ts): self.now = ts
    def advance(self, seconds): self.now += seconds


def at(hour, minute=0, second=0.0):
    whole = int(second)
    base = datetime.combine(date.today(), dtime(hour, minute, whole)).timestamp()
    return base + (second - whole)


def monitor_at(ts, **kwargs):
    clock = Clock(ts)
    kwargs.setdefault("gap_threshold_sec", 120)
    kwargs.setdefault("silence_stop_sec", 600)
    monitor = SessionMonitor(clock=clock, **kwargs)
    monitor.start()
    monitor.mark_reception_expected()
    return monitor, clock


# ── A. 종류별 시계로만 회복한다 ────────────────────────────────────────────

def test_호가만_재개되면_체결_침묵은_회복되지_않는다():
    """감사 발견: 회복 판정이 공통 last_event_ts 라 호가 1건이 체결 침묵 구간을 닫았다."""
    m, clock = monitor_at(at(16, 0), sessions=PROFILE_NXT_AFTERMARKET)
    m.on_trade()
    clock.set(at(16, 10))
    warning = m.tick()
    assert warning is not None and "신규 체결 없음" in warning and "결손 의심" in warning

    notices = []
    for _ in range(30):                 # 이후 30분 동안 호가만 계속 들어온다
        clock.advance(60)
        m.on_quote()
        notices.append(m.tick())
    assert not any(n and "수신 재개" in n for n in notices)
    assert all(g.kind != TRADE or not g.recovered for g in m._gaps)
    assert TRADE in m._kind_gaps                          # 체결 침묵 구간은 계속 열려 있다

    report = m.finish("테스트")
    assert report.healthy is False
    assert report.trailing_kind == TRADE
    assert "마지막 체결 16:00:00" in report.headline
    assert any(g.kind == TRADE and not g.recovered for g in report.gaps)


def test_체결이_재개되면_체결_침묵이_회복된다():
    m, clock = monitor_at(at(16, 0), sessions=PROFILE_NXT_AFTERMARKET)
    m.on_trade()
    clock.set(at(16, 10))
    assert m.tick() is not None
    clock.set(at(16, 12))
    m.on_trade()
    clock.advance(1)
    notice = m.tick()
    assert notice is not None and "체결 수신 재개" in notice
    gap = m._gaps[-1]
    assert gap.kind == TRADE and gap.recovered is True
    assert gap.start_ts == pytest.approx(at(16, 0)) and gap.end_ts == pytest.approx(at(16, 12))
    assert m.finish("테스트").healthy is True


def test_종료_권고는_호가로_풀리지_않고_체결로만_풀린다():
    m, clock = monitor_at(at(16, 0), sessions=PROFILE_NXT_AFTERMARKET)
    m.on_trade()
    clock.set(at(16, 10, 1))
    reason = m.silence_stop_reason()
    assert reason is not None and "마지막 체결 16:00:00" in reason
    m.on_quote(); clock.advance(1); m.tick()
    assert m._stop_advised_reference is not None          # 호가는 권고를 풀지 않는다
    assert m.silence_stop_reason() is None                # 같은 권고를 반복하지 않는다
    m.on_trade(); clock.advance(1); m.tick()
    assert m._stop_advised_reference is None              # 체결 수신만 회복이다


def test_호가를_판정하는_프로필에서는_호가_침묵이_호가로만_회복된다():
    quote_judged = (MarketSession("NXT", "시험 구간", dtime(10, 0), dtime(11, 0),
                                  trade_gap=UNJUDGED, quote_gap=60.0),)
    m, clock = monitor_at(at(10, 0), sessions=quote_judged)
    assert m._judged_kinds == (QUOTE,)
    m.on_quote()
    clock.set(at(10, 1, 1))
    warning = m.tick()
    assert warning is not None and "신규 호가 없음" in warning
    m.on_trade(); clock.advance(1)
    assert m.tick() is None                               # 체결은 호가 침묵을 회복시키지 않는다
    assert QUOTE in m._kind_gaps
    m.on_quote(); clock.advance(1)
    notice = m.tick()
    assert notice is not None and "호가 수신 재개" in notice
    assert m._gaps[-1].kind == QUOTE and m._gaps[-1].recovered


def test_legacy_경로는_기존_any_event_의미를_유지한다():
    """sessions=None 은 문서화된 기본 운영 경로다 — 공통 last_event_ts 로 경고하고 회복한다."""
    m, clock = monitor_at(at(10, 0))
    m.on_trade()
    clock.set(at(10, 2, 1))
    assert m.tick() is not None
    m.on_quote(); clock.advance(1)
    notice = m.tick()
    assert notice is not None and notice.startswith("✅") and "수신 재개" in notice
    report = m.finish("테스트")
    assert report.trailing_kind is None and "마지막 수신" in report.headline


def test_감시_시작_전_시간은_침묵으로_세지_않는다():
    # 17시에 감시를 시작했는데 애프터마켓 개장(15:40)부터 세면 즉시 결손으로 오판한다.
    m, clock = monitor_at(at(17, 0), sessions=PROFILE_NXT_AFTERMARKET)
    clock.advance(1)
    assert m.tick() is None
    clock.set(at(17, 9, 59))
    assert m.tick() is None
    clock.set(at(17, 10, 0))
    warning = m.tick()
    assert warning is not None and "체결 0건" in warning


# ── A. 종료 판정도 같은 프로필 의미를 쓴다 ───────────────────────────────

def test_애프터마켓_종료는_고정_정규장_창이_아니라_프로필로_판정한다():
    """감사 발견: finish() 가 09:00~15:30 창으로 잘라 애프터마켓 결손을 0초로 봤다."""
    m, clock = monitor_at(at(16, 0), sessions=PROFILE_NXT_AFTERMARKET)
    m.on_trade()
    for _ in range(180):
        clock.advance(60)
        m.on_quote()                                      # 호가만 3시간
    report = m.finish("테스트")
    assert report.healthy is False
    assert report.trailing_market_sec == pytest.approx(3 * 3600)
    assert report.trailing_threshold_sec == pytest.approx(600)
    assert "임계 600초" in report.headline


def test_프리마켓_종료도_프로필로_판정한다():
    m, clock = monitor_at(at(8, 0), sessions=PROFILE_NXT_PREMARKET)
    m.on_trade()
    clock.set(at(8, 49))
    report = m.finish("테스트")
    assert report.healthy is False                        # 49분 체결 침묵(임계 600초)
    assert report.trailing_market_sec == pytest.approx(49 * 60)


def test_구간_마감_뒤_종료_시간은_결손으로_세지_않는다():
    m, clock = monitor_at(at(19, 50), sessions=PROFILE_NXT_AFTERMARKET)
    clock.set(at(19, 59, 30))
    m.on_trade()
    clock.set(at(20, 30))                                 # 애프터마켓 마감 30분 뒤 종료
    report = m.finish("테스트")
    assert report.healthy is True
    assert report.trailing_market_sec == pytest.approx(30)
    assert report.trailing_wall_sec == pytest.approx(30 * 60 + 30)


def test_경고와_종료_판정이_같은_기준을_쓴다():
    for silence, expected_warn in ((599, False), (600, True)):
        m, clock = monitor_at(at(16, 0), sessions=PROFILE_NXT_AFTERMARKET)
        m.on_trade()
        clock.advance(silence)
        m.on_quote()
        warned = m.tick() is not None
        report = m.finish("테스트")
        assert warned is expected_warn, silence
        assert report.healthy is (not expected_warn), silence


# ── A. NOT_EXPECTED 구간 경계 ────────────────────────────────────────────

def test_NOT_EXPECTED_전환_구간의_침묵은_결손이_아니다():
    m, clock = monitor_at(at(15, 0), sessions=PROFILE_NXT_FULL)
    clock.set(at(15, 19))
    m.on_trade()                                          # 메인 마감 1분 전 마지막 체결
    for minute in range(20, 40):
        clock.set(at(15, minute))
        assert m.tick() is None                           # 15:20~15:40 체결 NOT_EXPECTED
    clock.set(at(15, 39, 59))
    report = m.finish("테스트")
    assert report.healthy is True
    assert report.trailing_market_sec == pytest.approx(60)   # 메인 안의 60초만 센다


def test_판정_구간이_닫히면_열린_침묵은_그_경계에서_미회복으로_닫힌다():
    m, clock = monitor_at(at(15, 0), sessions=PROFILE_NXT_FULL)
    clock.set(at(15, 5))
    m.on_trade()
    clock.set(at(15, 7, 1))
    assert m.tick() is not None                           # 메인 안 체결 침묵 경고
    clock.set(at(15, 20))
    notice = m.tick()
    assert notice is not None and "판정 구간 종료" in notice
    gap = m._gaps[-1]
    assert gap.kind == TRADE and gap.recovered is False
    assert gap.end_ts == pytest.approx(at(15, 20))        # 전환 구간 시간을 붙이지 않는다
    for minute in range(21, 40):
        clock.set(at(15, minute))
        assert m.tick() is None

    # 애프터마켓은 개장 시각부터 새로 잰다 — 임계 600초 경계.
    clock.set(at(15, 49, 59))
    assert m.tick() is None
    clock.set(at(15, 50, 0))
    assert m.tick() is not None

    report = m.finish("테스트")
    assert report.healthy is False
    spans = [(g.start_ts, g.end_ts) for g in report.gaps if not g.recovered]
    assert sum(s == pytest.approx(at(15, 5)) and e == pytest.approx(at(15, 20)) for s, e in spans) == 1
    assert any(s == pytest.approx(at(15, 40)) and e == pytest.approx(at(15, 50)) for s, e in spans)
    # 전환 구간(15:20~15:40)은 어떤 미회복 구간에도 들어가지 않는다.
    assert not any(s < at(15, 40) and e > at(15, 20) for s, e in spans)


def test_구간을_넘어_도착한_체결은_이전_구간을_회복으로_기록하지_않는다():
    m, clock = monitor_at(at(15, 0), sessions=PROFILE_NXT_FULL)
    clock.set(at(15, 5))
    m.on_trade()
    clock.set(at(15, 7, 1))
    assert m.tick() is not None
    clock.set(at(15, 45))
    m.on_trade()                                          # 애프터마켓 첫 체결 — tick 이 전환 중 돌지 않았다
    notice = m.tick()
    assert notice is not None and "판정 구간 종료" in notice
    gap = m._gaps[-1]
    assert gap.recovered is False and gap.end_ts == pytest.approx(at(15, 20))


# ── B. 적체 바닥은 실제 capacity 에서 정한다 ─────────────────────────────

def test_기본_바닥은_실제_raw_v2_큐_수용량_안에_있다():
    from collector.kiwoom.live_capture import LiveRawCapture, QUEUE_BATCH_SIZE, QUEUE_CAPACITY
    assert inspect.signature(LiveRawCapture.__init__).parameters["capacity"].default == QUEUE_CAPACITY
    floor = queue_backlog_floor_for_capacity(QUEUE_CAPACITY, QUEUE_BATCH_SIZE)
    assert floor == QUEUE_CAPACITY // 2
    assert QUEUE_BATCH_SIZE < floor <= QUEUE_CAPACITY
    # 감사 발견: 기존 기본값 20,000 은 capacity 8,192 + in-flight batch 512 에 도달할 수 없었다.
    assert DEFAULT_QUEUE_BACKLOG_FLOOR > QUEUE_CAPACITY + QUEUE_BATCH_SIZE
    m = SessionMonitor(queue_capacity=QUEUE_CAPACITY, queue_batch_size=QUEUE_BATCH_SIZE)
    assert m.queue_backlog_floor == floor


def test_capacity_보다_큰_바닥은_설정_오류다():
    with pytest.raises(ValueError, match="unreachable"):
        SessionMonitor(queue_backlog_floor=20_000, queue_capacity=8192, queue_batch_size=512)
    with pytest.raises(ValueError):
        SessionMonitor(queue_capacity=8192)               # capacity 와 batch 는 함께 준다
    with pytest.raises(ValueError):
        queue_backlog_floor_for_capacity(1024, 512)       # batch 변동과 구분할 수 없는 바닥
    assert SessionMonitor(queue_backlog_floor=8192, queue_capacity=8192,
                          queue_batch_size=512).queue_backlog_floor == 8192


def test_capacity_를_모르는_무한_큐는_기존_바닥을_쓴다():
    assert SessionMonitor().queue_backlog_floor == DEFAULT_QUEUE_BACKLOG_FLOOR


def test_도출한_바닥은_넘침_전에_실제로_경보한다():
    from collector.kiwoom.live_capture import QUEUE_BATCH_SIZE, QUEUE_CAPACITY
    m, clock = monitor_at(at(10, 0), queue_capacity=QUEUE_CAPACITY, queue_batch_size=QUEUE_BATCH_SIZE)
    notices = []
    for second in range(0, 302):                          # capacity 미만에서 줄지 않는 적체
        clock.set(at(10, 0) + second)
        notices.append(m.sample_queue_depth(QUEUE_CAPACITY - 1))
    assert any(n and n.startswith("🐢") for n in notices)


# ── B. 불규칙 표본에서도 정확한 지속시간 ────────────────────────────────

def _queue_monitor(**kwargs):
    kwargs.setdefault("queue_backlog_floor", 100)
    kwargs.setdefault("queue_stuck_window_sec", 300)
    kwargs.setdefault("warn_repeat_sec", 60)
    return monitor_at(at(10, 0), **kwargs)


def _sample(m, clock, offset, depth):
    clock.set(at(10, 0) + offset)
    return m.sample_queue_depth(depth)


def test_표본_간격이_창보다_길어도_지속_적체를_놓치지_않는다():
    """감사 발견: 창 이전 표본을 모두 버려 간격 > 창이면 경보가 영원히 나오지 않았다."""
    m, clock = _queue_monitor()
    assert _sample(m, clock, 0, 500) is None
    notice = _sample(m, clock, 400, 500)
    assert notice is not None and notice.startswith("🐢") and "6분 40초 지속" in notice
    assert _sample(m, clock, 800, 500) is not None        # (계속)


def test_창_경계에서_정확히_판정한다():
    m, clock = _queue_monitor()
    for offset in (0, 100, 299.5):
        assert _sample(m, clock, offset, 500) is None
    assert _sample(m, clock, 300, 500) is not None        # 기준점 t=0, 정확히 300초


def test_실제로_줄고_있으면_불규칙_표본에서도_경보하지_않는다():
    m, clock = _queue_monitor()
    for offset, depth in ((0, 9000), (170, 8000), (410, 7000), (905, 6000), (1500, 5000)):
        assert _sample(m, clock, offset, depth) is None


def test_창_시작점보다_늘었으면_중간에_줄었어도_경보한다():
    m, clock = _queue_monitor()
    assert _sample(m, clock, 0, 5000) is None
    assert _sample(m, clock, 250, 9000) is None
    notice = _sample(m, clock, 400, 6000)                 # 기준점(t=0, 5,000건)보다 많다
    assert notice is not None and "5,000건 → 6,000건" in notice


def test_바닥_아래로_내려가면_지속시간을_새로_잰다():
    m, clock = _queue_monitor()
    assert _sample(m, clock, 0, 500) is None
    assert _sample(m, clock, 200, 50) is None             # 바닥 아래 — 연속 끊김
    assert _sample(m, clock, 250, 500) is None
    assert _sample(m, clock, 500, 500) is None            # 250초부터 250초 지속
    assert _sample(m, clock, 551, 500) is not None


def test_시계가_되돌아가면_지속시간을_새로_잰다():
    m, clock = _queue_monitor()
    assert _sample(m, clock, 1000, 500) is None
    assert _sample(m, clock, 0, 500) is None              # 역행 — 새 기준
    assert _sample(m, clock, 299, 500) is None
    assert _sample(m, clock, 300, 500) is not None


# ── B. logger 배선 ───────────────────────────────────────────────────────

from tests.test_tick_collector_shutdown import collector  # noqa: E402,F401  오프라인 Qt/OCX 대역


def test_logger_기본_raw_v2_감시기는_capacity_기반_바닥을_쓴다(collector):
    from collector.kiwoom.live_capture import QUEUE_BATCH_SIZE, QUEUE_CAPACITY
    old, _, _ = collector
    logger = type(old)(code_revision="fixture")
    assert logger.storage == "raw-v2"
    assert logger.monitor.queue_capacity == QUEUE_CAPACITY
    assert logger.monitor.queue_backlog_floor == queue_backlog_floor_for_capacity(QUEUE_CAPACITY, QUEUE_BATCH_SIZE)
    with pytest.raises(ValueError, match="unreachable"):
        type(old)(code_revision="fixture", queue_backlog_floor=20_000)


def test_logger_raw_v1_무한_큐는_기존_바닥을_유지한다(collector):
    old, _, _ = collector
    logger = type(old)(code_revision="fixture", storage="raw-v1")
    assert logger.monitor.queue_capacity is None
    assert logger.monitor.queue_backlog_floor == DEFAULT_QUEUE_BACKLOG_FLOOR
