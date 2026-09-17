"""NXT 프리마켓 과열 판정 — 거래소가 확인되지 않으면 계산하지 않는다.

기존 `nxt_tick_engine` 은 08:00~08:50 이라는 **시간대만으로** 이벤트를 NXT
프리마켓 체결로 간주하고 과열 여부를 계산했다. 시간대는 거래소를 확정하지
못한다. 같은 구간에 KRX 장전 시간외종가(08:30~08:40) 체결이나 장전 동시호가
관련 이벤트가 섞여 들어올 수 있고, 실제로 현재 수집 경로는 08:00~08:30 에
아무 이벤트도 받지 못한 채 08:30 부터 이벤트를 받는다(2026-09-16/17 관측).

더 위험한 것은 결손의 방향이었다. 원래 코드는

    is_nxt_exhausted = False          # 기본값
    if nxt_trades: ...                # 있을 때만 갱신

이라서 **"NXT 를 보고 과열이 아니었다"** 와 **"NXT 데이터가 아예 없다"** 가
똑같이 False 로 접혔다. 둘 다 09:00 정규장 진입 허용으로 흘러, 데이터 결손이
조용히 '안전' 으로 바뀐다. 이 모듈은 그 두 갈래를 분리한다.

판정은 세 가지다.
    overheated  — 거래소가 확인된 NXT 체결로 과열 임계를 넘었다
    calm        — 거래소가 확인됐고 과열이 아니다 (NXT 체결 0건 포함)
    unverified  — 거래소를 확인할 수 없어 계산하지 않았다

`unverified` 는 False 가 아니다. 호출자가 명시적으로 허용하지 않는 한
`NxtUnverifiedError` 로 막는다. 계산을 건너뛴 결과를 '미과열' 로 쓰지 않기
위해서다.
"""
from __future__ import annotations

from dataclasses import dataclass

OVERHEATED = "overheated"
CALM = "calm"
UNVERIFIED = "unverified"

#: 이벤트마다 원문에서 확인된 거래소 값을 갖고 있는 경우에만 계산한다.
PER_EVENT_VENUE = "per_event_venue"
#: 거래소 확인 수단이 없는 입력. raw-v1 원본과 venue="unknown" 인 raw-v2 가 여기 해당한다.
VENUE_UNVERIFIED = "unverified"
VENUE_RESOLUTIONS = (PER_EVENT_VENUE, VENUE_UNVERIFIED)

#: 거래소가 확인되지 않은 것으로 취급하는 값들.
_UNRESOLVED_VENUES = (None, "", "unknown", "UNKNOWN")


class NxtUnverifiedError(RuntimeError):
    """거래소 미확인 상태에서 NXT 과열 판정을 요구했을 때."""


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


def classify_premarket(events, *, window, gain_threshold, volume_threshold,
                       venue_resolution=VENUE_UNVERIFIED, nxt_venue="NXT"):
    """프리마켓 구간의 NXT 과열 여부를 판정한다.

    events           : {"sec": int, "price": float, "vol": int, "venue": str?} 의 목록
    window           : [시작초, 끝초) — 하루 기준 초 단위 반열린 구간
    venue_resolution : PER_EVENT_VENUE 또는 VENUE_UNVERIFIED

    시간대만 맞는 이벤트를 NXT 로 간주하지 않는다. 거래소가 확인되지 않으면
    계산 없이 UNVERIFIED 를 돌려준다.
    """
    if venue_resolution not in VENUE_RESOLUTIONS:
        raise ValueError(f"지원하지 않는 거래소 확인 방식: {venue_resolution!r}")
    start, end = window
    if not isinstance(start, int) or not isinstance(end, int) or start >= end:
        raise ValueError("window 는 [시작초, 끝초) 의 정수 반열린 구간이어야 한다")

    if venue_resolution == VENUE_UNVERIFIED:
        return NxtVerdict(UNVERIFIED, None,
                          "거래소 확인 수단이 없다. 시간대만으로는 NXT 체결을 확정할 수 없다")

    in_window = [e for e in events if start <= e["sec"] < end]
    unresolved = [e for e in in_window if e.get("venue") in _UNRESOLVED_VENUES]
    if unresolved:
        return NxtVerdict(
            UNVERIFIED, None,
            f"프리마켓 구간 이벤트 {len(in_window)}건 중 {len(unresolved)}건의 거래소가 미확인이다",
            trade_count=len(in_window))

    nxt_trades = [e for e in in_window if e["venue"] == nxt_venue]
    if not nxt_trades:
        return NxtVerdict(CALM, False,
                          f"거래소 확인됨 — 프리마켓 구간 NXT 체결 0건 (구간 내 타 거래소 {len(in_window)}건)")

    open_price = nxt_trades[0]["price"]
    if open_price <= 0:
        return NxtVerdict(UNVERIFIED, None,
                          "NXT 시가가 0 이하라 상승률을 계산할 수 없다",
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
            "거래소 확인을 붙이거나 --allow-unverified-nxt 로 명시적으로 허용해야 한다")
    return verdict
