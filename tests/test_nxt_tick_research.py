from dataclasses import replace

import pytest

from engine.tick_ordering import OrderedTick
from engine.tick_session import replay_chunk
from execution.tick_simulator import TickSimulator
from strategies.nxt_breakout.tick_research import NxtResearchStrategy


def sim():
    return TickSimulator(source="test", session_id="s", code="005930", venue="unknown",
                         cash=1_000_000, max_quote_age_ns=100, buy_latency_ns=5,
                         sell_latency_ns=5, cancel_latency_ns=2)


def quote(seq=1, ns=0, second=32399, **changes):
    q = OrderedTick("test", "s", seq, ns, "005930", "unknown", "quote",
                    bid=10000, ask=10001, bid_size=10, ask_size=3,
                    market_second=second, bid_sizes=(10, 10, 10), ask_sizes=(3, 3, 3))
    return replace(q, **changes)


def trade(seq=2, ns=1, second=32400, **changes):
    return replace(quote(seq, ns, second), **(dict(kind="trade", price=10001, volume=30, is_buy=True) | changes))


def test_fixed_rule_end_to_end_signal_delayed_buy_and_sell():
    s, strategy = sim(), NxtResearchStrategy(quantity=2)
    replay_chunk(s, [quote(), trade()], strategy)
    assert len(strategy.signals) == 1 and not s.fills
    s.advance(6)
    assert s.position == 2
    replay_chunk(s, [quote(3, 7, 32401, bid=10100, ask=10101)], strategy)
    assert [x[1] for x in strategy.signals] == ["buy", "sell"]
    assert s.position == 2
    s.advance(12)
    assert s.position == 0
    assert [f.price for f in s.fills] == [10001, 10100]


@pytest.mark.parametrize("rule", ["fixed", "tick_trail", "step_trail"])
def test_all_exit_variants_submit_delayed_stop(rule):
    s, strategy = sim(), NxtResearchStrategy(quantity=1, exit_rule=rule)
    replay_chunk(s, [quote(), trade(), quote(3, 7, 32401, bid=9900, ask=9901)], strategy)
    assert strategy.signals[-1][1:] == ("sell", 1, rule)
    assert len(s.fills) == 1
    s.advance(12)
    assert len(s.fills) == 2


def test_future_regular_open_cannot_trigger_preopen_entry():
    s, strategy = sim(), NxtResearchStrategy(quantity=1)
    replay_chunk(s, [quote(second=30000), trade(second=30000)], strategy)
    assert strategy.open is None and not strategy.signals


def test_missing_top_three_does_not_fabricate_obi():
    s, strategy = sim(), NxtResearchStrategy(quantity=1)
    replay_chunk(s, [quote(bid_sizes=None), trade()], strategy)
    assert not strategy.signals


def test_overheat_is_accumulated_only_from_observed_premarket():
    s, strategy = sim(), NxtResearchStrategy(quantity=1)
    replay_chunk(s, [quote(second=28800), trade(second=28800),
                     trade(3, 2, 31000, price=11000, volume=50000),
                     trade(4, 3, 32400, price=11000)], strategy)
    assert strategy.pre_volume == 50030 and not strategy.signals


def test_chunked_strategy_replay_matches_single_pass():
    events = [quote(), trade(), quote(3, 7, 32401, bid=10100, ask=10101)]
    a, b = sim(), sim()
    sa, sb = NxtResearchStrategy(quantity=2), NxtResearchStrategy(quantity=2)
    replay_chunk(a, events, sa)
    for event in events:
        replay_chunk(b, [event], sb)
    a.close(20)
    b.close(20)
    assert a.fills == b.fills and a.audit == b.audit and sa.signals == sb.signals


def test_required_wall_clock_is_not_guessed_from_monotonic_time():
    s, strategy = sim(), NxtResearchStrategy(quantity=1)
    with pytest.raises(ValueError, match="market_second"):
        replay_chunk(s, [quote(market_second=None)], strategy)


@pytest.mark.parametrize("rule", ["tick_trail", "step_trail"])
def test_trailing_exits_after_observed_peak(rule):
    s, strategy = sim(), NxtResearchStrategy(quantity=1, exit_rule=rule)
    replay_chunk(s, [quote(), trade(), quote(3, 7, 32401, bid=10100, ask=10101),
                     trade(4, 8, 32401, price=10100)], strategy)
    assert len(strategy.signals) == 1
    replay_chunk(s, [quote(5, 9, 32401, bid=10070, ask=10071)], strategy)
    assert strategy.signals[-1][1] == "sell"


def test_partial_entry_cancelled_before_exit_quantity_is_submitted():
    s, strategy = sim(), NxtResearchStrategy(quantity=5)
    replay_chunk(s, [quote(), trade(), quote(3, 7, 32401, bid=9900, ask=9901,
                                            ask_size=1, ask_sizes=(1, 1, 1),
                                            bid_size=3, bid_sizes=(3, 3, 3))], strategy)
    assert s.position == 4 and len(strategy.signals) == 1
    assert strategy.exit_requested
    replay_chunk(s, [trade(4, 9, 32401, price=9900)], strategy)
    assert s.orders["nxt-fixed-1"].status == "cancelled"
    assert strategy.signals[-1][1:3] == ("sell", 4)
    s.advance(14)
    assert s.position == 1  # only three shares of displayed bid liquidity
    replay_chunk(s, [quote(5, 15, 32402, bid=9900, ask=9901)], strategy)
    assert s.position == 0
