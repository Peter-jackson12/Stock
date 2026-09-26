"""MWFD-05 schema binding freeze — 경제 outcome·factor 계약 합성 회귀 (실데이터 없음)."""
from __future__ import annotations

import math

import pytest

from research.mwfd05 import economic_outcomes as eo
from research.mwfd05 import factor_contract as fc

S = 1_000_000_000


def completed(entry=10_000.0, exit_=10_100.0, qty=1, **overrides):
    fee = lambda p: p * qty * eo.FAST_FEE_RATE
    gross = (exit_ - entry) * qty
    value = dict(status="COMPLETED",
                 entry_fill=dict(price=entry, quantity=qty, fee=fee(entry), side="buy"),
                 exit_fill=dict(price=exit_, quantity=qty, fee=fee(exit_), side="sell"),
                 gross_pnl=gross, fees=fee(entry) + fee(exit_), net_pnl=gross - fee(entry) - fee(exit_))
    value.update(overrides)
    return value


# ── economic outcomes ─────────────────────────────────────────────────────

def test_trade_return_uses_executed_entry_notional_and_is_net_of_fees():
    outcome = eo.trade_outcome(completed(10_000.0, 10_100.0))
    assert outcome.entry_executed_notional_krw == 10_000.0
    assert outcome.gross_trade_return_bps == pytest.approx(100.0)
    assert outcome.transaction_cost_bps == pytest.approx(20.1)          # 10 + 10.1 bps
    assert outcome.net_trade_return_bps == pytest.approx(100.0 - 20.1)
    assert outcome.net_trade_return_pct == pytest.approx(outcome.net_trade_return_bps / 100)
    assert outcome.gross_trade_return_bps - outcome.transaction_cost_bps == pytest.approx(outcome.net_trade_return_bps)


def test_flat_price_trade_loses_about_round_trip_cost():
    assert eo.trade_outcome(completed(5_000.0, 5_000.0)).net_trade_return_bps == pytest.approx(-20.0)


def test_same_percentage_move_gives_same_return_regardless_of_price_level():
    cheap, expensive = eo.trade_outcome(completed(5_000.0, 5_050.0)), eo.trade_outcome(completed(100_000.0, 101_000.0))
    assert cheap.net_trade_return_bps == pytest.approx(expensive.net_trade_return_bps)
    assert expensive.net_trade_pnl_krw > 10 * cheap.net_trade_pnl_krw       # KRW PnL scales with price


@pytest.mark.parametrize("mutate,message", [
    (lambda t: t["entry_fill"].update(price=0.0), "entry price"),
    (lambda t: t["entry_fill"].update(price=-1.0), "entry price"),
    (lambda t: t["exit_fill"].update(price=float("nan")), "exit price"),
    (lambda t: t["entry_fill"].update(quantity=0), "quantity"),
    (lambda t: t["entry_fill"].update(fee=0.0), "fee"),
    (lambda t: t.update(net_pnl=t["gross_pnl"]), "gross − fees"),
    (lambda t: t.update(status="OPEN_POSITION"), "COMPLETED"),
    (lambda t: t.update(exit_fill=None), "fills"),
])
def test_invalid_trades_are_rejected_not_coerced(mutate, message):
    trade = completed()
    mutate(trade)
    with pytest.raises(eo.OutcomeContractError, match=message):
        eo.trade_outcome(trade)


def test_candidate_cell_aggregates_multiple_trades_on_total_notional():
    trades = [completed(10_000.0, 10_200.0), completed(20_000.0, 19_800.0)]
    net = sum(t["net_pnl"] for t in trades)
    outcome = eo.candidate_cell_outcome("TRADED_FLAT", {"completed_trades": 2, "fast_net_result": net}, trades)
    assert outcome.cell_total_entry_notional_krw == 30_000.0
    assert outcome.cell_total_net_pnl_krw == pytest.approx(net)
    assert outcome.net_return_on_entry_notional_bps == pytest.approx(net / 30_000.0 * 10_000)
    per_trade = [eo.trade_outcome(t).net_trade_return_bps for t in trades]
    assert outcome.mean_trade_return_bps == pytest.approx(sum(per_trade) / 2)
    assert outcome.net_return_on_entry_notional_bps != pytest.approx(outcome.mean_trade_return_bps)  # notional-weighted
    assert outcome.positive_trade_rate == 0.5 and outcome.completed_trade_count == 2


