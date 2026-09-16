from dataclasses import replace
from decimal import Decimal

import pytest

from engine.tick_ordering import OrderedTick
from engine.tick_session import replay_chunk
from execution.tick_simulator import TickSimulator


def sim(**kwargs):
    defaults = dict(source="test", session_id="s", code="005930", venue="unknown",
                    cash=10000, max_quote_age_ns=100, buy_latency_ns=5,
                    sell_latency_ns=5, cancel_latency_ns=2)
    return TickSimulator(**(defaults | kwargs))


def quote(seq=1, ns=0, ask_size=3, **kwargs):
    e = OrderedTick("test", "s", seq, ns, "005930", "unknown", "quote",
                    bid=99, ask=101, bid_size=3, ask_size=ask_size)
    return replace(e, **kwargs)


def test_both_sides_wait_and_timers_work_without_trades():
    s = sim()
    s.on_event(quote())
    s.submit("buy", "buy", 2)
    s.advance(4)
    assert not s.fills
    s.advance(5)
    assert s.position == 2
    s.submit("sell", "sell", 2)
    s.advance(9)
    assert s.position == 2
    s.advance(10)
    assert s.position == 0 and s.cash == 9996
    assert [f.time_ns for f in s.fills] == [5, 10]


def test_partial_fifo_no_refill_on_trade_or_timer_new_quote_replenishes():
    s = sim()
    s.on_event(quote())
    s.submit("a", "buy", 2)
    s.submit("b", "buy", 3)
    s.advance(5)
    assert [f.quantity for f in s.fills] == [2, 1]
    s.on_event(quote(2, 6, kind="trade", price=101))
    s.advance(7)
    assert len(s.fills) == 2
    s.on_event(quote(3, 8))
    assert [f.quantity for f in s.fills] == [2, 1, 2]
    assert s.orders["b"].status == "filled"


def test_cancellation_latency_allows_fills_before_ack():
    s = sim(cancel_latency_ns=10)
    s.on_event(quote(ask_size=1))
    s.submit("a", "buy", 3)
    s.cancel("a")
    s.advance(5)
    assert s.position == 1
    s.advance(10)
    assert s.orders["a"].status == "cancelled"
    s.on_event(quote(2, 11))
    assert s.position == 1


def test_cancel_wins_same_deadline_and_duplicate_request_is_noop():
    s = sim(cancel_latency_ns=5)
    s.on_event(quote())
    s.submit("a", "buy", 1)
    assert s.cancel("a") and not s.cancel("a")
    s.advance(5)
    assert not s.fills


def test_quote_at_deadline_is_not_used_early():
    s = sim()
    s.on_event(quote())
    s.submit("a", "buy", 1)
    s.on_event(quote(2, 5, ask=200))
    assert s.fills[0].price == 101


@pytest.mark.parametrize("event", [quote(ask=0), quote(ask_size=None)])
def test_invalid_book_never_fills(event):
    s = sim()
    s.on_event(event)
    s.submit("a", "buy", 1)
    s.advance(5)
    assert not s.fills


def test_stale_or_missing_quote_does_not_fill():
    s = sim(max_quote_age_ns=4)
    s.on_event(quote())
    s.submit("a", "buy", 1)
    s.advance(5)
    assert not s.fills
    s.on_event(quote(2, 6))
    assert s.fills[0].time_ns == 6


def test_cash_fees_and_no_short_sales():
    s = sim(cash=205, fee_rate="0.01")
    s.on_event(quote())
    s.submit("a", "buy", 3)
    s.advance(5)
    assert s.position == 2 and s.cash == Decimal("0.98")
    s.cancel("a")
    s.advance(7)
    s.submit("b", "sell", 10)
    s.advance(12)
    assert s.position == 0 and s.orders["b"].remaining == 8
    assert s.cash == Decimal("197.00")


def test_close_expires_at_boundary_without_forced_liquidation():
    s = sim()
    s.on_event(quote())
    s.submit("a", "buy", 1)
    s.close(5)
    assert not s.fills and s.orders["a"].status == "expired"
    with pytest.raises(ValueError):
        s.submit("b", "buy", 1)


def test_close_keeps_partial_holding_and_expires_remainder():
    s = sim()
    s.on_event(quote(ask_size=1))
    s.submit("a", "buy", 3)
    s.close(6)
    assert s.position == 1 and s.orders["a"].remaining == 2
    assert s.orders["a"].status == "expired"


def test_chunk_and_future_suffix_invariance_with_strategy():
    def strategy(view, s):
        if view.event.seq == 1:
            s.submit("a", "buy", 1)
    prefix = [quote(), quote(2, 5)]
    a, b = sim(), sim()
    replay_chunk(a, prefix, strategy)
    for event in prefix:
        replay_chunk(b, [event], strategy)
    assert a.fills == b.fills and a.audit == b.audit
    previous = list(a.fills)
    replay_chunk(a, [quote(3, 6, ask=999)], strategy)
    replay_chunk(b, [quote(3, 6, ask=102)], strategy)
    assert a.fills == b.fills == previous


def test_rejected_event_cannot_execute_due_orders():
    s = sim()
    s.on_event(quote())
    s.submit("a", "buy", 1)
    with pytest.raises(ValueError):
        s.on_event(quote(2, 5, session_id="wrong"))
    assert s.now == 0 and not s.fills


def test_zero_latency_happens_after_trigger_and_duplicate_id_rejected():
    s = sim(buy_latency_ns=0)
    s.on_event(quote())
    s.submit("a", "buy", 1)
    assert len(s.fills) == 1
    with pytest.raises(ValueError):
        s.submit("a", "buy", 1)


def test_cannot_retroactively_close_at_already_processed_timestamp():
    s = sim()
    s.on_event(quote())
    s.submit("a", "buy", 1)
    s.advance(5)
    with pytest.raises(ValueError, match="strictly after"):
        s.close(5)
    assert s.fills[0].time_ns == 5 and not s.closed
