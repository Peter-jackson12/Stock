"""tests/test_market_sessions.py — 거래소별 활동 구간과 침묵 계산

창 하나(09:00~15:30)로는 "열려 있는데 조용하다"(결손)와 "닫혀서 조용하다"(정상)를 가를 수
없다. 그리고 체결과 호가는 기대 프로필이 다르다 — 체결이 기대되지 않는 전환 구간에도
호가는 들어올 수 있으나, 들어온다고 확인한 바도 없으므로 판정하지 않는다.

가장 중요한 확인 둘:
  1. 기존 정규장 동작이 그대로인가 (이 모듈이 현재 운영을 바꾸면 안 된다)
  2. 닫힌 구간을 가로지른 침묵이 누적되지 않고 다음 구간 시작부터 다시 계산되는가

실행:
    uv run pytest tests/test_market_sessions.py -v
"""
from __future__ import annotations

import sys
from datetime import date, datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.kiwoom.market_sessions import (  # noqa: E402
    DEFAULT_GAP_SEC,
    KRX,
    NOT_EXPECTED,
    NXT,
    NXT_AFTERMARKET,
    NXT_MAIN,
    NXT_TRANSITION,
    PROFILE_KRX_REGULAR,
    PROFILE_NXT_AFTERMARKET,
    PROFILE_NXT_FULL,
    PROFILE_NXT_PREMARKET,
    PROVISIONAL_LOW_ACTIVITY_GAP_SEC,
    QUOTE,
    TRADE,
    UNJUDGED,
    active_session,
    resolve_profile,
    silence_seconds,
)
from collector.kiwoom.session_monitor import MARKET_CLOSE, MARKET_OPEN  # noqa: E402

DAY = date(2026, 9, 17)


def at(hour, minute=0, second=0):
    return datetime.combine(DAY, dtime(hour, minute, second))


def judges(profile, when, kind=TRADE):
    s = active_session(profile, when, kind)
    return s is not None and s.judges(kind)


# ── 기존 동작 보존 ──────────────────────────────────────────────────────────

def test_기본_프로필은_지금_운영과_같은_창과_임계다():
    s = PROFILE_KRX_REGULAR[0]
    assert (s.opens, s.closes) == (MARKET_OPEN, MARKET_CLOSE)
    assert s.trade_gap == DEFAULT_GAP_SEC and not s.provisional


def test_기본_프로필에서_장전_동시호가는_판정_대상이_아니다():
    assert not judges(PROFILE_KRX_REGULAR, at(8, 45))
    assert judges(PROFILE_KRX_REGULAR, at(9, 0))


def test_기본_프로필의_경계():
    assert not judges(PROFILE_KRX_REGULAR, at(8, 59, 59))
    assert judges(PROFILE_KRX_REGULAR, at(15, 29, 59))
    assert not judges(PROFILE_KRX_REGULAR, at(15, 30))


# ── NXT 구간 ────────────────────────────────────────────────────────────────

def test_NXT_메인은_9시_30초에_시작한다():
    # 공식 안내상 메인마켓 개장은 09:00:30 이다. 09:00:00 이 아니다.
    assert NXT_MAIN.opens == dtime(9, 0, 30)
    assert not NXT_MAIN.contains(dtime(9, 0, 29))
    assert NXT_MAIN.contains(dtime(9, 0, 30))


def test_NXT_프리마켓과_애프터마켓_경계():
    assert judges(PROFILE_NXT_PREMARKET, at(8, 10))
    assert not judges(PROFILE_NXT_PREMARKET, at(8, 50))
    assert not judges(PROFILE_NXT_AFTERMARKET, at(15, 39))
    assert judges(PROFILE_NXT_AFTERMARKET, at(15, 40))
    assert not judges(PROFILE_NXT_AFTERMARKET, at(20, 0))


# ── 체결과 호가의 기준이 다르다 ─────────────────────────────────────────────

def test_전환_구간은_체결을_기대하지_않지만_호가는_판정하지_않는다():
    # 15:20~15:40 은 체결 기대 밖이다. 그렇다고 호가가 안 온다고 단정하지 않는다.
    assert NXT_TRANSITION.trade_gap == NOT_EXPECTED
    assert NXT_TRANSITION.quote_gap == UNJUDGED
    assert not NXT_TRANSITION.judges(TRADE) and not NXT_TRANSITION.judges(QUOTE)
    # 판정하지 않을 뿐 수신·저장을 막는 것이 아니다 — 이 모듈은 저장에 관여하지 않는다.
    seconds, session = silence_seconds(PROFILE_NXT_FULL, last_event_at=at(15, 0), now=at(15, 30))
    assert seconds is None and session is NXT_TRANSITION


def test_호가는_어느_구간에서도_아직_판정하지_않는다():
    # 호가 기대 프로필은 확인된 바 없다. 근거가 생기기 전까지 판정하지 않는다.
    for when in (at(9, 30), at(8, 10), at(17, 0)):
        assert not judges(PROFILE_NXT_FULL, when, QUOTE)


# ── 닫힌 구간을 가로지른 침묵은 누적하지 않는다 ─────────────────────────────

