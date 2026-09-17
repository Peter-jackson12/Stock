"""tests/test_market_sessions.py — 거래소별 활동 구간 판정

침묵 판정이 09:00~15:30 하나에 묶여 있으면, NXT 를 함께 받을 때 "장중인데 조용하다"(결손)와
"닫혀 있어 조용하다"(정상)를 가를 수 없다. NXT 는 프리마켓이 더 이르고 애프터마켓이 저녁까지
이어지며 그 사이에 아무것도 기대되지 않는 구간이 낀다.

가장 중요한 확인은 **기존 정규장 동작이 그대로인가** 다. 이 모듈이 현재 운영 수집을 바꾸면 안 된다.

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
    LOW_LIQUIDITY_GAP_SEC,
    NXT,
    PROFILE_KRX_REGULAR,
    PROFILE_NXT_AFTERMARKET,
    PROFILE_NXT_PREMARKET,
    active_session,
    is_expected,
    resolve_profile,
)
from collector.kiwoom.session_monitor import MARKET_CLOSE, MARKET_OPEN  # noqa: E402


def at(hour, minute=0, second=0):
    return dtime(hour, minute, second)


# ── 기존 동작 보존 (가장 중요) ──────────────────────────────────────────────

def test_기본_프로필은_지금_운영과_같은_창이다():
    session = PROFILE_KRX_REGULAR[0]
    assert (session.opens, session.closes) == (MARKET_OPEN, MARKET_CLOSE)
    assert session.gap_threshold_sec == DEFAULT_GAP_SEC


def test_기본_프로필에서_장전_동시호가는_판정_대상이_아니다():
    # 08:30~09:00 에도 수신은 오지만 지금까지 침묵 판정 대상이 아니었다. 그대로 둔다.
    assert not is_expected(PROFILE_KRX_REGULAR, at(8, 45))
    assert is_expected(PROFILE_KRX_REGULAR, at(9, 0))


def test_기본_프로필의_경계():
    assert not is_expected(PROFILE_KRX_REGULAR, at(8, 59, 59))
    assert is_expected(PROFILE_KRX_REGULAR, at(9, 0, 0))
    assert is_expected(PROFILE_KRX_REGULAR, at(15, 29, 59))
    assert not is_expected(PROFILE_KRX_REGULAR, at(15, 30, 0))    # 끝은 열린 구간


# ── NXT 구간 ────────────────────────────────────────────────────────────────

def test_NXT_프리마켓은_정규장보다_이르다():
    session = active_session(PROFILE_NXT_PREMARKET, at(8, 10))
    assert session is not None and session.venue == NXT
    assert not is_expected(PROFILE_NXT_PREMARKET, at(8, 50))       # 08:50 에 닫힌다
    # 같은 시각이 정규장 프로필에서는 기대 구간이 아니다.
    assert not is_expected(PROFILE_KRX_REGULAR, at(8, 10))


def test_NXT_애프터마켓은_15시40분에_열린다():
    assert not is_expected(PROFILE_NXT_AFTERMARKET, at(15, 39))
    assert is_expected(PROFILE_NXT_AFTERMARKET, at(15, 40))
    assert is_expected(PROFILE_NXT_AFTERMARKET, at(19, 59, 59))
    assert not is_expected(PROFILE_NXT_AFTERMARKET, at(20, 0))


def test_정규장_마감과_애프터마켓_사이_공백은_기대하지_않는다():
    # 15:30~15:40 은 어느 쪽도 아니다. 이 구간의 침묵을 결손으로 의심하면 안 된다.
    both = PROFILE_KRX_REGULAR + PROFILE_NXT_AFTERMARKET
    assert not is_expected(both, at(15, 35))


def test_저유동성_구간은_침묵_임계가_길다():
    # 애프터마켓에서 몇 분간 무체결은 정상이다. 연결 장애로 단정하지 않는다.
    after = active_session(PROFILE_NXT_AFTERMARKET, at(17, 0))
    assert after.low_liquidity and after.gap_threshold_sec == LOW_LIQUIDITY_GAP_SEC
    regular = active_session(PROFILE_KRX_REGULAR, at(10, 0))
    assert not regular.low_liquidity
    assert regular.gap_threshold_sec < after.gap_threshold_sec


def test_구간이_겹치면_더_촘촘한_수신을_기대하는_쪽을_고른다():
    # 결손을 놓치지 않으려면 보수적으로 판정해야 한다.
    both = PROFILE_KRX_REGULAR + PROFILE_NXT_PREMARKET
    session = active_session(both, at(9, 30))          # 정규장만 해당
    assert session.venue == KRX
    overlap = (PROFILE_KRX_REGULAR[0], PROFILE_NXT_AFTERMARKET[0],
               PROFILE_NXT_PREMARKET[0])
    # 09:30 은 정규장(120초)만 겹치므로 그대로, 08:10 은 프리마켓만
    assert active_session(overlap, at(8, 10)).venue == NXT


def test_datetime을_넣어도_시각만_본다():
    moment = datetime.combine(date(2026, 9, 17), at(10, 0))
    assert is_expected(PROFILE_KRX_REGULAR, moment)


def test_알_수_없는_프로필은_거부한다():
    assert resolve_profile("krx_regular") == PROFILE_KRX_REGULAR
    try:
        resolve_profile("야간")
    except ValueError as exc:
        assert "알 수 없는" in str(exc)
    else:
        raise AssertionError("잘못된 프로필 이름을 통과시켰다")
