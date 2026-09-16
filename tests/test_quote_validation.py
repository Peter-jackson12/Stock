from dataclasses import replace
from decimal import Decimal

import pytest

from engine.tick_ordering import OrderedTick, ReceiveOrderReplay
from engine.raw_v1_reader import LegacyEvent, LegacyView
from execution.quote_validation import check_ordered_quote, check_legacy_quote


def ordered(**changes):
    q = OrderedTick("fixture", "session", 1, 0, "005930", "unknown", "quote",
                    bid=99, ask=101, bid_size=10, ask_size=20)
    state = ReceiveOrderReplay(source="fixture", session_id="session", max_quote_age_ns=10)
    return state.accept(replace(q, **changes))


def test_valid_quote_retains_finite_prices_and_integer_sizes():
    check = check_ordered_quote(ordered())
    assert check.reason == "eligible"
    assert check.book.ask == Decimal("101")
    assert check.book.ask_size == 20


@pytest.mark.parametrize("field,value", [
    ("bid", 0), ("ask", -101), ("ask", float("nan")), ("bid", float("inf")),
    ("ask", True), ("bid", None), ("ask_size", 0), ("bid_size", -1),
    ("ask_size", 1.5), ("bid_size", False), ("ask_size", None),
])
def test_invalid_fields_cannot_be_used_for_fills(field, value):
    check = check_ordered_quote(ordered(**{field: value}))
    assert check.book is None
    assert check.reason == "invalid_" + field


@pytest.mark.parametrize("bid", [101, 102])
def test_locked_or_crossed_book_is_withheld(bid):
    assert check_ordered_quote(ordered(bid=bid)).reason == "locked_or_crossed"


def test_missing_and_stale_quotes_never_use_trade_price():
    state = ReceiveOrderReplay(source="fixture", session_id="session", max_quote_age_ns=10)
    q = ordered().quote
    missing = state.accept(replace(q, kind="trade", price=100))
    assert check_ordered_quote(missing).reason == "missing"
    state.accept(replace(q, seq=2))
    stale = state.accept(replace(q, seq=3, received_ns=11, kind="trade", price=100))
    assert check_ordered_quote(stale).reason == "stale"


def legacy(ask="101,102", bid="99,98", ask_size="20,30", bid_size="10,15"):
    q = LegacyEvent("fixture.db", "raw_quotes", 1, 32400, "005930",
                    ("090000", "005930", ask, ask_size, bid, bid_size))
    t = LegacyEvent("fixture.db", "raw_trades", 1, 32401, "005930", ("090001", "005930", 100, 1, 0))
    return LegacyView(t, q, 1, "observed")


def test_legacy_adapter_preserves_original_ladder_and_uncertainty():
    view = legacy()
    check = check_legacy_quote(view)
    assert check.reason == "eligible" and check.book.ask_size == 20
    assert view.quote.fields[2] == "101,102"
    assert view.event.order_quality == "second_only"


@pytest.mark.parametrize("value", ["", "NaN,102", "-101,102", b"101,102", "oops,102"])
def test_legacy_bad_price_is_not_coerced(value):
    assert check_legacy_quote(legacy(ask=value)).reason == "invalid_ask"


def test_legacy_absent_stale_and_fractional_size_are_rejected():
    assert check_legacy_quote(replace(legacy(), quote=None)).reason == "missing"
    assert check_legacy_quote(replace(legacy(), quote_status="stale")).reason == "stale"
    assert check_legacy_quote(legacy(ask_size="1.5,10")).reason == "invalid_ask_size"
