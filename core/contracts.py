"""
core/contracts.py — 계층 간 데이터 계약 (Data Contracts)

이 모듈은 '어떤 계층도 다른 계층의 내부 구현을 알 필요가 없게' 만드는 경계다.

    L3 전략   : MarketSnapshot 을 받아 Signal 을 낸다.  DB 도 브로커도 모른다.
    L4 실행   : Signal 을 받아 Order 를 내고 Fill 을 돌려준다.  전략 로직을 모른다.
    L5 런스토어: Trade 만 저장한다.  어떤 엔진이 만들었는지 무관하다.

여기 있는 타입이 바뀌면 모든 계층이 영향을 받는다. 변경에 신중할 것.

참고: ARCHITECTURE_V2.md §2, §6
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping, Optional

__all__ = [
    "Side",
    "OrderType",
    "SignalKind",
    "MarketSnapshot",
    "Signal",
    "Order",
    "Fill",
    "Position",
    "Trade",
    "TRADE_COLUMNS",
]


# ---------------------------------------------------------------------------
# 열거형
# ---------------------------------------------------------------------------

class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"           # 시장가
    LIMIT = "limit"             # 지정가
    BEST_ASK = "best_ask"       # 최우선 매도호가 (즉시 체결 목적 매수)
    BEST_BID = "best_bid"       # 최우선 매수호가 (즉시 체결 목적 매도)


class SignalKind(str, Enum):
    ENTRY = "entry"
    EXIT = "exit"


# ---------------------------------------------------------------------------
# L2 → L3 : 전략이 보는 세계의 전부
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """
    특정 시점 t 의 시장 상태 + 그 시점 피처 값.

    전략은 오직 이 객체만 본다. SQLite 커넥션도, parquet 경로도, 브로커도 모른다.
    백테스트에서는 과거 데이터로 채워지고, 실전에서는 실시간 스트림으로 채워진다.
    전략 입장에서 둘은 구분 불가능해야 한다 — 그게 이 설계의 핵심이다.

    주의: features 에는 이미 warmup 이 충족된 값만 들어온다.
          아직 준비되지 않은 피처는 키 자체가 없다 (None 이 아니라 KeyError).
          전략이 조용히 0 을 쓰는 사고를 막기 위함이다.
    """
    code: str                       # 6자리 종목코드
    ts: datetime                    # 이벤트 시각 (tz-aware, Asia/Seoul)
    sec: int                        # 당일 누적 초 (09:00:00 -> 32400) — 비교용 빠른 경로

    price: float                    # 최종 체결가
    bid_p1: float                   # 최우선 매수호가
    ask_p1: float                   # 최우선 매도호가

    features: Mapping[str, float] = field(default_factory=dict)

    # ---- 전략이 쓰는 편의 접근자 -------------------------------------------

    def f(self, name: str) -> float:
        """
        피처 조회. 준비되지 않은 피처는 KeyError 를 낸다 (의도적).

        거시(일봉) 피처와 미시(틱/초봉) 피처를 같은 인터페이스로 조회한다.
        해상도 차이는 L2 가 흡수한다.  ARCHITECTURE_V2.md §3.6
        """
        return self.features[name]

    def has(self, name: str) -> bool:
        return name in self.features

    @property
    def spread_pct(self) -> float:
        if self.bid_p1 <= 0:
            return 1.0
        return (self.ask_p1 - self.bid_p1) / self.bid_p1


# ---------------------------------------------------------------------------
# L3 → L4 : 전략의 산출물
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Signal:
    """
    전략의 '의도'. 주문이 아니다.

    전략은 수량도 자본도 모른다. 얼마나 살지는 L4(StrategyAccount)가 정한다.
    이 분리 덕분에 같은 전략을 자본 규모만 바꿔 여러 계좌에 올릴 수 있다.
    """
    kind: SignalKind
    code: str
    ts: datetime
    side: Side
    strength: float = 1.0           # 0.0~1.0 — 계좌가 비중 결정에 참고 (기본: 전량)
    reason: str = ""                # "시가돌파+OBI" / "2틱 반락 컷" 등
    meta: Mapping[str, Any] = field(default_factory=dict)
    # meta 에 진입 시점 피처 스냅샷을 넣는다 -> Trade.signal_meta 로 흘러가
    # "왜 이 거래가 졌는가"를 원천 DB 재조회 없이 답할 수 있게 된다. §6.2


@dataclass(frozen=True, slots=True)
class Order:
    """L4 가 Signal 을 자본·리스크 한도와 결합해 만든 실행 가능한 지시."""
    code: str
    side: Side
    qty: int
    order_type: OrderType
    limit_price: Optional[float] = None
    ts: Optional[datetime] = None
    strategy_id: str = ""
    signal_reason: str = ""


@dataclass(frozen=True, slots=True)
class Fill:
    """체결 결과. 백테스트/모의/실전이 모두 이 형태로 돌려준다."""
    order_id: str
    code: str
    side: Side
    qty: int
    price: float
    ts: datetime
    fee: float = 0.0
    is_partial: bool = False


@dataclass(slots=True)
class Position:
    """보유 포지션. 계좌가 소유한다."""
    code: str
    qty: int
    avg_price: float
    opened_at: datetime
    strategy_id: str = ""

    peak_price: float = 0.0         # 트레일링 청산용 고점 추적
    trough_price: float = 0.0       # MAE 계산용 저점 추적

    def mark(self, price: float) -> None:
        """현재가 반영 — 고점/저점 갱신."""
        self.peak_price = max(self.peak_price, price) if self.peak_price else price
        self.trough_price = min(self.trough_price, price) if self.trough_price else price

    def unrealized_pct(self, price: float) -> float:
        if self.avg_price <= 0:
            return 0.0
        return (price - self.avg_price) / self.avg_price


# ---------------------------------------------------------------------------
# L4 → L5 : 표준 결과 레코드
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Trade:
    """
    완결된 1회 거래. 모든 엔진(bar/tick)과 모든 모드(backtest/paper/live)의
    출력이 이 형태로 수렴한다.

    현재 engine.py 의 20컬럼 CSV 와 nxt_tick_engine.py 의 history dict 를 통합한 것.
    ARCHITECTURE_V2.md §6.2
    """
    run_id: str
    strategy_id: str
    strategy_version: str
    exit_rule: str                  # 어떤 청산 규칙의 결과인가 (3대 컷 동시 비교용)

    code: str
    name: str
    date: date

    entry_time: str                 # "HHMMSS"
    entry_price: float
    exit_time: str
    exit_price: float
    qty: int

    gross_pnl_pct: float
    fee_pct: float
    net_pnl_pct: float

    mae_pct: float                  # 최대 역행폭 (기존 mdd)
    mfe_pct: float                  # 최대 순행폭 (기존 mdu)
    holding_sec: int
    exit_reason: str

    signal_meta: Mapping[str, Any] = field(default_factory=dict)


#: Trade 를 parquet/CSV 로 직렬화할 때의 컬럼 순서 (런 스토어가 사용)
TRADE_COLUMNS: tuple[str, ...] = (
    "run_id", "strategy_id", "strategy_version", "exit_rule",
    "code", "name", "date",
    "entry_time", "entry_price", "exit_time", "exit_price", "qty",
    "gross_pnl_pct", "fee_pct", "net_pnl_pct",
    "mae_pct", "mfe_pct", "holding_sec", "exit_reason",
    "signal_meta",
)
