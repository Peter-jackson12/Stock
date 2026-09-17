"""NXT 프리마켓 과열 판정 — 거래소와 수신 완전성이 모두 확인될 때만 계산한다.

기존 `nxt_tick_engine` 은 08:00~08:50 이라는 **시간대만으로** 이벤트를 NXT
프리마켓 체결로 간주했다. 시간대는 거래소를 확정하지 못한다. 같은 구간에
KRX 장전 시간외종가(08:30~08:40) 체결이 들어오고, 실제로 현재 수집 경로는
08:00~08:30 에 아무 이벤트도 받지 못한 채 08:30 부터 받는다(2026-09-16/17 관측).

더 위험한 것은 결손의 방향이었다. 원래 코드는

    is_nxt_exhausted = False          # 기본값
    if nxt_trades: ...                # 있을 때만 갱신

이라서 **"NXT 를 보고 과열이 아니었다"** 와 **"NXT 데이터가 아예 없다"** 가
똑같이 False 로 접혔고, 둘 다 09:00 정규장 진입 허용으로 흘렀다.

판정에는 **서로 다른 두 축**이 필요하다. 이 둘을 하나로 접으면 결손이 다시
'미과열' 로 새어나간다.

  1. 거래소 확인 (venue_resolution)
     이벤트가 어느 거래소 것인지 말할 수 있는가.
     키움 개발가이드: 실시간 종목코드는 조회한 종목코드와 같고, KRX 는 6자리,
     NXT 는 "_NX", 통합(최우선호가)은 "_AL" 접미사를 갖는다. 따라서 거래소는
     콜백의 종목코드에서 도출된다 (`venue_from_code`).

  2. NXT 구간 수신 완전성 (nxt_coverage)
     그 구간에 NXT 이벤트가 **올 수 있었는가**. 거래소를 구분할 수 있다는 것과
     NXT 를 실제로 구독해 받고 있었다는 것은 별개다. KRX 체결만 잔뜩 있는
     구간은 "NXT 가 잠잠했다" 의 근거가 되지 못한다 — NXT 를 애초에 구독하지
     않았을 뿐일 수 있다.

판정은 세 가지다.
    overheated  — 두 축 모두 확인된 NXT 체결이 과열 임계를 넘었다
    calm        — 두 축 모두 확인됐고 과열이 아니다 (NXT 체결 0건 포함)
    unverified  — 둘 중 하나라도 확인되지 않아 계산하지 않았다

`unverified` 는 False 가 아니다. 호출자가 명시적으로 허용하지 않는 한
`NxtUnverifiedError` 로 막는다.
"""
from __future__ import annotations

from dataclasses import dataclass

OVERHEATED = "overheated"
CALM = "calm"
UNVERIFIED = "unverified"

# --- 축 1: 거래소 확인 방식 -------------------------------------------------
#: 이벤트마다 거래소가 확인된 입력 (예: 종목코드 접미사에서 도출).
PER_EVENT_VENUE = "per_event_venue"
#: 확인 수단이 없는 입력. raw-v1 원본과 venue="unknown" 인 raw-v2 가 여기 해당한다.
VENUE_UNVERIFIED = "unverified"
VENUE_RESOLUTIONS = (PER_EVENT_VENUE, VENUE_UNVERIFIED)

# --- 축 2: NXT 구간 수신 완전성 ---------------------------------------------
#: 해당 구간에 NXT 실시간을 구독해 수신하고 있었음이 확인됐다.
NXT_COVERAGE_CONFIRMED = "confirmed"
#: 확인되지 않았다. 기본값 — 현재 수집기는 6자리(KRX) 코드만 등록한다.
NXT_COVERAGE_UNCONFIRMED = "unconfirmed"
NXT_COVERAGES = (NXT_COVERAGE_CONFIRMED, NXT_COVERAGE_UNCONFIRMED)

KRX = "KRX"
NXT = "NXT"
#: 통합(최우선호가) 시장. 어느 거래소에서 체결됐는지 단정할 수 없어 NXT 귀속에 쓰지 않는다.
UNIFIED = "AL"

_UNRESOLVED_VENUES = (None, "", "unknown", "UNKNOWN")


class NxtUnverifiedError(RuntimeError):
    """거래소 또는 NXT 수신이 확인되지 않은 상태에서 과열 판정을 요구했을 때."""


def venue_from_code(code):
    """종목코드 접미사에서 거래소를 도출한다.

    키움 개발가이드(koa_devguide.xml): KRX 는 기존 6자리, NXT 는 "_NX",
    통합시장은 "_AL" 을 붙이며, 실시간 시세의 종목코드는 조회한 종목코드와 같다.
    """
    if not isinstance(code, str) or not code:
        return None
    base, _, suffix = code.partition("_")
    if not (len(base) == 6 and base.isdigit()):
        return None
    if not suffix:
        return KRX
    return {"NX": NXT, "AL": UNIFIED}.get(suffix.upper())


