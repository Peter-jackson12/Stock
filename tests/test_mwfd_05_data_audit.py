"""MWFD-05 Stage 0 data audit — 합성 계약 회귀 (MWFD-04 산출물 없이)."""
from __future__ import annotations

import pytest

from research.mwfd05 import data_audit as audit

LIMITS = dict(buy_latency_ns=1_000_000_000, sell_latency_ns=1_000_000_000, max_quote_age_ns=2_000_000_000,
              gate_threshold_krw=100_000_000, session_start_second=32400)


def cell(**overrides):
    row = dict(execution_status="EVALUATED", screening_status="COMPLETE_FLAT", ending_position=0,
               ending_open_order=False, completed_trades=0, entry_signals=0, fills=0,
               fast_net_result=0.0, fast_gross_result=0.0, ending_equity=1_000_000.0, ending_cash=1_000_000.0,
               ending_equity_status="FLAT_CASH", fee=0.0, max_adverse_pct=None)
    row.update(overrides)
    return row


def open_cell(**overrides):
    values = dict(screening_status="OPEN_POSITION_UNRANKED", ending_position=1, fast_net_result=None,
                  fast_gross_result=None, ending_equity=None, ending_equity_status="OPEN_POSITION_UNMARKED",
                  entry_signals=1, fills=1)
    values.update(overrides)
    return cell(**values)


def pre(**overrides):
    value = {name: 1 for name in audit.PRE_ENTRY_FIELDS}
    value.update(seq=10, market_second=32500, quote_age_ns=500, gate_status="PASS",
                 gate_reason="ask10_notional_gte_threshold", ask_depth_notional_10=150_000_000)
    value.update(overrides)
    return value


def trade(status="COMPLETED", index=0, **overrides):
    decision = 10_000_000_000
    fill = {"time_ns": decision + 1_000_000_000, "quote_seq": 11, "price": 100.0, "fee": 0.1, "quantity": 1, "side": "buy"}
    value = dict(status=status, trade_index=index, entry_decision_ns=decision, decision_row_resolution="UNIQUE_TRADE_ROW",
                 entry_fill=None, exit_fill=None, exit_decision_ns=None, net_pnl=None, gross_pnl=None, pre_entry=pre())
    if status in ("COMPLETED", "OPEN_POSITION"):
        value["entry_fill"] = fill
    if status == "COMPLETED":
        value.update(exit_decision_ns=decision + 2_000_000_000, net_pnl=-1.2, gross_pnl=-1.0,
                     exit_fill=dict(fill, time_ns=decision + 3_000_000_000, side="sell"))
    value.update(overrides)
    return value


@pytest.mark.parametrize("row,state", [
    (cell(), audit.NO_TRADE_FLAT),
    (cell(ending_open_order=True, entry_signals=1), audit.NO_TRADE_OPEN_ORDER),
    (cell(completed_trades=2, entry_signals=2, fills=4, fast_net_result=-3.0), audit.TRADED_FLAT),
    (cell(completed_trades=1, entry_signals=2, fills=2, fast_net_result=-3.0, ending_open_order=True), audit.TRADED_FLAT_OPEN_ORDER),
    (open_cell(), audit.OPEN_POSITION),
    (open_cell(completed_trades=3), audit.OPEN_POSITION),
    (cell(execution_status="FAILED"), audit.ERROR_INCOMPLETE),
])
def test_candidate_cell_states_are_mutually_exclusive(row, state):
    assert audit.classify_candidate_cell(row) == state


def test_open_position_is_censored_not_zero():
    assert audit.candidate_cell_contract_issues(open_cell()) == []
    assert "net_null_contract" in audit.candidate_cell_contract_issues(open_cell(fast_net_result=0.0))
    assert "equity_status_contract" in audit.candidate_cell_contract_issues(open_cell(ending_equity_status="FLAT_CASH"))


def test_no_trade_contract_and_nonfinite_detection():
    assert audit.candidate_cell_contract_issues(cell()) == []
    assert "no_trade_nonzero" in audit.candidate_cell_contract_issues(cell(fast_net_result=5.0))
    assert "no_trade_flat_with_signal" in audit.candidate_cell_contract_issues(cell(entry_signals=1))
    assert "nonfinite:fee" in audit.candidate_cell_contract_issues(cell(fee=float("nan")))
    assert audit.candidate_cell_contract_issues(cell(execution_status="X")) == ["error_incomplete"]


def test_trade_ledger_contract_passes_for_valid_rows():
    for status in audit.TRADE_STATUSES:
        assert audit.trade_contract_issues(trade(status), **LIMITS) == [], status


@pytest.mark.parametrize("overrides,issue", [
    ({"pre_entry": None}, "pre_entry_missing"),
    ({"pre_entry": pre(gate_status="UNKNOWN")}, "decision_gate_not_pass"),
    ({"pre_entry": pre(ask_depth_notional_10=99_999_999)}, "decision_depth_below_gate"),
    ({"pre_entry": pre(quote_age_ns=2_000_000_001)}, "decision_quote_not_fresh"),
    ({"pre_entry": pre(market_second=32399)}, "decision_before_session_start"),
    ({"pre_entry": dict(pre(), exit_fill=None)}, "pre_entry_field_set"),
    ({"decision_row_resolution": "FIRST_STATIC_ELIGIBLE_SAME_NS"}, "non_unique_decision_row"),
    ({"entry_decision_ns": 10_500_000_000}, "entry_fill_before_latency"),
    ({"exit_decision_ns": 10_900_000_000}, "exit_decision_before_entry_fill"),
    ({"net_pnl": None}, "completed_missing_fill_or_pnl"),
    ({"status": "SOMETHING"}, "unknown_status"),
])
def test_trade_ledger_contract_violations(overrides, issue):
    row = trade("COMPLETED")
    row.update(overrides)
    assert issue in audit.trade_contract_issues(row, **LIMITS)


def test_reconcile_matches_candidate_cell():
    acc = audit.KeyAccumulator()
    for index in range(2):
        acc.add(trade("COMPLETED", index=index))
    acc.add(trade("OPEN_POSITION", index=2))
    expected = dict(entry_signals=3, completed_trades=2, ending_position=1, ending_open_order=False, fast_net_result=None)
    assert audit.reconcile_key(acc, expected) == []
    flat = audit.KeyAccumulator()
    flat.add(trade("COMPLETED"))
    assert audit.reconcile_key(flat, dict(entry_signals=1, completed_trades=1, ending_position=0,
                                          ending_open_order=False, fast_net_result=-1.2)) == []
    assert "completed_net_ne_fast_net" in audit.reconcile_key(
        flat, dict(entry_signals=1, completed_trades=1, ending_position=0, ending_open_order=False, fast_net_result=-5.0))


def test_reconcile_flags_gaps_counts_and_nonterminal_open_rows():
    acc = audit.KeyAccumulator()
    acc.add(trade("OPEN_POSITION", index=0))
    acc.add(trade("COMPLETED", index=2))
    issues = audit.reconcile_key(acc, dict(entry_signals=3, completed_trades=0, ending_position=0,
                                           ending_open_order=False, fast_net_result=None))
    assert {"rows_ne_entry_signals", "completed_ne_candidate_cell", "trade_index_gap",
            "terminal_episode_not_last", "open_position_mismatch"} <= set(issues)
    unfilled = audit.KeyAccumulator()
    unfilled.add(trade("ENTRY_UNFILLED"))
    assert "unfilled_without_open_order" in audit.reconcile_key(
        unfilled, dict(entry_signals=1, completed_trades=0, ending_position=0, ending_open_order=False, fast_net_result=0.0))
