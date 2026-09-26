"""MWFD-05 frozen economic outcome contract (schema binding freeze).

Primary economic scale = net return relative to executed entry notional.
KRW PnL is kept as a secondary descriptive outcome only.

These are pure functions over MWFD-04 trade / candidate-cell records. They do not
aggregate across candidates or cells and compute no association with factors.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import statistics
from typing import Iterable, Mapping

FAST_FEE_RATE = 0.001          # MWFD-04 account.fee_rate, applied per side to fill notional
FEE_TOLERANCE_KRW = 1e-6
RECONCILE_RELATIVE_TOLERANCE = 1e-9
BPS = 10_000.0

CANDIDATE_CELL_PRIMARY_STATE = "TRADED_FLAT"
EXCLUDED_FROM_CONDITIONAL_ECONOMICS = ("NO_TRADE_FLAT", "NO_TRADE_OPEN_ORDER", "TRADED_FLAT_OPEN_ORDER",
                                       "OPEN_POSITION", "ERROR_INCOMPLETE")


class OutcomeContractError(ValueError):
    """A record violates the frozen outcome contract; it must not be coerced into a value."""


def _positive_finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise OutcomeContractError(f"{name} must be a positive finite number")
    return float(value)


def _finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise OutcomeContractError(f"{name} must be finite")
    return float(value)


@dataclass(frozen=True)
class TradeOutcome:
    entry_executed_notional_krw: float
    gross_trade_pnl_krw: float
    transaction_cost_krw: float
    net_trade_pnl_krw: float
    gross_trade_return_bps: float
    transaction_cost_bps: float
    net_trade_return: float

    @property
    def net_trade_return_pct(self) -> float:
        return 100.0 * self.net_trade_return

    @property
    def net_trade_return_bps(self) -> float:
        return BPS * self.net_trade_return


def trade_outcome(trade: Mapping, *, fee_rate: float = FAST_FEE_RATE) -> TradeOutcome:
    """Frozen trade-grain outcome for one COMPLETED trade row of trades.jsonl.

    entry_executed_notional = entry_fill.price × entry_fill.quantity (fees excluded).
    net_trade_pnl = stored net_pnl, verified == (exit − entry) × quantity − (entry fee + exit fee)
    with each fee == fill price × quantity × fee_rate. Denominator never uses cash, equity,
    the 100,000,000 KRW gate threshold, or market cap.
    """
    if trade.get("status") != "COMPLETED":
        raise OutcomeContractError("only COMPLETED trades have a realized trade outcome")
    entry, exit_ = trade.get("entry_fill"), trade.get("exit_fill")
    if not entry or not exit_:
        raise OutcomeContractError("completed trade requires entry and exit fills")
    quantity = entry.get("quantity")
    if type(quantity) is not int or quantity <= 0 or exit_.get("quantity") != quantity:
        raise OutcomeContractError("positive integer quantity equal on both fills required")
    entry_price = _positive_finite(entry.get("price"), "entry price")
    exit_price = _positive_finite(exit_.get("price"), "exit price")
    entry_fee, exit_fee = _finite(entry.get("fee"), "entry fee"), _finite(exit_.get("fee"), "exit fee")
    for price, fee, side in ((entry_price, entry_fee, "entry"), (exit_price, exit_fee, "exit")):
        if abs(fee - price * quantity * fee_rate) > FEE_TOLERANCE_KRW:
            raise OutcomeContractError(f"{side} fee is not fill notional × fee_rate")
    notional = entry_price * quantity
    gross = (exit_price - entry_price) * quantity
    cost = entry_fee + exit_fee
    net = _finite(trade.get("net_pnl"), "net_pnl")
    if abs(net - (gross - cost)) > FEE_TOLERANCE_KRW * max(1.0, abs(net)):
        raise OutcomeContractError("stored net_pnl is not gross − fees")
    stored_gross, stored_fees = trade.get("gross_pnl"), trade.get("fees")
    if stored_gross is not None and abs(stored_gross - gross) > FEE_TOLERANCE_KRW * max(1.0, abs(gross)):
        raise OutcomeContractError("stored gross_pnl disagrees with fills")
    if stored_fees is not None and abs(stored_fees - cost) > FEE_TOLERANCE_KRW * max(1.0, cost):
        raise OutcomeContractError("stored fees disagree with fills")
    return TradeOutcome(
        entry_executed_notional_krw=notional,
        gross_trade_pnl_krw=gross,
        transaction_cost_krw=cost,
        net_trade_pnl_krw=net,
        gross_trade_return_bps=BPS * gross / notional,
        transaction_cost_bps=BPS * cost / notional,
        net_trade_return=net / notional,
    )


@dataclass(frozen=True)
class CandidateCellOutcome:
    completed_trade_count: int
    cell_total_entry_notional_krw: float
    cell_total_net_pnl_krw: float
    net_return_on_entry_notional: float
    mean_trade_return_bps: float
    median_trade_return_bps: float
    positive_trade_rate: float

    @property
    def net_return_on_entry_notional_pct(self) -> float:
        return 100.0 * self.net_return_on_entry_notional

    @property
    def net_return_on_entry_notional_bps(self) -> float:
        return BPS * self.net_return_on_entry_notional


def candidate_cell_outcome(state: str, candidate_cell: Mapping, trades: Iterable[Mapping], *,
                           fee_rate: float = FAST_FEE_RATE) -> CandidateCellOutcome:
    """Frozen conditional economic outcome for one TRADED_FLAT candidate-cell.

    Not an account, compounded, portfolio or daily capital return: the same capital can
    be reused across the cell's trades. Other states are rejected, never returned as 0%.
    """
    if state != CANDIDATE_CELL_PRIMARY_STATE:
        raise OutcomeContractError(f"{state} is excluded from conditional economics (not 0%)")
    rows = list(trades)
    if any(row.get("status") != "COMPLETED" for row in rows):
        raise OutcomeContractError("TRADED_FLAT candidate-cell must contain only COMPLETED trades")
    outcomes = [trade_outcome(row, fee_rate=fee_rate) for row in rows]
    if not outcomes or len(outcomes) != candidate_cell.get("completed_trades"):
        raise OutcomeContractError("trade rows do not reconcile with completed_trades")
    notional = math.fsum(o.entry_executed_notional_krw for o in outcomes)
    net = math.fsum(o.net_trade_pnl_krw for o in outcomes)
    stored = _finite(candidate_cell.get("fast_net_result"), "fast_net_result")
    if abs(net - stored) > max(FEE_TOLERANCE_KRW, RECONCILE_RELATIVE_TOLERANCE * abs(stored)):
        raise OutcomeContractError("Σ completed net_pnl does not reconcile with fast_net_result")
    returns = [o.net_trade_return_bps for o in outcomes]
    return CandidateCellOutcome(
        completed_trade_count=len(outcomes),
        cell_total_entry_notional_krw=notional,
        cell_total_net_pnl_krw=net,
        net_return_on_entry_notional=net / notional,
        mean_trade_return_bps=statistics.fmean(returns),
        median_trade_return_bps=statistics.median(returns),
        positive_trade_rate=sum(o.net_trade_pnl_krw > 0 for o in outcomes) / len(outcomes),
    )


def population_c_member(trade: Mapping, parent_state: str) -> dict:
    """Population C row: one completed trade plus its parent candidate-cell terminal state."""
    outcome = trade_outcome(trade)
    return {"parent_candidate_cell_state": parent_state, "net_trade_return_bps": outcome.net_trade_return_bps,
            "net_trade_pnl_krw": outcome.net_trade_pnl_krw}
