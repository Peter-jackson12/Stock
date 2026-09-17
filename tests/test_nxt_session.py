"""tests/test_nxt_session.py — NXT 프리마켓 판정에서 결손이 '미과열' 로 접히지 않는가

기존 nxt_tick_engine 의 위험은 계산이 틀린 것이 아니라 **결손의 방향**이었다.
`is_nxt_exhausted = False` 가 기본값이라, NXT 데이터를 못 받은 날과 NXT 가
잠잠했던 날이 같은 값으로 접히고 둘 다 09:00 진입 허용으로 흘렀다.
여기서는 그 두 갈래가 갈라지는지, 그리고 시간대만 맞는 타 거래소 체결이
NXT 과열 계산에 들어가지 않는지를 확인한다.

실행:
    uv run pytest tests/test_nxt_session.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.nxt_session import (  # noqa: E402
    CALM,
    OVERHEATED,
    PER_EVENT_VENUE,
    UNVERIFIED,
    VENUE_UNVERIFIED,
    NxtUnverifiedError,
    classify_premarket,
    require_verdict,
)

WINDOW = (28800, 31800)          # 08:00:00 ~ 08:50:00
THRESHOLDS = dict(gain_threshold=0.05, volume_threshold=50000)


def _tick(sec, price, vol, venue="NXT"):
    return {"sec": sec, "price": price, "vol": vol, "venue": venue}


def test_거래소_확인_수단이_없으면_계산하지_않는다():
    # raw-v1 원본과 venue="unknown" 인 raw-v2 가 여기 해당한다.
    events = [_tick(28900, 10000, 100000), _tick(31000, 11000, 100000)]
    verdict = classify_premarket(events, window=WINDOW, venue_resolution=VENUE_UNVERIFIED, **THRESHOLDS)
    assert verdict.status == UNVERIFIED
    assert verdict.exhausted is None          # False 로 접히지 않는다
    assert not verdict.verified


def test_미확인은_False가_아니라_막힌다():
    events = [_tick(28900, 10000, 100000)]
    verdict = classify_premarket(events, window=WINDOW, venue_resolution=VENUE_UNVERIFIED, **THRESHOLDS)
    try:
        require_verdict(verdict)
    except NxtUnverifiedError:
        pass
    else:
        raise AssertionError("미확인 판정이 조용히 통과했다")
    # 명시적으로 허용할 때만 지나간다.
    assert require_verdict(verdict, allow_unverified=True) is verdict


def test_구간에_거래소_미확인_이벤트가_섞이면_미확인이다():
    events = [_tick(28900, 10000, 100000), _tick(29000, 10500, 1000, venue="unknown")]
    verdict = classify_premarket(events, window=WINDOW, venue_resolution=PER_EVENT_VENUE, **THRESHOLDS)
    assert verdict.status == UNVERIFIED
    assert verdict.exhausted is None
    assert "미확인" in verdict.reason


def test_시간대만_맞는_KRX_체결은_NXT_과열에_들어가지_않는다():
    # 08:30~08:40 KRX 장전 시간외종가 체결이 구간 안에 있어도 NXT 계산에서 빠진다.
    events = [
        _tick(30600, 10000, 500000, venue="KRX"),   # 08:30:00
        _tick(31000, 12000, 500000, venue="KRX"),   # 08:36:40 — +20%, 대량
    ]
    verdict = classify_premarket(events, window=WINDOW, venue_resolution=PER_EVENT_VENUE, **THRESHOLDS)
    assert verdict.status == CALM               # 거래소는 확인됐고
    assert verdict.exhausted is False           # NXT 체결이 0건이라 과열이 아니다
    assert verdict.trade_count == 0
    assert "NXT 체결 0건" in verdict.reason


def test_확인된_NXT_과열은_그대로_잡는다():
    events = [
        _tick(28800, 10000, 30000),
        _tick(31700, 10600, 30000),             # +6%, 6만주
    ]
    verdict = classify_premarket(events, window=WINDOW, venue_resolution=PER_EVENT_VENUE, **THRESHOLDS)
    assert verdict.status == OVERHEATED
    assert verdict.exhausted is True
    assert verdict.volume == 60000
    assert round(verdict.gain, 4) == 0.06


def test_확인된_미과열은_False로_남는다():
    events = [_tick(28800, 10000, 1000), _tick(31700, 10100, 1000)]   # +1%, 2천주
    verdict = classify_premarket(events, window=WINDOW, venue_resolution=PER_EVENT_VENUE, **THRESHOLDS)
    assert verdict.status == CALM
    assert verdict.exhausted is False
    assert verdict.verified                      # 미확인과 구분된다


def test_구간_밖_NXT_체결은_제외된다():
    events = [
        _tick(28799, 10000, 100000),            # 07:59:59 — 구간 앞
        _tick(31800, 20000, 100000),            # 08:50:00 — 반열린 구간의 끝, 제외
    ]
    verdict = classify_premarket(events, window=WINDOW, venue_resolution=PER_EVENT_VENUE, **THRESHOLDS)
    assert verdict.status == CALM
    assert verdict.trade_count == 0


def test_잘못된_인자는_거부한다():
    for kwargs in (dict(venue_resolution="시간대"), dict(window=(31800, 28800))):
        base = dict(window=WINDOW, venue_resolution=PER_EVENT_VENUE, **THRESHOLDS)
        base.update(kwargs)
        try:
            classify_premarket([], **base)
        except ValueError:
            continue
        raise AssertionError(f"잘못된 인자를 통과시켰다: {kwargs}")
