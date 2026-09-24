"""Pure exact regressions for portfolio performance-accounting contracts."""
from decimal import Decimal
from fractions import Fraction

import pytest

from execution.portfolio_accounting import (
    COST_BASIS_METHOD,
    MARKING_POLICY,
    account_portfolio_fills,
    value_portfolio_at_bid_marks,
)
from execution.portfolio_simulator import PortfolioFill


def fill(
    *,
    order_id,
    time_ns,
    side,
    quantity,
    price,
    fee,
    quote_seq,
    code="005930",
):
    return PortfolioFill(
        order_id,
        code,
        "unknown",
        time_ns,
        side,
        quantity,
        Decimal(str(price)),
        Decimal(str(fee)),
        quote_seq,
    )


def test_actual_smoke_round_trip_is_exact_flat_realized_accounting():
    fills = [
        fill(
            order_id="nxt:005930:nxt-fixed-1",
            time_ns=8033223285400,
            side="buy",
            quantity=1,
            price="270500",
            fee="270.500",
            quote_seq=2322490,
        ),
        fill(
            order_id="nxt:005930:nxt-fixed-2",
            time_ns=8753412620500,
            side="sell",
            quantity=1,
            price="269000",
            fee="269.000",
            quote_seq=3906354,
        ),
    ]

    ledger = account_portfolio_fills(fills)

    assert ledger.cost_basis_method == COST_BASIS_METHOD
    assert ledger.fill_count == 2
    assert ledger.realized_pnl == Fraction(-4079, 2)
    assert ledger.cash_delta == Fraction(-4079, 2)
    position = ledger.position("005930")
    assert position.quantity == 0
    assert position.cost_basis == 0
    assert position.average_cost is None

    valuation = value_portfolio_at_bid_marks(
        ledger,
        initial_cash=Decimal("1000000"),
        current_cash=Decimal("997960.500"),
        bid_marks={},
    )

    assert valuation.marking_policy == MARKING_POLICY
    assert valuation.hypothetical_exit_fee_included is False
    assert valuation.realized_pnl == Fraction(-4079, 2)
    assert valuation.unrealized_pnl == 0
    assert valuation.total_pnl == Fraction(-4079, 2)
    assert valuation.equity == Fraction(1995921, 2)
    assert valuation.expected_cash_from_fills == Fraction(1995921, 2)
    assert valuation.cash_reconciled is True
    assert valuation.accounting_identity_reconciled is True
    assert valuation.unpriced_codes == ()
    assert dict(valuation.marks) == {}


def test_weighted_average_partial_sell_remains_exact_rational():
    fills = [
        fill(order_id="b1", time_ns=1, side="buy", quantity=1, price="100", fee="0", quote_seq=1, code="A"),
        fill(order_id="b2", time_ns=2, side="buy", quantity=2, price="101", fee="0", quote_seq=2, code="A"),
        fill(order_id="s1", time_ns=3, side="sell", quantity=1, price="103", fee="0", quote_seq=3, code="A"),
    ]

    ledger = account_portfolio_fills(fills)
    position = ledger.position("A")

    assert position.quantity == 2
    assert position.cost_basis == Fraction(604, 3)
    assert position.average_cost == Fraction(302, 3)
    assert ledger.realized_pnl == Fraction(7, 3)
    assert ledger.cash_delta == -199

    valuation = value_portfolio_at_bid_marks(
        ledger,
        initial_cash=1000,
        current_cash=801,
        bid_marks={"A": 102},
    )

    assert valuation.unrealized_pnl == Fraction(8, 3)
    assert valuation.total_pnl == 5
    assert valuation.equity == 1005
    assert valuation.cash_reconciled is True
    assert valuation.accounting_identity_reconciled is True


def test_buy_fee_is_included_in_cost_basis_and_sell_fee_reduces_realized_pnl():
    fills = [
        fill(order_id="b", time_ns=1, side="buy", quantity=1, price="100", fee="1", quote_seq=1, code="A"),
        fill(order_id="s", time_ns=2, side="sell", quantity=1, price="110", fee="1.1", quote_seq=2, code="A"),
    ]

    ledger = account_portfolio_fills(fills)

    assert ledger.realized_pnl == Fraction(79, 10)
    assert ledger.cash_delta == Fraction(79, 10)
    assert ledger.position("A").quantity == 0


