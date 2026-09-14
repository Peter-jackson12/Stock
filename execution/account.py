"""
execution/account.py — 전략별 계좌 + 전역 안전장치 (Phase E)

운용 방식: **전략별 계좌/자본 분리**
전략끼리 자본을 두고 경쟁하지 않으므로 중재(arbitration) 로직이 필요 없다.
각 전략은 자기 예산 안에서 독립적으로 판단한다. 설계 복잡도가 크게 내려간다.

⚠️ 다만 **자본은 분리되어도 리스크는 분리되지 않는다.**
   전략 3개가 모두 "09:05 코스닥 중소형주 돌파"를 노린다면, 계좌가 나뉘어 있어도
   같은 시장 충격에 동시에 맞는다. 계좌 분리는 회계적 분리지 리스크 분산이 아니다.
   그래서 계좌 위에 얇은 전역 계층 하나를 남긴다 (아래 GlobalGuard).

참고: ARCHITECTURE_V2.md §5.2, §5.3
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from core.contracts import Order, OrderType, Position, Side, Signal
from execution.broker import Broker

__all__ = ["StrategyAccount", "GlobalGuard", "KillSwitch", "Heartbeat"]


PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 전략별 계좌
# ---------------------------------------------------------------------------

@dataclass
class StrategyAccount:
    """
    하나의 전략에 할당된 자본과 리스크 한도.

    전략은 Signal(의도)만 내고, 얼마나 살지는 여기서 정한다.
    이 분리 덕분에 같은 전략을 자본 규모만 바꿔 여러 계좌에 올릴 수 있다.
    """

    strategy_id: str
    broker: Broker
    capital: float
    max_positions: int = 3
    max_position_pct: float = 0.34
    daily_loss_limit_pct: float = -2.0

    _realized_pnl_today: float = field(init=False, default=0.0)
    _halted_date: Optional[date] = field(init=False, default=None)

    # -- 시그널 -> 주문 변환 ------------------------------------------------

    def size_order(self, signal: Signal, price: float) -> Optional[Order]:
        """
        TODO(Phase E): Signal 을 자본·한도와 결합해 Order 로 변환.

        판단 순서:
          1. 당일 정지 상태인가 (daily_loss_limit 초과) -> None
          2. GlobalGuard 킬스위치가 켜져 있는가 -> None
          3. 동시 보유 종목 수가 max_positions 를 넘는가 -> None
          4. 예산 = capital * max_position_pct * signal.strength
          5. qty = int(예산 / price)  (호가단위/최소수량 고려)
          6. qty <= 0 이면 None
        """
        raise NotImplementedError("Phase E")

    # -- 상태 ---------------------------------------------------------------

    @property
    def is_halted(self) -> bool:
        return self._halted_date == date.today()

    def record_realized(self, pnl_amount: float) -> None:
        """
        TODO(Phase E): 실현손익 반영. 일일 한도 초과 시 self._halted_date 설정.
        한도에 걸리면 **조용히 멈추지 말고 알림을 보낸다** — 조용한 정지가 가장 위험하다.
        """
        raise NotImplementedError("Phase E")

    def reset_daily(self) -> None:
        self._realized_pnl_today = 0.0
        self._halted_date = None


# ---------------------------------------------------------------------------
# 전역 안전장치 — 대시보드보다 먼저 만들어야 하는 것들
# ---------------------------------------------------------------------------

class KillSwitch:
    """
    단일 플래그로 전 전략 신규 진입 즉시 중단.

    파일 기반으로 구현하는 이유: 프로세스가 응답하지 않아도,
    원격 데스크톱이 끊겨도, 파일 하나만 만들면 멈출 수 있어야 한다.
    (Phase 4 '홈 PC 원격 무인 가동' 환경에서 특히 중요)

        touch runs/.KILL      -> 전 전략 신규 진입 중단

    ⚠️ 보유 포지션 청산 여부는 별도 결정 사항이다.
       "신규 진입 중단"과 "전량 청산"을 같은 스위치로 묶으면
       일시적 이상 상황에서 불필요한 손실 확정이 발생한다.
    """

    DEFAULT_PATH = PROJECT_ROOT / "runs" / ".KILL"

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else self.DEFAULT_PATH

    @property
    def engaged(self) -> bool:
        return self.path.exists()

    def engage(self, reason: str = "") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            f"{datetime.now().isoformat()}\n{reason}\n", encoding="utf-8"
        )

    def release(self) -> None:
        self.path.unlink(missing_ok=True)


class Heartbeat:
    """
    각 전략 루프의 생존 신호.

    **조용한 정지가 가장 위험하다.** 전략이 죽어도 포지션은 남아 있고,
    아무도 모르는 채로 장이 끝난다. 주기적으로 타임스탬프를 찍고,
    감시자가 임계 시간 이상 갱신되지 않으면 알림을 보낸다.

    TODO(Phase E): 파일/SQLite 기반 기록 + 감시 프로세스에서 지연 감지
    """

    def __init__(self, strategy_id: str, stale_after_sec: int = 60) -> None:
        self.strategy_id = strategy_id
        self.stale_after_sec = stale_after_sec

    def beat(self) -> None:
        raise NotImplementedError("Phase E")

    def is_stale(self) -> bool:
        raise NotImplementedError("Phase E")


@dataclass
class GlobalGuard:
    """
    전 계좌 합산 감시. 자본은 분리되어 있어도 리스크는 상관되어 있다.

    TODO(Phase E):
      - aggregate_exposure(): 전 전략 합산 노출액 + 종목 중복도
      - global_daily_loss(): 전 계좌 합산 일일 손실 -> 한도 초과 시 킬스위치 자동 발동
      - stale_heartbeats(): 응답 없는 전략 목록
    """

    accounts: list[StrategyAccount] = field(default_factory=list)
    kill_switch: KillSwitch = field(default_factory=KillSwitch)
    global_daily_loss_limit_pct: float = -3.0

    def allows_new_entry(self) -> bool:
        return not self.kill_switch.engaged

    def check(self) -> dict:
        """주기적 호출. 이상 감지 시 알림 페이로드 반환."""
        raise NotImplementedError("Phase E")
