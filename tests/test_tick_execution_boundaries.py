"""현재 실행 순서의 합성 경계 회귀. 외부 엔진의 순서를 이식하지 않는다."""
from dataclasses import replace
from decimal import Decimal

import pytest

from engine.tick_ordering import OrderedTick
from engine.tick_session import replay_chunk
from execution.reality_contract import simulation_contract
from execution.tick_simulator import TickSimulator


def simulator(**changes):
    config = dict(source="test", session_id="s", code="005930", venue="unknown",
                  cash=10000, max_quote_age_ns=100, buy_latency_ns=5,
                  sell_latency_ns=5, cancel_latency_ns=5, fee_rate="0")
    return TickSimulator(**(config | changes))


def quote(seq=1, ns=0, **changes):
    event = OrderedTick("test", "s", seq, ns, "005930", "unknown", "quote",
                        bid=99, ask=101, bid_size=3, ask_size=1)
    return replace(event, **changes)


@pytest.mark.parametrize("latency", [0, 5])
def test_resting_or_pending_remainder_gets_new_quote_before_strategy(latency):
    sim = simulator(buy_latency_ns=latency)
    sim.on_event(quote())
    sim.submit("old", "buy", 2)
    seen = []
    def strategy(view, account):
        seen.append([(fill.order_id, fill.quote_seq) for fill in account.fills])
        account.submit("new", "buy", 1)
    replay_chunk(sim, [quote(2, 5, ask=111)], strategy)
    assert seen == [[("old", 1), ("old", 2)]]
    sim.advance(10)
    assert [(fill.order_id, fill.price) for fill in sim.fills] == [
        ("old", Decimal(101)), ("old", Decimal(111))]
    assert sim.orders["new"].remaining == 1
    assert sim.position == 2


@pytest.mark.parametrize("max_age,expected_price,expected_seq", [(5, 101, 1), (4, 111, 2)])
def test_ready_deadline_and_external_event_tie_respects_old_quote_age(max_age, expected_price, expected_seq):
    sim = simulator(max_quote_age_ns=max_age)
    sim.on_event(quote())
    sim.submit("buy", "buy", 1)
    sim.on_event(quote(2, 5, ask=111))
    assert [(fill.time_ns, fill.price, fill.quote_seq) for fill in sim.fills] == [
        (5, Decimal(expected_price), expected_seq)]
    assert simulation_contract(sim)["same_timestamp_policy"]["id"] == "timers_old_quote_then_event_then_strategy_v1"


def test_effective_cancel_wins_ready_deadline_and_quote_refill_tie():
    sim = simulator()
    sim.on_event(quote())
    sim.submit("buy", "buy", 2)
    sim.cancel("buy")
    sim.on_event(quote(2, 5, ask_size=10))
    assert sim.fills == [] and sim.orders["buy"].status == "cancelled"
    assert sim.audit[-1] == (5, "buy", "cancelled")


def test_cancel_requested_in_market_callback_cannot_undo_prior_fill():
    sim = simulator(cancel_latency_ns=0)
    sim.on_event(quote())
    sim.submit("buy", "buy", 1)
    acknowledgements = []
    replay_chunk(sim, [quote(2, 5, ask=111)],
                 lambda view, account: acknowledgements.append(account.cancel("buy")))
    assert acknowledgements == [False]
    assert [(fill.time_ns, fill.quote_seq) for fill in sim.fills] == [(5, 1)]
    assert sim.orders["buy"].status == "filled"
    assert not any(action == "cancel_requested" for _, _, action in sim.audit)


def test_zero_latency_uses_trigger_quote_not_previous_or_later_same_timestamp_quote():
    sim = simulator(buy_latency_ns=0)
    def strategy(view, account):
        if view.event.seq == 2:
            assert not account.fills
            account.submit("triggered", "buy", 1)
    replay_chunk(sim, [quote(), quote(2, 5, ask=111), quote(3, 5, ask=121)], strategy)
    assert [(fill.time_ns, fill.quote_seq, fill.price) for fill in sim.fills] == [(5, 2, Decimal(111))]
    assert sim.audit == [(5, "triggered", "submitted"), (5, "triggered", "filled")]