@dataclass(frozen=True)
class NxtVerdict:
    status: str
    exhausted: bool | None
    reason: str
    trade_count: int = 0
    gain: float | None = None
    volume: int = 0

    @property
    def verified(self) -> bool:
        return self.status != UNVERIFIED


def _unverified(reason, trade_count=0):
    return NxtVerdict(UNVERIFIED, None, reason, trade_count=trade_count)


def classify_premarket(events, *, window, gain_threshold, volume_threshold,
                       venue_resolution=VENUE_UNVERIFIED,
                       nxt_coverage=NXT_COVERAGE_UNCONFIRMED):
    """프리마켓 구간의 NXT 과열 여부를 판정한다.

    events         : {"sec": int, "price": float, "vol": int, "venue": str?} 의 목록
    window         : [시작초, 끝초) — 하루 기준 초 단위 반열린 구간
    venue_resolution / nxt_coverage : 위의 두 축. 기본값은 둘 다 '미확인' 이다.

    시간대만 맞는 이벤트를 NXT 로 간주하지 않으며, 두 축 중 하나라도 확인되지
    않으면 계산 없이 UNVERIFIED 를 돌려준다.
    """
    if venue_resolution not in VENUE_RESOLUTIONS:
        raise ValueError(f"지원하지 않는 거래소 확인 방식: {venue_resolution!r}")
    if nxt_coverage not in NXT_COVERAGES:
        raise ValueError(f"지원하지 않는 NXT 수신 확인 값: {nxt_coverage!r}")
    start, end = window
    if not isinstance(start, int) or not isinstance(end, int) or start >= end:
        raise ValueError("window 는 [시작초, 끝초) 의 정수 반열린 구간이어야 한다")

    # 관측 자체가 없으면 시장이 잠잠했다는 근거가 아니라 수집이 비어 있다는 뜻이다.
    if not events:
        return _unverified("이벤트가 하나도 없다. 수집 결손과 무거래를 구분할 수 없다")

    if venue_resolution == VENUE_UNVERIFIED:
        return _unverified("거래소 확인 수단이 없다. 시간대만으로는 NXT 체결을 확정할 수 없다")

    # 거래소를 구분할 수 있다는 것과 NXT 를 받고 있었다는 것은 별개다.
    if nxt_coverage != NXT_COVERAGE_CONFIRMED:
        return _unverified(
            "NXT 구간 수신이 확인되지 않았다. 구간 내 타 거래소 체결은 NXT 수신의 근거가 아니다")

    in_window = [e for e in events if start <= e["sec"] < end]
    unresolved = [e for e in in_window if e.get("venue") in _UNRESOLVED_VENUES]
    if unresolved:
        return _unverified(
            f"프리마켓 구간 이벤트 {len(in_window)}건 중 {len(unresolved)}건의 거래소가 미확인이다",
            trade_count=len(in_window))
    unified = [e for e in in_window if e.get("venue") == UNIFIED]
    if unified:
        return _unverified(
            f"통합시장(_AL) 체결 {len(unified)}건은 체결 거래소를 단정할 수 없어 NXT 귀속이 불가하다",
            trade_count=len(in_window))

    nxt_trades = [e for e in in_window if e["venue"] == NXT]
    if not nxt_trades:
        return NxtVerdict(CALM, False,
                          f"NXT 수신 확인됨 — 프리마켓 구간 NXT 체결 0건 "
                          f"(구간 내 타 거래소 {len(in_window)}건)")

    open_price = nxt_trades[0]["price"]
    if open_price <= 0:
        return _unverified("NXT 시가가 0 이하라 상승률을 계산할 수 없다",
                           trade_count=len(nxt_trades))

    close_price = nxt_trades[-1]["price"]
    volume = sum(int(e["vol"]) for e in nxt_trades)
    gain = (close_price - open_price) / open_price
    if gain >= gain_threshold and volume >= volume_threshold:
        return NxtVerdict(OVERHEATED, True,
                          f"NXT 프리마켓 {gain * 100:.2f}% 상승, {volume:,}주 — 과열 임계 초과",
                          trade_count=len(nxt_trades), gain=gain, volume=volume)
    return NxtVerdict(CALM, False,
                      f"NXT 프리마켓 {gain * 100:.2f}% 상승, {volume:,}주 — 과열 임계 미만",
                      trade_count=len(nxt_trades), gain=gain, volume=volume)


def require_verdict(verdict, *, allow_unverified=False):
    """UNVERIFIED 를 조용히 통과시키지 않는다."""
    if verdict.status == UNVERIFIED and not allow_unverified:
        raise NxtUnverifiedError(
            f"NXT 과열 판정을 낼 수 없다: {verdict.reason}. "
            "거래소 확인과 NXT 수신 확인을 붙이거나 --allow-unverified-nxt 로 명시적으로 허용해야 한다")
    return verdict
