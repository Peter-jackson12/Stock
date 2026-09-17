"""거래소별 활동 구간 — 침묵 판정이 "지금 무엇이 기대되는가" 를 시장에 맞춰 묻게 한다.

지금까지 침묵 판정은 09:00~15:30 하나에 묶여 있었다. KRX 정규장만 수집하는 동안에는
맞았지만, NXT 를 함께 받으면 틀린다. NXT 는 프리마켓이 정규장보다 이르고 애프터마켓이
저녁까지 이어지며, 그 사이에 체결이 기대되지 않는 구간이 낀다. 창 하나로는
"열려 있는데 조용하다"(결손)와 "닫혀서 조용하다"(정상)를 가를 수 없다.

**체결과 호가는 기대 프로필이 다르다.** 메인마켓 종료와 애프터마켓 개장 사이처럼 체결이
기대되지 않는 구간에도 호가는 들어올 수 있다. 그러나 실제로 들어오는지 확인한 바 없으므로
'기대하지 않는다' 고 단정하지도 않는다. 그래서 종류별 상태가 셋이다.

    임계값 있음  — 그 시간만큼 조용하면 결손을 의심한다
    NOT_EXPECTED — 조용한 것이 정상이다. 경고하지 않는다
    UNJUDGED     — 근거가 없다. 경고도 안 하고 정상이라고도 하지 않는다

**이 모듈은 판정에만 쓰인다. 수신과 저장을 막지 않는다.** 판정하지 않는 구간에도 들어온
데이터는 그대로 받아 저장한다.

침묵 길이는 **구간 안에 있던 시간만** 센다. 닫힌 구간을 가로지른 침묵은 누적하지 않고
다음 구간이 열리는 시각부터 다시 잰다. 밤새 조용했다고 아침에 결손을 외치면 안 된다.

시각 출처: 넥스트레이드 거래 시스템 안내(프리 08:00~08:50, 메인 09:00:30~15:20,
애프터 15:40~20:00), 한국거래소 정규장 09:00~15:30.

임계값은 **임시 정책값이다.** 저유동성 표시가 붙었다고 그 숫자가 타당하다고 검증된 것은
아니다. 실측으로 정해지기 전까지 provisional 로 표시해 둔다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type, datetime, time as dtime

KRX = "KRX"
NXT = "NXT"

TRADE = "trade"
QUOTE = "quote"

#: 조용한 것이 정상인 구간. 경고하지 않는다.
NOT_EXPECTED = "not_expected"
#: 근거가 없어 판정하지 않는 구간. 경고도, 정상 판정도 하지 않는다.
UNJUDGED = "unjudged"

#: 체결이 촘촘한 구간의 침묵 경고 임계. 기존 운영값이다.
DEFAULT_GAP_SEC = 120.0
#: 저활동 구간의 임시 정책값. 실측으로 검증된 값이 아니다.
PROVISIONAL_LOW_ACTIVITY_GAP_SEC = 600.0


@dataclass(frozen=True)
class MarketSession:
    venue: str
    name: str
    opens: dtime
    closes: dtime
    #: 체결 침묵 임계(초) 또는 NOT_EXPECTED / UNJUDGED
    trade_gap: object = DEFAULT_GAP_SEC
    #: 호가 침묵 임계(초) 또는 NOT_EXPECTED / UNJUDGED
    quote_gap: object = UNJUDGED
    #: 임계값이 실측이 아닌 임시 정책값인가
    provisional: bool = False

    def contains(self, moment: dtime) -> bool:
        return self.opens <= moment < self.closes

    def gap_for(self, kind):
        if kind not in (TRADE, QUOTE):
            raise ValueError(f"지원하지 않는 이벤트 종류: {kind!r}")
        return self.trade_gap if kind == TRADE else self.quote_gap

    def judges(self, kind) -> bool:
        return isinstance(self.gap_for(kind), (int, float))


#: 한국거래소 정규장. 기존 운영과 같다. 장전 동시호가(08:30~09:00)는 수신이 오지만
#: 지금까지 판정 대상이 아니었으므로 여기서도 넣지 않는다.
KRX_REGULAR = MarketSession(KRX, "KRX 정규장", dtime(9, 0), dtime(15, 30),
                            trade_gap=DEFAULT_GAP_SEC, quote_gap=UNJUDGED)

NXT_PREMARKET = MarketSession(
    NXT, "NXT 프리마켓", dtime(8, 0), dtime(8, 50),
    trade_gap=PROVISIONAL_LOW_ACTIVITY_GAP_SEC, quote_gap=UNJUDGED, provisional=True)
#: 공식 안내상 메인마켓은 09:00:30 에 시작한다.
NXT_MAIN = MarketSession(
    NXT, "NXT 메인마켓", dtime(9, 0, 30), dtime(15, 20),
    trade_gap=DEFAULT_GAP_SEC, quote_gap=UNJUDGED)
#: 메인 종료와 애프터 개장 사이. 체결은 기대하지 않지만 호가 수신 여부는 확인된 바 없다.
NXT_TRANSITION = MarketSession(
    NXT, "NXT 전환 구간", dtime(15, 20), dtime(15, 40),
    trade_gap=NOT_EXPECTED, quote_gap=UNJUDGED)
NXT_AFTERMARKET = MarketSession(
    NXT, "NXT 애프터마켓", dtime(15, 40), dtime(20, 0),
    trade_gap=PROVISIONAL_LOW_ACTIVITY_GAP_SEC, quote_gap=UNJUDGED, provisional=True)

PROFILE_KRX_REGULAR = (KRX_REGULAR,)
PROFILE_NXT_PREMARKET = (NXT_PREMARKET,)
PROFILE_NXT_AFTERMARKET = (NXT_AFTERMARKET,)
PROFILE_NXT_FULL = (NXT_PREMARKET, NXT_MAIN, NXT_TRANSITION, NXT_AFTERMARKET)

PROFILES = {
    "krx_regular": PROFILE_KRX_REGULAR,
    "nxt_premarket": PROFILE_NXT_PREMARKET,
    "nxt_aftermarket": PROFILE_NXT_AFTERMARKET,
    "nxt_full": PROFILE_NXT_FULL,
}


def resolve_profile(name):
    if name not in PROFILES:
        raise ValueError(f"알 수 없는 시장 구간 프로필: {name!r} (가능: {', '.join(sorted(PROFILES))})")
    return PROFILES[name]


def active_session(sessions, when, kind=TRADE):
    """그 시각의 구간을 돌려준다. 어느 구간에도 없으면 None.

    구간이 겹치면 해당 종류를 실제로 판정하는 구간을, 그중에서도 임계가 짧은 쪽을 고른다.
    결손을 놓치지 않으려면 보수적으로 판정해야 한다.
    """
    moment = when.time() if isinstance(when, datetime) else when
    matched = [s for s in sessions if s.contains(moment)]
    if not matched:
        return None
    judging = [s for s in matched if s.judges(kind)]
    if judging:
        return min(judging, key=lambda s: s.gap_for(kind))
    return matched[0]


def silence_seconds(sessions, *, last_event_at, now, kind=TRADE):
    """판정에 쓸 침묵 길이(초)와 구간을 돌려준다. 판정하지 않으면 (None, 구간).

    닫힌 구간을 가로지른 침묵은 누적하지 않는다. 현재 구간이 열린 시각과 마지막 수신 시각
    중 나중 것부터 잰다 — 구간 밖 시간은 세지 않고 다음 구간 시작부터 다시 계산된다.
    """
    session = active_session(sessions, now, kind)
    if session is None or not session.judges(kind):
        return None, session
    opened_at = datetime.combine(
        now.date() if isinstance(now, datetime) else date_type.today(), session.opens)
    reference = opened_at if last_event_at is None else max(last_event_at, opened_at)
    return max(0.0, (now - reference).total_seconds()), session
