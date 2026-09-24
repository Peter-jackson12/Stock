"""기존 NxtResearchStrategy를 공유계좌의 순수 주문 의도 경계에 연결한다.

한 전략 종류를 여러 종목에 적용하되 종목별 전략 상태는 분리한다. 전략 인스턴스는
계좌를 직접 바꾸지 않고 SymbolIntentPort를 통해 OrderIntent/CancelIntent만 만든다.
다중 전략 arbitration/자본 분할은 이 모듈의 범위가 아니다.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from execution.portfolio_simulator import (
    CancelIntent,
    OrderIntent,
    PortfolioSnapshot,
    TERMINAL,
)
from strategies.nxt_breakout.tick_research import NxtResearchStrategy


@dataclass(frozen=True)
class NxtPortfolioSignal:
    time_ns: int
    code: str
    side: str
    quantity: int
    reason: str


class _SymbolIntentPort:
    """NxtResearchStrategy가 기대하는 최소 sim 표면만 제공한다."""

    def __init__(self, *, code: str, snapshot: PortfolioSnapshot,
                 strategy: NxtResearchStrategy, prefix: str) -> None:
        self.code = code
        self.now = snapshot.now
        self.fills = tuple(fill for fill in snapshot.fills if fill.code == code)
        live = tuple(order for order in snapshot.orders
                     if order.code == code and order.status not in TERMINAL)
        self.orders = MappingProxyType({order.order_id: order for order in live})
        self._strategy = strategy
        self._prefix = prefix
        self.intents: list[OrderIntent | CancelIntent] = []

    def submit(self, order_id: str, side: str, quantity: int) -> str:
        global_id = f"{self._prefix}:{self.code}:{order_id}"
        reason = ""
        if self._strategy.signals:
            time_ns, signal_side, signal_qty, signal_reason = self._strategy.signals[-1]
            if time_ns == self.now and signal_side == side and signal_qty == quantity:
                reason = signal_reason
        self.intents.append(OrderIntent(global_id, self.code, side, quantity, reason))
        return global_id

    def cancel(self, order_id: str) -> bool:
        order = self.orders.get(order_id)
        if order is None:
            return False
        self.intents.append(CancelIntent(order_id))
        return True


class NxtPortfolioStrategy:
    """NXT breakout 한 전략의 종목별 상태를 하나의 공유계좌에 연결한다."""

    strategy_id = "nxt_breakout"
    adapter_version = "nxt_portfolio_adapter_v1"

    def __init__(self, *, instruments: Mapping[str, str], quantity: int,
                 exit_rule: str = "fixed", cooldown_ns: int = 10_000_000_000,
                 params=None, order_prefix: str = "nxt",
                 unknown_direction_policy: str = "strict") -> None:
        if not isinstance(instruments, Mapping) or not instruments:
            raise ValueError("nonempty instruments mapping required")
        if any(not isinstance(code, str) or not code or not isinstance(venue, str) or not venue
               for code, venue in instruments.items()):
            raise ValueError("nonempty code/venue strings required")
        if not isinstance(order_prefix, str) or not order_prefix:
            raise ValueError("nonempty order prefix required")
        self.instruments = MappingProxyType(dict(sorted(instruments.items())))
        self.quantity = quantity
        self.exit_rule = exit_rule
        self.cooldown_ns = cooldown_ns
        self.order_prefix = order_prefix
        self.unknown_direction_policy = unknown_direction_policy
        self._states = {
            code: NxtResearchStrategy(
                quantity=quantity,
                exit_rule=exit_rule,
                cooldown_ns=cooldown_ns,
                params=deepcopy(params),
                unknown_direction_policy=unknown_direction_policy,
            )
            for code in self.instruments
        }
        self.signals: list[NxtPortfolioSignal] = []

    @property
    def params(self):
        first = next(iter(self._states.values()))
        return deepcopy(first.params)

    def settings(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "adapter_version": self.adapter_version,
            "instruments": dict(self.instruments),
            "quantity": self.quantity,
            "exit_rule": self.exit_rule,
            "cooldown_ns": self.cooldown_ns,
            "order_prefix": self.order_prefix,
            "unknown_direction_policy": self.unknown_direction_policy,
            "params": self.params,
        }

    def __call__(self, view, snapshot: PortfolioSnapshot):
        code = view.event.code
        expected_venue = self.instruments.get(code)
        if expected_venue is None or expected_venue != view.event.venue:
            raise ValueError("event outside declared NXT portfolio instruments")
        strategy = self._states[code]
        before = len(strategy.signals)
        port = _SymbolIntentPort(
            code=code,
            snapshot=snapshot,
            strategy=strategy,
            prefix=self.order_prefix,
        )
        strategy(view, port)
        for time_ns, side, quantity, reason in strategy.signals[before:]:
            self.signals.append(NxtPortfolioSignal(time_ns, code, side, quantity, reason))
        return tuple(port.intents)
