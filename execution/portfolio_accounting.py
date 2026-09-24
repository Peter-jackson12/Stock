"""Exact pure accounting contract for PortfolioFill ledgers.

This module does not mutate PortfolioSimulator and does not infer market marks.
It separates three concerns:

1. Fill accounting uses fee-inclusive weighted-average cost for long-only positions.
2. Cashflow is recomputed independently from fills for ledger reconciliation.
3. Open-position valuation accepts only explicit bid marks supplied by a caller
   that has already verified freshness and quote validity. Missing marks stay
   unavailable; there is no last-trade/stale-quote fallback.

Mark-to-bid values are gross of any hypothetical future exit fee. Actual sell
fees are recognized only when a sell fill exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from types import MappingProxyType
from typing import Mapping

from execution.portfolio_simulator import PortfolioFill


COST_BASIS_METHOD = "weighted_average_fee_inclusive_v0"
MARKING_POLICY = "explicit_fresh_valid_bid_gross_of_hypothetical_exit_fee_v0"


def _fraction(value, name: str) -> Fraction:
    if isinstance(value, Fraction):
        result = value
    elif type(value) in (int, str, Decimal):
        try:
            result = Fraction(value)
        except (ValueError, ZeroDivisionError, InvalidOperation) as exc:
            raise ValueError(f"invalid {name}") from exc
    else:
        raise ValueError(f"invalid {name}")
    return result


@dataclass(frozen=True)
class PositionAccounting:
    code: str
    quantity: int
    cost_basis: Fraction

    @property
    def average_cost(self) -> Fraction | None:
        return None if self.quantity == 0 else self.cost_basis / self.quantity


@dataclass(frozen=True)
class PortfolioAccountingLedger:
    cost_basis_method: str
    realized_pnl: Fraction
    cash_delta: Fraction
    fill_count: int
    positions: tuple[PositionAccounting, ...]

    def position(self, code: str) -> PositionAccounting | None:
        return next((item for item in self.positions if item.code == code), None)


@dataclass(frozen=True)
class PortfolioValuation:
    marking_policy: str
    hypothetical_exit_fee_included: bool
    realized_pnl: Fraction
    unrealized_pnl: Fraction | None
    total_pnl: Fraction | None
    equity: Fraction | None
    initial_cash: Fraction
    current_cash: Fraction
    expected_cash_from_fills: Fraction
    cash_reconciled: bool
    accounting_identity_reconciled: bool | None
    unpriced_codes: tuple[str, ...]
    marks: Mapping[str, Fraction]


def account_portfolio_fills(fills) -> PortfolioAccountingLedger:
    """Build an exact long-only weighted-average-cost ledger from fills."""
    realized = Fraction(0)
    cash_delta = Fraction(0)
    positions: dict[str, tuple[int, Fraction]] = {}
    venues: dict[str, str] = {}
    fill_count = 0
    last_time = -1

    for fill in fills:
        if type(fill) is not PortfolioFill:
            raise ValueError("PortfolioFill ledger required")
        if not isinstance(fill.code, str) or not fill.code:
            raise ValueError("fill code required")
        if not isinstance(fill.venue, str) or not fill.venue:
            raise ValueError("fill venue required")
        previous_venue = venues.get(fill.code)
        if previous_venue is not None and previous_venue != fill.venue:
            raise ValueError("one venue per code accounting contract required")
        venues[fill.code] = fill.venue
        if fill.side not in ("buy", "sell"):
            raise ValueError("fill side must be buy or sell")
        if type(fill.quantity) is not int or fill.quantity <= 0:
            raise ValueError("positive integer fill quantity required")
        if type(fill.time_ns) is not int or fill.time_ns < 0 or fill.time_ns < last_time:
            raise ValueError("nondecreasing nonnegative fill time required")
        if type(fill.quote_seq) is not int or fill.quote_seq < 1:
            raise ValueError("positive fill quote_seq required")

        price = _fraction(fill.price, "fill price")
        fee = _fraction(fill.fee, "fill fee")
        if price <= 0:
            raise ValueError("positive fill price required")
        if fee < 0:
            raise ValueError("nonnegative fill fee required")

        quantity, cost_basis = positions.get(fill.code, (0, Fraction(0)))
        gross = price * fill.quantity

        if fill.side == "buy":
            total_cost = gross + fee
            quantity += fill.quantity
            cost_basis += total_cost
            cash_delta -= total_cost
        else:
            if fill.quantity > quantity:
                raise ValueError("long-only accounting cannot sell more than held quantity")
            net_proceeds = gross - fee
            released_cost = cost_basis * fill.quantity / quantity
            realized += net_proceeds - released_cost
            cash_delta += net_proceeds
            quantity -= fill.quantity
            cost_basis -= released_cost
            if quantity == 0:
                cost_basis = Fraction(0)

        if quantity < 0 or cost_basis < 0:
            raise ArithmeticError("accounting state cannot become negative")
        positions[fill.code] = (quantity, cost_basis)
        fill_count += 1
        last_time = fill.time_ns

    records = tuple(
        PositionAccounting(code, quantity, cost_basis)
        for code, (quantity, cost_basis) in sorted(positions.items())
    )
    return PortfolioAccountingLedger(
        cost_basis_method=COST_BASIS_METHOD,
        realized_pnl=realized,
        cash_delta=cash_delta,
        fill_count=fill_count,
        positions=records,
    )


def value_portfolio_at_bid_marks(
    ledger: PortfolioAccountingLedger,
    *,
    initial_cash,
    current_cash,
    bid_marks: Mapping[str, object],
) -> PortfolioValuation:
    """Value open long positions at explicit caller-validated bid marks.

    Missing/None marks make unrealized PnL, total PnL and equity unavailable.
    No fallback price is invented. Hypothetical future sell fees are not
    subtracted from bid marks in this v0 contract.
    """
    if type(ledger) is not PortfolioAccountingLedger:
        raise ValueError("PortfolioAccountingLedger required")
    if not isinstance(bid_marks, Mapping):
        raise ValueError("bid_marks mapping required")

    initial = _fraction(initial_cash, "initial cash")
    current = _fraction(current_cash, "current cash")
    if initial < 0 or current < 0:
        raise ValueError("cash must be nonnegative")

    marks: dict[str, Fraction] = {}
    unpriced: list[str] = []
    market_value = Fraction(0)
    unrealized = Fraction(0)

    open_positions = [position for position in ledger.positions if position.quantity > 0]
    for position in open_positions:
        value = bid_marks.get(position.code)
        if value is None:
            unpriced.append(position.code)
            continue
        mark = _fraction(value, f"bid mark for {position.code}")
        if mark <= 0:
            raise ValueError("positive bid mark required")
        marks[position.code] = mark
        market_value += mark * position.quantity
        unrealized += mark * position.quantity - position.cost_basis

    expected_cash = initial + ledger.cash_delta
    cash_reconciled = current == expected_cash

    if unpriced:
        equity = None
        unrealized_pnl = None
        total_pnl = None
        identity = None
    else:
        equity = current + market_value
        unrealized_pnl = unrealized
        total_pnl = ledger.realized_pnl + unrealized
        identity = (
            cash_reconciled
            and total_pnl == equity - initial
        )

    return PortfolioValuation(
        marking_policy=MARKING_POLICY,
        hypothetical_exit_fee_included=False,
        realized_pnl=ledger.realized_pnl,
        unrealized_pnl=unrealized_pnl,
        total_pnl=total_pnl,
        equity=equity,
        initial_cash=initial,
        current_cash=current,
        expected_cash_from_fills=expected_cash,
        cash_reconciled=cash_reconciled,
        accounting_identity_reconciled=identity,
        unpriced_codes=tuple(sorted(unpriced)),
        marks=MappingProxyType(dict(sorted(marks.items()))),
    )