def test_구간_밖_시간은_침묵에_포함되지_않는다():
    # 마지막 수신 15:10(메인), 현재 15:45(애프터). 그 사이 전환 구간 20분은 세지 않고
    # 애프터마켓이 열린 15:40 부터 다시 잰다 → 5분.
    seconds, session = silence_seconds(
        PROFILE_NXT_FULL, last_event_at=at(15, 10), now=at(15, 45))
    assert session is NXT_AFTERMARKET
    assert seconds == 300.0


def test_밤을_지난_침묵은_다음_구간_시작부터_다시_잰다():
    # 어제 저녁 마지막 수신, 오늘 아침 프리마켓 중. 밤새 시간을 결손으로 세면 안 된다.
    last = datetime.combine(date(2026, 9, 16), dtime(19, 0))
    seconds, session = silence_seconds(PROFILE_NXT_FULL, last_event_at=last, now=at(8, 10))
    assert session.venue == NXT and seconds == 600.0      # 08:00 부터 10분


def test_구간_안에서는_마지막_수신부터_잰다():
    seconds, _ = silence_seconds(PROFILE_KRX_REGULAR, last_event_at=at(10, 0), now=at(10, 3))
    assert seconds == 180.0


def test_수신_이력이_없으면_구간_시작부터_잰다():
    seconds, _ = silence_seconds(PROFILE_KRX_REGULAR, last_event_at=None, now=at(9, 5))
    assert seconds == 300.0


def test_판정하지_않는_시각은_길이를_돌려주지_않는다():
    seconds, session = silence_seconds(PROFILE_KRX_REGULAR, last_event_at=at(10, 0), now=at(16, 0))
    assert seconds is None and session is None


# ── 임계값의 지위 ───────────────────────────────────────────────────────────

def test_저활동_임계는_임시값으로_표시된다():
    # 저활동 표시만으로 600초가 타당하다고 검증된 것이 아니다.
    assert NXT_AFTERMARKET.provisional
    assert NXT_AFTERMARKET.trade_gap == PROVISIONAL_LOW_ACTIVITY_GAP_SEC
    assert not PROFILE_KRX_REGULAR[0].provisional


def test_구간이_겹치면_더_촘촘한_수신을_기대하는_쪽을_고른다():
    both = PROFILE_KRX_REGULAR + PROFILE_NXT_FULL
    assert active_session(both, at(9, 30), TRADE).trade_gap == DEFAULT_GAP_SEC
    assert active_session(both, at(8, 10), TRADE).venue == NXT


def test_알_수_없는_프로필과_종류는_거부한다():
    assert resolve_profile("krx_regular") == PROFILE_KRX_REGULAR
    for bad in ("야간", "nxt"):
        try:
            resolve_profile(bad)
        except ValueError:
            continue
        raise AssertionError(f"잘못된 프로필을 통과시켰다: {bad}")
    try:
        NXT_MAIN.gap_for("체결")
    except ValueError:
        return
    raise AssertionError("잘못된 종류를 통과시켰다")


# ── 감시 모듈 연결 ──────────────────────────────────────────────────────────

def _monitor(clock, **kw):
    from collector.kiwoom.session_monitor import SessionMonitor
    m = SessionMonitor(clock=clock, gap_threshold_sec=120, silence_stop_sec=600, **kw)
    m.start(); m.mark_reception_expected()
    return m


class _Clock:
    def __init__(self, moment): self.now = moment.timestamp()
    def __call__(self): return self.now
    def set(self, moment): self.now = moment.timestamp()
    def advance(self, seconds): self.now += seconds


def _today(hour, minute=0, second=0):
    from datetime import date as d
    return datetime.combine(d.today(), dtime(hour, minute, second))


def test_프로필을_주지_않으면_기존_경로_그대로다():
    # 정규장 창 밖(16시)에서는 경고하지 않는다 — 지금 운영과 같은 동작.
    clock = _Clock(_today(16, 0))
    m = _monitor(clock); m.on_trade()
    clock.advance(3600)
    assert m.tick() is None and m.silence_stop_reason() is None
    # 정규장 안에서는 경고한다.
    clock.set(_today(10, 0)); m2 = _monitor(clock); m2.on_trade()
    clock.advance(121)
    assert m2.tick() is not None


def test_애프터마켓_프로필을_주면_저녁에도_판정한다():
    # 기존 경로라면 16시는 판정 대상이 아니지만, 애프터마켓 프로필에서는 대상이다.
    clock = _Clock(_today(16, 0))
    m = _monitor(clock, sessions=PROFILE_NXT_AFTERMARKET)
    m.on_trade()
    clock.advance(121)
    assert m.tick() is None                    # 애프터마켓 임계는 600초라 아직 아니다
    clock.advance(500)
    warning = m.tick()
    assert warning is not None and "결손 의심" in warning


def test_전환_구간에서는_경고하지_않는다():
    # 15:20~15:40 은 체결 기대 밖이다. 조용해도 경고하면 안 된다.
    clock = _Clock(_today(15, 25))
    m = _monitor(clock, sessions=PROFILE_NXT_FULL)
    m.on_trade()
    clock.advance(900)                          # 15분 침묵
    assert m.tick() is None
    assert m.silence_stop_reason() is None
