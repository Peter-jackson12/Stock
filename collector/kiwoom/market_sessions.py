"""거래소별 활동 구간 — 침묵 판정이 "지금 수신이 기대되는가" 를 시장에 맞춰 묻게 한다.

지금까지 침묵 판정은 09:00~15:30 하나에 묶여 있었다(session_monitor 의 상수 두 개).
KRX 정규장만 수집하는 동안에는 맞았지만, NXT 를 함께 받으면 틀린다. NXT 는 프리마켓이
정규장보다 한 시간 일찍 열리고 애프터마켓이 저녁 8시까지 이어지며, 그 사이 15:20~15:40 처럼
**아무것도 기대되지 않는 구간**이 낀다. 하나의 창으로는 "장중인데 조용하다"(결손)와
"닫혀 있어 조용하다"(정상)를 가를 수 없다.

또 하나. 애프터마켓처럼 종목이 적고 유동성이 낮은 구간에서는 몇 분간 체결이 없는 것이
정상이다. 런북이 짚은 대로 저유동성 무체결을 연결 장애로 단정하면 안 된다. 그래서 구간마다
침묵 임계를 따로 들고 다닌다 — 정규장의 2분과 애프터마켓의 2분은 같은 의미가 아니다.

시각 출처: 넥스트레이드 거래 시스템 안내(프리 08:00~08:50, 메인 09:00~15:20,
애프터 15:40~20:00), 한국거래소 정규장 09:00~15:30.

**이 모듈은 시각을 판정할 뿐 구독을 바꾸지 않는다.** 기본 프로필은 오늘 운영과 동일한
KRX 정규장 단독이며, NXT 구간은 명시적으로 선택해야 켜진다. 기존 수집 동작을 바꾸지 않기
위한 것이다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime

KRX = "KRX"
NXT = "NXT"

#: 기본 침묵 경고 임계. 정규장처럼 체결이 촘촘한 구간 기준이다.
DEFAULT_GAP_SEC = 120.0
#: 저유동성 구간의 침묵 경고 임계. 무체결이 곧 장애는 아니므로 넉넉히 둔다.
LOW_LIQUIDITY_GAP_SEC = 600.0


@dataclass(frozen=True)
class MarketSession:
    venue: str
    name: str
    opens: dtime
    closes: dtime
    #: 이 구간에서 침묵을 결손으로 의심하기까지의 시간.
    gap_threshold_sec: float = DEFAULT_GAP_SEC
    #: 체결이 드문 것이 정상인 구간인가. 참이면 무체결만으로 장애를 단정하지 않는다.
    low_liquidity: bool = False

    def contains(self, moment: dtime) -> bool:
        return self.opens <= moment < self.closes


#: 한국거래소 정규장. 장전 동시호가(08:30~09:00)는 수신이 오지만 지금까지 침묵 판정 대상이
#: 아니었고, 여기서도 대상에 넣지 않는다 — 기존 동작을 바꾸지 않기 위해서다.
KRX_REGULAR = MarketSession(KRX, "KRX 정규장", dtime(9, 0), dtime(15, 30))

NXT_PREMARKET = MarketSession(NXT, "NXT 프리마켓", dtime(8, 0), dtime(8, 50),
                              gap_threshold_sec=LOW_LIQUIDITY_GAP_SEC, low_liquidity=True)
NXT_MAIN = MarketSession(NXT, "NXT 메인마켓", dtime(9, 0), dtime(15, 20))
NXT_AFTERMARKET = MarketSession(NXT, "NXT 애프터마켓", dtime(15, 40), dtime(20, 0),
                                gap_threshold_sec=LOW_LIQUIDITY_GAP_SEC, low_liquidity=True)

#: 오늘 운영과 같은 프로필. 이것이 기본값이다.
PROFILE_KRX_REGULAR = (KRX_REGULAR,)
#: NXT 애프터마켓만 따로 관측할 때.
PROFILE_NXT_AFTERMARKET = (NXT_AFTERMARKET,)
#: NXT 프리마켓만.
PROFILE_NXT_PREMARKET = (NXT_PREMARKET,)

PROFILES = {
    "krx_regular": PROFILE_KRX_REGULAR,
    "nxt_premarket": PROFILE_NXT_PREMARKET,
    "nxt_aftermarket": PROFILE_NXT_AFTERMARKET,
}


def resolve_profile(name):
    if name not in PROFILES:
        raise ValueError(f"알 수 없는 시장 구간 프로필: {name!r} (가능: {', '.join(sorted(PROFILES))})")
    return PROFILES[name]


def active_session(sessions, when):
    """그 시각에 수신이 기대되는 구간을 돌려준다. 없으면 None.

    구간이 겹치면 침묵 임계가 짧은 쪽(더 촘촘한 수신을 기대하는 쪽)을 고른다.
    보수적으로 판정해야 결손을 놓치지 않기 때문이다.
    """
    moment = when.time() if isinstance(when, datetime) else when
    matched = [s for s in sessions if s.contains(moment)]
    if not matched:
        return None
    return min(matched, key=lambda s: s.gap_threshold_sec)


def is_expected(sessions, when):
    return active_session(sessions, when) is not None
