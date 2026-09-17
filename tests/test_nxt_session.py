"""tests/test_nxt_session.py — NXT 프리마켓 판정에서 결손이 '미과열' 로 접히지 않는가

기존 nxt_tick_engine 의 위험은 계산이 틀린 것이 아니라 **결손의 방향**이었다.
`is_nxt_exhausted = False` 가 기본값이라, NXT 데이터를 못 받은 날과 NXT 가
잠잠했던 날이 같은 값으로 접히고 둘 다 09:00 진입 허용으로 흘렀다.

판정에는 서로 다른 두 축이 필요하다. 거래소를 구분할 수 있는가(venue_resolution)와
그 구간에 NXT 를 실제로 받고 있었는가(nxt_coverage)는 별개다. 이 둘을 하나로
접으면 KRX 체결만 있는 구간이 'NXT 가 잠잠했다' 로 둔갑한다.

실행:
    uv run pytest tests/test_nxt_session.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.nxt_session import (  # noqa: E402
    CALM,
    KRX,
    NXT,
    NXT_COVERAGE_CONFIRMED,
    NXT_COVERAGE_UNCONFIRMED,
    OVERHEATED,
    PER_EVENT_VENUE,
    UNIFIED,
    UNVERIFIED,
    VENUE_UNVERIFIED,
    NxtUnverifiedError,
    classify_premarket,
    require_verdict,
    venue_from_code,
)

WINDOW = (28800, 31800)          # 08:00:00 ~ 08:50:00
THRESHOLDS = dict(gain_threshold=0.05, volume_threshold=50000)
RESOLVED = dict(venue_resolution=PER_EVENT_VENUE, nxt_coverage=NXT_COVERAGE_CONFIRMED)


def _tick(sec, price, vol, venue=NXT):
    return {"sec": sec, "price": price, "vol": vol, "venue": venue}


def _classify(events, **over):
    kwargs = dict(window=WINDOW, **THRESHOLDS, **RESOLVED)
    kwargs.update(over)
    return classify_premarket(events, **kwargs)


# --- 축 1: 거래소 확인 -------------------------------------------------------

def test_거래소_확인_수단이_없으면_계산하지_않는다():
    # raw-v1 원본과 venue="unknown" 인 raw-v2 가 여기 해당한다.
    events = [_tick(28900, 10000, 100000), _tick(31000, 11000, 100000)]
    verdict = _classify(events, venue_resolution=VENUE_UNVERIFIED)
    assert verdict.status == UNVERIFIED
    assert verdict.exhausted is None          # False 로 접히지 않는다
    assert not verdict.verified


def test_구간에_거래소_미확인_이벤트가_섞이면_미확인이다():
    events = [_tick(28900, 10000, 100000), _tick(29000, 10500, 1000, venue="unknown")]
    verdict = _classify(events)
    assert verdict.status == UNVERIFIED
    assert verdict.exhausted is None


def test_통합시장_체결은_NXT로_귀속하지_않는다():
    # "_AL" 은 최우선호가 시장이라 어느 거래소에서 체결됐는지 단정할 수 없다.
    events = [_tick(28900, 10000, 100000, venue=UNIFIED)]
    verdict = _classify(events)
    assert verdict.status == UNVERIFIED
    assert "통합시장" in verdict.reason


# --- 축 2: NXT 구간 수신 완전성 (2026-09-17 지적된 반례) ----------------------

def test_KRX_체결만_있는_구간은_NXT가_잠잠했다는_근거가_아니다():
    # 거래소는 확인되지만 NXT 수신이 확인되지 않았다면 calm 으로 접으면 안 된다.
    # 08:30~08:40 KRX 장전 시간외종가 체결이 구간 안에 들어온다.
    events = [
        _tick(30600, 10000, 500000, venue=KRX),   # 08:30:00
        _tick(31000, 12000, 500000, venue=KRX),   # 08:36:40 — +20%, 대량
    ]
    verdict = _classify(events, nxt_coverage=NXT_COVERAGE_UNCONFIRMED)
    assert verdict.status == UNVERIFIED
    assert verdict.exhausted is None
    assert "NXT 구간 수신이 확인되지 않았다" in verdict.reason


def test_빈_입력은_무거래가_아니라_미확인이다():
    verdict = _classify([])
    assert verdict.status == UNVERIFIED
    assert verdict.exhausted is None
    assert "수집 결손" in verdict.reason


def test_수신이_확인된_경우에만_NXT_0건이_미과열이_된다():
    events = [_tick(30600, 10000, 500000, venue=KRX)]
    assert _classify(events, nxt_coverage=NXT_COVERAGE_UNCONFIRMED).status == UNVERIFIED
    confirmed = _classify(events, nxt_coverage=NXT_COVERAGE_CONFIRMED)
    assert confirmed.status == CALM
    assert confirmed.exhausted is False
    assert confirmed.trade_count == 0


# --- 차단 동작 ---------------------------------------------------------------

def test_미확인은_False가_아니라_막힌다():
    verdict = _classify([_tick(28900, 10000, 100000)], venue_resolution=VENUE_UNVERIFIED)
    try:
        require_verdict(verdict)
    except NxtUnverifiedError:
        pass
    else:
        raise AssertionError("미확인 판정이 조용히 통과했다")
    assert require_verdict(verdict, allow_unverified=True) is verdict


# --- 확인된 경우의 계산 ------------------------------------------------------

def test_확인된_NXT_과열은_그대로_잡는다():
    events = [_tick(28800, 10000, 30000), _tick(31700, 10600, 30000)]   # +6%, 6만주
    verdict = _classify(events)
    assert verdict.status == OVERHEATED
    assert verdict.exhausted is True
    assert verdict.volume == 60000
    assert round(verdict.gain, 4) == 0.06


def test_확인된_미과열은_False로_남는다():
    events = [_tick(28800, 10000, 1000), _tick(31700, 10100, 1000)]     # +1%, 2천주
    verdict = _classify(events)
    assert verdict.status == CALM
    assert verdict.exhausted is False
    assert verdict.verified                      # 미확인과 구분된다


def test_구간_밖_NXT_체결은_제외된다():
    events = [
        _tick(28799, 10000, 100000),            # 07:59:59 — 구간 앞
        _tick(31800, 20000, 100000),            # 08:50:00 — 반열린 구간의 끝, 제외
    ]
    verdict = _classify(events)
    assert verdict.status == UNVERIFIED
    assert verdict.trade_count == 0


# --- 종목코드 → 거래소 (koa_devguide.xml) ------------------------------------

def test_종목코드_접미사로_거래소를_도출한다():
    assert venue_from_code("039490") == KRX
    assert venue_from_code("039490_NX") == NXT
    assert venue_from_code("039490_AL") == UNIFIED
    assert venue_from_code("039490_nx") == NXT        # 대소문자 무시
    for bad in ("", None, "39490", "039490_XX", "039490_", "abcdef", 39490):
        assert venue_from_code(bad) is None, bad


def test_잘못된_인자는_거부한다():
    for kwargs in (dict(venue_resolution="시간대"), dict(nxt_coverage="아마도"),
                   dict(window=(31800, 28800))):
        try:
            _classify([_tick(28900, 10000, 1)], **kwargs)
        except ValueError:
            continue
        raise AssertionError(f"잘못된 인자를 통과시켰다: {kwargs}")


def test_unrecognized_venue_cannot_certify_calm():
    for venue in ("nxt", "KRXX", "unresolved", None, ""):
        verdict = _classify([_tick(30000, 10000, 1, venue=venue)])
        assert verdict.status == UNVERIFIED and verdict.exhausted is None


def test_per_event_resolution_alone_never_certifies_coverage():
    for events in ([], [_tick(30000, 10000, 1, KRX)], [_tick(30000, 10000, 1)]):
        verdict = classify_premarket(events, window=WINDOW, **THRESHOLDS,
                                     venue_resolution=PER_EVENT_VENUE)
        assert verdict.status == UNVERIFIED and verdict.exhausted is None
