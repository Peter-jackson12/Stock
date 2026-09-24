"""NXT portfolio result integration regressions for pure performance accounting."""
from dataclasses import replace
from fractions import Fraction
import json

import pytest

from engine.nxt_portfolio_research import NxtPortfolioRunFailed, run_nxt_portfolio
from engine.tick_ordering import OrderedTick


def quote(seq=1, ns=0, second=32399, **changes):
    event = OrderedTick(
        "test",
        "s",
        seq,
        ns,
        "A",
        "unknown",
        "quote",
        bid=10000,
        ask=10001,
        bid_size=10,
        ask_size=3,
        market_second=second,
        bid_sizes=(10, 10, 10),
        ask_sizes=(3, 3, 3),
    )
    return replace(event, **changes)


def trade(seq=2, ns=1, second=32400, **changes):
    return replace(
        quote(seq, ns, second),
        **(dict(kind="trade", price=10001, volume=30, is_buy=True) | changes),
    )


def config(cash=1_000_000, fee_rate="0.001"):
    return dict(
        source="test",
        session_id="s",
        instruments={"A": "unknown"},
        cash=cash,
        fee_rate=fee_rate,
        max_quote_age_ns=100,
        buy_latency_ns=5,
        sell_latency_ns=5,
        cancel_latency_ns=2,
    )


def fraction(record):
    if record is None:
        return None
    return Fraction(int(record["numerator"]), int(record["denominator"]))


def test_open_position_result_has_accounting_but_no_fabricated_mark_valuation(tmp_path):
    path = run_nxt_portfolio(
        [quote(), trade()],
        output_root=tmp_path,
        dataset_label="open-accounting",
        simulator_config=config(),
        close_ns=20,
        quantity=2,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    accounting = saved["performance_accounting"]

    assert saved["status"] == "completed_with_open_position"
    assert accounting["schema"] == "portfolio_performance_accounting_v1"
    assert accounting["status"] == "open_unpriced"
    assert accounting["mark_integration"] == "not_connected"
    assert accounting["legacy_top_level_pnl_fields_populated"] is False
    assert accounting["hypothetical_exit_fee_included"] is False
    assert accounting["fill_count"] == 1
    assert fraction(accounting["realized_pnl"]) == 0
    assert accounting["unrealized_pnl"] is None
    assert accounting["total_pnl"] is None
    assert accounting["equity"] is None
    assert accounting["unpriced_codes"] == ["A"]
    assert accounting["bid_marks"] == {}
    assert accounting["cash_reconciled"] is True
    assert accounting["position_reconciled"] is True
    assert accounting["accounting_identity_reconciled"] is None

    assert saved["realized_pnl"] is None
    assert saved["unrealized_pnl"] is None
    assert saved["equity"] is None
    assert "execution/portfolio_accounting.py" in saved["code_sha256"]
    assert "no_pnl_valuation_no_forced_liquidation" not in saved["limitations"]
    assert "performance_accounting_subrecord_only_legacy_top_level_pnl_fields_unpopulated" in saved["limitations"]
    assert "open_position_final_bid_mark_integration_not_connected" in saved["limitations"]
    assert "no_forced_liquidation" in saved["limitations"]


def test_flat_result_has_exact_realized_accounting_and_equity(tmp_path):
    path = run_nxt_portfolio(
        [
            quote(),
            trade(),
            quote(3, 7, 32401, bid=10100, ask=10101),
        ],
        output_root=tmp_path,
        dataset_label="flat-accounting",
        simulator_config=config(),
        close_ns=20,
        quantity=1,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    accounting = saved["performance_accounting"]

    assert saved["status"] == "completed_flat"
    assert accounting["status"] == "flat_complete"
    assert accounting["unpriced_codes"] == []
    assert accounting["bid_marks"] == {}
    assert accounting["cash_reconciled"] is True
    assert accounting["position_reconciled"] is True
    assert accounting["accounting_identity_reconciled"] is True

    initial = fraction(accounting["initial_cash"])
    current = fraction(accounting["current_cash"])
    expected = fraction(accounting["expected_cash_from_fills"])
    realized = fraction(accounting["realized_pnl"])
    unrealized = fraction(accounting["unrealized_pnl"])
    total = fraction(accounting["total_pnl"])
    equity = fraction(accounting["equity"])

    assert current == expected
    assert unrealized == 0
    assert total == realized
    assert equity == current
    assert realized == current - initial

    assert saved["realized_pnl"] is None
    assert saved["unrealized_pnl"] is None
    assert saved["equity"] is None


def test_empty_result_is_flat_complete_with_initial_cash_equity(tmp_path):
    path = run_nxt_portfolio(
        [],
        output_root=tmp_path,
        dataset_label="empty-accounting",
        simulator_config=config(cash=123456),
        close_ns=20,
        quantity=1,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    accounting = saved["performance_accounting"]

    assert saved["status"] == "completed_empty_input"
    assert accounting["status"] == "flat_complete"
    assert accounting["fill_count"] == 0
    assert fraction(accounting["realized_pnl"]) == 0
    assert fraction(accounting["unrealized_pnl"]) == 0
    assert fraction(accounting["total_pnl"]) == 0
    assert fraction(accounting["equity"]) == 123456
    assert fraction(accounting["initial_cash"]) == 123456
    assert fraction(accounting["current_cash"]) == 123456
    assert accounting["cash_reconciled"] is True
    assert accounting["position_reconciled"] is True
    assert accounting["accounting_identity_reconciled"] is True


def test_failure_diagnostics_include_partial_accounting_without_hiding_failure(tmp_path):
    stream = [
        quote(),
        trade(),
        replace(trade(3, 2), session_id="wrong"),
    ]

    with pytest.raises(NxtPortfolioRunFailed) as caught:
        run_nxt_portfolio(
            stream,
            output_root=tmp_path,
            dataset_label="failed-accounting",
            simulator_config=config(),
            close_ns=20,
            quantity=1,
        )

    saved = json.loads(caught.value.path.read_text(encoding="utf-8"))
    accounting = saved["performance_accounting"]

    assert saved["status"] == "failed"
    assert saved["diagnostics_only"] is True
    assert saved["input_complete"] is False
    assert accounting["schema"] == "portfolio_performance_accounting_v1"
    assert accounting["fill_count"] == 0
    assert accounting["cash_reconciled"] is True
    assert accounting["position_reconciled"] is True
    assert saved["realized_pnl"] is None
    assert saved["unrealized_pnl"] is None
    assert saved["equity"] is None


def test_accounting_subrecord_participates_in_reproducibility_identity(tmp_path):
    kwargs = dict(
        events=[quote(), trade()],
        output_root=tmp_path,
        dataset_label="accounting-repro",
        simulator_config=config(),
        close_ns=20,
        quantity=1,
    )

    first = json.loads(run_nxt_portfolio(**kwargs).read_text(encoding="utf-8"))
    second = json.loads(run_nxt_portfolio(**kwargs).read_text(encoding="utf-8"))

    assert first["performance_accounting"] == second["performance_accounting"]
    assert first["reproducibility_key"] == second["reproducibility_key"]
    assert first["code_sha256"]["execution/portfolio_accounting.py"] == second["code_sha256"]["execution/portfolio_accounting.py"]