@pytest.mark.parametrize("state", eo.EXCLUDED_FROM_CONDITIONAL_ECONOMICS)
def test_non_traded_flat_states_are_excluded_not_zero(state):
    with pytest.raises(eo.OutcomeContractError, match="excluded"):
        eo.candidate_cell_outcome(state, {"completed_trades": 0, "fast_net_result": 0.0}, [])


def test_candidate_cell_reconciliation_is_enforced():
    trades = [completed()]
    with pytest.raises(eo.OutcomeContractError, match="reconcile"):
        eo.candidate_cell_outcome("TRADED_FLAT", {"completed_trades": 1, "fast_net_result": 999.0}, trades)
    with pytest.raises(eo.OutcomeContractError, match="completed_trades"):
        eo.candidate_cell_outcome("TRADED_FLAT", {"completed_trades": 2, "fast_net_result": trades[0]["net_pnl"]}, trades)
    with pytest.raises(eo.OutcomeContractError, match="only COMPLETED"):
        eo.candidate_cell_outcome("TRADED_FLAT", {"completed_trades": 1, "fast_net_result": 0.0},
                                  [completed(), {"status": "OPEN_POSITION"}])


def test_population_c_keeps_parent_state_for_trades_of_open_cells():
    row = eo.population_c_member(completed(), "OPEN_POSITION")
    assert row["parent_candidate_cell_state"] == "OPEN_POSITION" and math.isfinite(row["net_trade_return_bps"])


# ── factor contract ───────────────────────────────────────────────────────

def depth(received_ns=0, ask=150_000_000, bid=50_000_000, ask_status="COMPLETE", bid_status="COMPLETE"):
    return dict(received_ns=received_ns, ask_depth_notional_10=ask, bid_depth_notional_10=bid,
                ask_status=ask_status, bid_status=bid_status)


def test_f01_f03_freshness_completeness_and_formula():
    assert fc.f01_ask_depth_notional_10(depth(), 2 * S).value == 150_000_000
    assert fc.f01_ask_depth_notional_10(depth(), 2 * S + 1).missing == fc.MISSING_STALE_QUOTE
    assert fc.f01_ask_depth_notional_10(None, S).missing == fc.MISSING_NO_QUOTE
    assert fc.f01_ask_depth_notional_10(depth(ask_status="PARTIAL", ask=None), S).missing == fc.MISSING_INCOMPLETE_DEPTH
    assert fc.f03_depth_imbalance_10(depth(), S).value == pytest.approx(-0.5)
    assert fc.f03_depth_imbalance_10(depth(bid_status="PARTIAL"), S).missing == fc.MISSING_INCOMPLETE_DEPTH
    assert fc.f03_depth_imbalance_10(depth(ask=0, bid=0), S).missing == fc.MISSING_ZERO_DEPTH


def quote(seq, t, bid=100.0, ask=101.0, eligible=True, second=33000):
    return dict(seq=seq, received_ns=t, bid=bid, ask=ask, quote_eligible=eligible, market_second=second)


def test_f02_midpoint_denominator_and_rejections():
    assert fc.f02_relative_spread_bps(quote(1, 0, 100.0, 102.0), S).value == pytest.approx(2 / 101 * 10_000)
    assert fc.f02_relative_spread_bps(quote(1, 0, 100.0, 100.0), S).missing == fc.MISSING_INVALID_QUOTE
    assert fc.f02_relative_spread_bps(quote(1, 0, eligible=False), S).missing == fc.MISSING_INVALID_QUOTE
    assert fc.f02_relative_spread_bps(quote(1, 0), 3 * S).missing == fc.MISSING_STALE_QUOTE


def trade(seq, t, volume, is_buy, price=100.0):
    return dict(seq=seq, received_ns=t, trade_volume=volume, is_buy=is_buy, trade_price=price)