def test_open_position_bid_mark_is_gross_of_hypothetical_exit_fee():
    ledger = account_portfolio_fills([
        fill(order_id="b", time_ns=1, side="buy", quantity=2, price="100", fee="2", quote_seq=1, code="A"),
    ])

    position = ledger.position("A")
    assert position.cost_basis == 202
    assert position.average_cost == 101

    valuation = value_portfolio_at_bid_marks(
        ledger,
        initial_cash=1000,
        current_cash=798,
        bid_marks={"A": 105},
    )

    assert valuation.hypothetical_exit_fee_included is False
    assert valuation.realized_pnl == 0
    assert valuation.unrealized_pnl == 8
    assert valuation.equity == 1008
    assert valuation.total_pnl == 8
    assert valuation.accounting_identity_reconciled is True


def test_missing_open_position_mark_keeps_valuation_unavailable():
    ledger = account_portfolio_fills([
        fill(order_id="b", time_ns=1, side="buy", quantity=1, price="100", fee="0", quote_seq=1, code="A"),
    ])

    valuation = value_portfolio_at_bid_marks(
        ledger,
        initial_cash=1000,
        current_cash=900,
        bid_marks={},
    )

    assert valuation.realized_pnl == 0
    assert valuation.unrealized_pnl is None
    assert valuation.total_pnl is None
    assert valuation.equity is None
    assert valuation.unpriced_codes == ("A",)
    assert valuation.cash_reconciled is True
    assert valuation.accounting_identity_reconciled is None


def test_explicit_none_mark_is_unpriced_not_zero():
    ledger = account_portfolio_fills([
        fill(order_id="b", time_ns=1, side="buy", quantity=1, price="100", fee="0", quote_seq=1, code="A"),
    ])

    valuation = value_portfolio_at_bid_marks(
        ledger,
        initial_cash=1000,
        current_cash=900,
        bid_marks={"A": None},
    )

    assert valuation.unpriced_codes == ("A",)
    assert valuation.equity is None


@pytest.mark.parametrize("mark", [0, -1, "0", "-0.01"])
def test_nonpositive_explicit_bid_mark_is_rejected(mark):
    ledger = account_portfolio_fills([
        fill(order_id="b", time_ns=1, side="buy", quantity=1, price="100", fee="0", quote_seq=1, code="A"),
    ])

    with pytest.raises(ValueError, match="positive bid mark"):
        value_portfolio_at_bid_marks(
            ledger,
            initial_cash=1000,
            current_cash=900,
            bid_marks={"A": mark},
        )


def test_cash_ledger_mismatch_is_reported_without_repair():
    ledger = account_portfolio_fills([
        fill(order_id="b", time_ns=1, side="buy", quantity=1, price="100", fee="0", quote_seq=1, code="A"),
    ])

    valuation = value_portfolio_at_bid_marks(
        ledger,
        initial_cash=1000,
        current_cash=899,
        bid_marks={"A": 100},
    )

    assert valuation.expected_cash_from_fills == 900
    assert valuation.current_cash == 899
    assert valuation.cash_reconciled is False
    assert valuation.accounting_identity_reconciled is False


def test_long_only_accounting_rejects_oversell():
    with pytest.raises(ValueError, match="sell more than held"):
        account_portfolio_fills([
            fill(order_id="s", time_ns=1, side="sell", quantity=1, price="100", fee="0", quote_seq=1, code="A"),
        ])


def test_fill_time_must_be_nondecreasing():
    with pytest.raises(ValueError, match="nondecreasing"):
        account_portfolio_fills([
            fill(order_id="b1", time_ns=2, side="buy", quantity=1, price="100", fee="0", quote_seq=1, code="A"),
            fill(order_id="b2", time_ns=1, side="buy", quantity=1, price="100", fee="0", quote_seq=2, code="A"),
        ])


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"quantity": 0}, "quantity"),
        ({"price": "0"}, "price"),
        ({"fee": "-1"}, "fee"),
        ({"quote_seq": 0}, "quote_seq"),
    ],
)
def test_invalid_fill_numeric_contract_fails_closed(changes, message):
    kwargs = dict(
        order_id="x",
        time_ns=1,
        side="buy",
        quantity=1,
        price="100",
        fee="0",
        quote_seq=1,
        code="A",
    )
    kwargs.update(changes)

    with pytest.raises(ValueError, match=message):
        account_portfolio_fills([fill(**kwargs)])



def test_same_code_cannot_mix_venues():
    first = fill(
        order_id="b1",
        time_ns=1,
        side="buy",
        quantity=1,
        price="100",
        fee="0",
        quote_seq=1,
        code="A",
    )
    second = PortfolioFill(
        "b2",
        "A",
        "other",
        2,
        "buy",
        1,
        Decimal("100"),
        Decimal("0"),
        2,
    )

    with pytest.raises(ValueError, match="one venue per code"):
        account_portfolio_fills([first, second])