def test_multiple_same_time_orders_use_submission_order_not_id_sort_or_exchange_queue():
    sim = simulator()
    sim.on_event(quote(ask_size=2))
    for order_id in ("z", "a", "m"):
        sim.submit(order_id, "buy", 1)
    sim.on_event(quote(2, 5, ask=111))
    assert [fill.order_id for fill in sim.fills] == ["z", "a", "m"]
    assert [fill.time_ns for fill in sim.fills] == [5, 5, 5]
    assert [fill.quote_seq for fill in sim.fills] == [1, 1, 2]
    assert simulation_contract(sim)["queue_position_model"] == "none"


def test_identical_new_quote_sequence_refills_but_trade_and_timer_do_not():
    sim = simulator(buy_latency_ns=0)
    sim.on_event(quote())
    sim.submit("buy", "buy", 5)
    sim.on_event(quote(2, 0, kind="trade", price=101, volume=999, is_buy=True))
    sim.advance(0)
    assert len(sim.fills) == 1
    sim.on_event(quote(3, 0))
    assert [(fill.quote_seq, fill.quantity) for fill in sim.fills] == [(1, 1), (3, 1)]
    assert sim.orders["buy"].remaining == 3


@pytest.mark.parametrize("age,filled", [(5, True), (6, False)])
def test_stale_threshold_is_inclusive_even_without_a_new_market_event(age, filled):
    sim = simulator(buy_latency_ns=0, max_quote_age_ns=5)
    sim.on_event(quote())
    sim.advance(age)
    sim.submit("buy", "buy", 1)
    assert bool(sim.fills) is filled
    if filled:
        assert sim.fills[0].time_ns == 5
    else:
        assert sim.position == 0 and sim.orders["buy"].remaining == 1


@pytest.mark.parametrize("latency,expected_fills", [(9, 1), (10, 0), (11, 0)])
def test_last_tick_inflight_order_fills_only_before_exclusive_close(latency, expected_fills):
    sim = simulator(buy_latency_ns=latency)
    sim.on_event(quote())
    sim.submit("buy", "buy", 2)
    sim.close(10)
    assert len(sim.fills) == expected_fills
    assert all(fill.time_ns < 10 for fill in sim.fills)
    assert sim.position == expected_fills
    assert sim.orders["buy"].remaining == 2 - expected_fills
    assert sim.orders["buy"].status == "expired"
    assert sim.now == 10 and sim.closed


def test_post_last_tick_processing_does_not_bypass_stale_quote_policy():
    sim = simulator(buy_latency_ns=8, max_quote_age_ns=7)
    sim.on_event(quote())
    sim.submit("buy", "buy", 1)
    sim.close(10)
    assert sim.fills == [] and sim.orders["buy"].status == "expired"


@pytest.mark.parametrize("cancel_latency,status,filled", [(9, "cancelled", 0), (10, "expired", 1)])
def test_close_boundary_does_not_process_a_cancel_at_close(cancel_latency, status, filled):
    sim = simulator(buy_latency_ns=9, cancel_latency_ns=cancel_latency)
    sim.on_event(quote())
    sim.submit("buy", "buy", 2)
    sim.cancel("buy")
    sim.close(10)
    assert sim.orders["buy"].status == status
    assert sim.position == filled and len(sim.fills) == filled
    assert sim.audit[-1] == ((9 if status == "cancelled" else 10), "buy", status)


def test_no_strategy_callback_is_fabricated_for_trailing_fill_or_close():
    sim = simulator()
    calls = []
    def strategy(view, account):
        calls.append(view.event.seq)
        if view.event.seq == 2:
            account.submit("last-tick", "buy", 1)
    replay_chunk(sim, [quote(), quote(2, 1)], strategy)
    assert not sim.fills
    sim.close(20)
    assert calls == [1, 2]
    assert [(fill.time_ns, fill.quote_seq) for fill in sim.fills] == [(6, 2)]
    assert sim.position == 1


def test_splitting_same_timestamp_events_into_chunks_preserves_fills_and_audit():
    stream = [quote(), quote(2, 5, ask=111), quote(3, 5, ask=121)]
    def strategy(view, account):
        if view.event.seq == 1:
            account.submit("order", "buy", 3)
    whole, chunked = simulator(), simulator()
    replay_chunk(whole, stream, strategy)
    for event in stream:
        replay_chunk(chunked, [event], strategy)
    whole.close(10)
    chunked.close(10)
    assert whole.fills == chunked.fills
    assert whole.audit == chunked.audit
    assert whole.cash == chunked.cash and whole.position == chunked.position == 3
