"""
execution/broker.py — L4 브로커 포트 (Phase E)

이 설계의 핵심 한 문장:
    **전략 코드는 자신이 백테스트 중인지 실전 중인지 알지 못한다.**

전략(L3)은 Signal 을 낼 뿐이고, 그 Signal 이 과거 데이터 재생으로 채워지는지
실계좌 주문으로 나가는지는 여기서 갈린다. 어댑터만 갈아끼운다.

    BacktestBroker   과거 데이터 재생 — latency_sec 후 ask_p1, 수수료 차감
    PaperBroker      모의투자 — 실시간 호가 + 가상 체결 (실전 직전 검증)
    LiveBroker       실계좌 — KIS / 키움 REST, 주문번호 추적, 부분체결 처리

현재 nxt_tick_engine.py 의 지연 체결 로직이 이미 BacktestBroker 의 원형이다:

    if pending_buy and cur_sec >= target_buy_sec:
        fill_price = tick["ask_p1"]

이 로직을 엔진 루프에서 꺼내 BacktestBroker.submit() 안으로 옮기면
그 자리에 LiveBroker 를 끼울 수 있게 된다.
**엔진을 다시 쓰는 게 아니라 이미 있는 로직의 소속을 바꾸는 작업이다.**

참고: ARCHITECTURE_V2.md §5
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from core.contracts import Fill, MarketSnapshot, Order, Position

__all__ = ["Broker", "BacktestBroker", "PaperBroker", "LiveBroker"]


@runtime_checkable
class Broker(Protocol):
    """모든 체결 경로가 지키는 포트."""

    def submit(self, order: Order) -> str:
        """주문 제출. 주문 ID 반환."""
        ...

    def cancel(self, order_id: str) -> bool:
        ...

    def positions(self) -> list[Position]:
        ...

    def cash(self) -> float:
        ...

    def poll_fills(self) -> list[Fill]:
        """직전 호출 이후 발생한 체결 목록."""
        ...


# ---------------------------------------------------------------------------
# 백테스트
# ---------------------------------------------------------------------------

@dataclass
class BacktestBroker:
    """
    과거 데이터 재생 기반 체결 시뮬레이터.

    보수적 체결 원칙 (현재 nxt_tick_engine 이 이미 지키고 있는 것):
      - 매수는 latency_sec 후 시점의 **ask_p1**(최우선 매도호가)로 체결
        -> 즉시 체결 환상을 깨고 실전 슬리피지를 강제 주입
      - 매도는 **bid_p1**(최우선 매수호가)로 체결
      - 수수료·세금은 양방향 차감

    TODO(Phase E):
      - pending 주문 큐 + latency 카운트다운
      - 호가 잔량 대비 주문 수량이 크면 부분체결/호가 소진 모델링
        (현재는 ask_p1 무한 유동성 가정 — 소형주에서 낙관적일 수 있음)
      - 체결 실패(호가 증발) 케이스
    """

    latency_sec: int = 1
    fee_rate: float = 0.0020
    initial_cash: float = 10_000_000.0

    _cash: float = field(init=False, default=0.0)
    _positions: dict[str, Position] = field(init=False, default_factory=dict)
    _pending: list[tuple[int, Order]] = field(init=False, default_factory=list)
    _fills: list[Fill] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self._cash = self.initial_cash

    def on_snapshot(self, snap: MarketSnapshot) -> None:
        """
        TODO(Phase E): 엔진 루프가 매 이벤트마다 호출.
        latency 가 만료된 pending 주문을 이 스냅샷의 호가로 체결시킨다.
        """
        raise NotImplementedError("Phase E")

    def submit(self, order: Order) -> str:
        raise NotImplementedError("Phase E")

    def cancel(self, order_id: str) -> bool:
        raise NotImplementedError("Phase E")

    def positions(self) -> list[Position]:
        return list(self._positions.values())

    def cash(self) -> float:
        return self._cash

    def poll_fills(self) -> list[Fill]:
        out, self._fills = self._fills, []
        return out


# ---------------------------------------------------------------------------
# 모의투자
# ---------------------------------------------------------------------------

class PaperBroker:
    """
    실시간 호가 + 가상 체결. 실전 투입 직전의 필수 관문.

    ⚠️ 모의투자 단계에서 반드시 측정할 것 (ARCHITECTURE_V2.md §5.4):
       같은 날짜에 대해
         (a) 실시간으로 발생한 시그널 목록
         (b) 장 마감 후 그날 데이터로 백테스트를 돌린 시그널 목록
       이 둘이 일치하는가.

       불일치는 곧 피처 패리티(§3.3)가 깨졌다는 뜻이고,
       그 상태로 실계좌에 올리면 안 된다.
    """

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError("Phase E")


# ---------------------------------------------------------------------------
# 실계좌
# ---------------------------------------------------------------------------

class LiveBroker:
    """
    KIS / 키움 REST 어댑터.

    TODO(Phase E):
      - 주문 제출 -> 주문번호 수신 -> 체결 통보 수신의 비동기 흐름
      - 부분체결 누적 처리
      - 재접속 후 미체결 주문 복구 (프로세스 재시작 시 고아 주문 방지)
      - 주문/체결 전량을 별도 로그로 영구 기록 (사후 대조용, 절대 생략 금지)

    ⚠️ 이 클래스를 구현하기 전에 execution/account.py 의 안전장치
       (KillSwitch, Heartbeat)가 먼저 동작해야 한다. 화면보다 먼저다.
    """

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError("Phase E")