def test_f04_f05_window_endpoints_exclude_decision_trade_and_include_start():
    t_d = 100 * S
    trades = [trade(1, t_d - 31 * S, 1000, True),   # before window
              trade(2, t_d - 30 * S, 10, True),     # start inclusive
              trade(3, t_d - 5 * S, 30, False),
              trade(4, t_d, 7, None),               # same ns, earlier seq: included
              trade(5, t_d, 500, True)]             # the decision trade: excluded
    value = fc.f04_buy_sell_imbalance_30s(trades, 5, t_d, stream_start_ns=0)
    assert value.value == pytest.approx((10 - 30) / 40)
    assert fc.f05_traded_notional_30s(trades, 5, t_d, 0).value == pytest.approx(100.0 * (10 + 30 + 7))


def test_f04_unknown_side_and_support_rules():
    t_d = 100 * S
    assert fc.f04_buy_sell_imbalance_30s([trade(1, t_d - S, 10, None)], 9, t_d, 0).missing == fc.MISSING_NO_KNOWN_DIRECTION
    mostly_unknown = [trade(1, t_d - S, 4, True), trade(2, t_d - S, 6, None)]
    assert fc.f04_buy_sell_imbalance_30s(mostly_unknown, 9, t_d, 0).missing == fc.MISSING_UNKNOWN_DIRECTION_SHARE
    assert fc.f04_buy_sell_imbalance_30s([], 9, t_d, t_d - 29 * S).missing == fc.MISSING_TRUNCATED_WINDOW
    assert fc.f05_traded_notional_30s([], 9, t_d, 0).value == 0.0            # no trades is a valid zero


def test_f06_f07_midpoint_series_rules():
    t_d = 100 * S
    quotes = [quote(1, t_d - 40 * S, 100.0, 102.0),   # mid 101 carried forward to t_d-30s
              quote(2, t_d - 10 * S, 101.0, 103.0),   # mid 102
              quote(3, t_d - S, 101.0, 103.0),        # fresh at t_d
              quote(9, t_d, 500.0, 502.0)]            # seq >= s_d: excluded
    series = fc.MidpointSeries(quotes)
    assert fc.f06_midpoint_return_30s_bps(series, 5, t_d, 0).value == pytest.approx((102 / 101 - 1) * 10_000)
    vol = fc.f07_midpoint_realized_vol_30s_bps(series, 5, t_d, 0).value
    assert vol == pytest.approx(abs(math.log(102 / 101)) * 10_000)            # one change inside the grid
    flat = fc.MidpointSeries([quote(1, t_d - 40 * S), quote(2, t_d - S)])
    assert fc.f07_midpoint_realized_vol_30s_bps(flat, 5, t_d, 0).value == 0.0


def test_f06_f07_missing_rules():
    t_d = 100 * S
    stale = fc.MidpointSeries([quote(1, t_d - 40 * S)])
    assert fc.f06_midpoint_return_30s_bps(stale, 5, t_d, 0).missing == fc.MISSING_STALE_QUOTE
    pre_open = fc.MidpointSeries([quote(1, t_d - 40 * S, second=32390), quote(2, t_d - S)])
    assert fc.f06_midpoint_return_30s_bps(pre_open, 5, t_d, 0).missing == fc.MISSING_PRE_OPEN_REFERENCE
    invalid_latest = fc.MidpointSeries([quote(1, t_d - 40 * S), quote(2, t_d - 20 * S, eligible=False), quote(3, t_d - S)])
    assert fc.f07_midpoint_realized_vol_30s_bps(invalid_latest, 5, t_d, 0).missing == fc.MISSING_INVALID_QUOTE
    assert fc.f06_midpoint_return_30s_bps(fc.MidpointSeries([quote(1, t_d - S)]), 5, t_d, 0).missing == fc.MISSING_NO_QUOTE
    assert fc.f07_midpoint_realized_vol_30s_bps(stale, 5, t_d, t_d - 10 * S).missing == fc.MISSING_TRUNCATED_WINDOW


def test_factor_value_is_either_finite_or_missing():
    with pytest.raises(ValueError):
        fc.FactorValue(None, None)
    with pytest.raises(ValueError):
        fc.FactorValue(float("inf"))
