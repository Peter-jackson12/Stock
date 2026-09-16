"""Tiny, offline counterexample for legacy alignment and receive-order replay."""
from dataclasses import replace
import sqlite3

import pytest

from engine.nxt_tick_engine import NextradeTickEngine
from engine.tick_ordering import OrderedTick, ReceiveOrderReplay, TickOrderError


def tick(seq, kind="quote", *, ns=0, code="005930", venue="unknown", ask=101, bid=99):
    return OrderedTick("fixture", "session-1", seq, ns, code, venue, kind,
                       price=100 if kind == "trade" else None,
                       ask=ask if kind == "quote" else None,
                       bid=bid if kind == "quote" else None)


def replay(max_age=100):
    return ReceiveOrderReplay(source="fixture", session_id="session-1", max_quote_age_ns=max_age)


def test_existing_engine_reproduces_later_quote_in_same_second(tmp_path):
    """A -> trade -> B arrives within one second; legacy preprocessing picks B.

    This test records an existing defect, not an accepted execution policy. It
    should be revised when the legacy reader is replaced by a causal adapter.
    """
    path = tmp_path / "synthetic.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE raw_trades(t_time,code,price,vol,is_buy)")
        conn.execute("CREATE TABLE raw_quotes(q_time,code,offer_p,offer_v,bid_p,bid_v)")
        conn.execute("INSERT INTO raw_quotes VALUES (?,?,?,?,?,?)", ("090001", "005930", "101", "10", "99", "10"))
        conn.execute("INSERT INTO raw_trades VALUES (?,?,?,?,?)", ("090001", "005930", 100, 1, 0))
        conn.execute("INSERT INTO raw_quotes VALUES (?,?,?,?,?,?)", ("090001", "005930", "111", "10", "109", "10"))
    engine = NextradeTickEngine.__new__(NextradeTickEngine)
    engine.code, engine.db_path = "005930", path
    events = engine.load_and_preprocess()
    assert events[0]["ask_p1"] == 111  # not A=101; separate rowids lost cross-stream order


def test_common_sequence_keeps_same_time_trade_on_previous_quote():
    a, trade, b = tick(1), tick(2, "trade"), tick(3, ask=111, bid=109)
    views = list(replay().consume([a, trade, b]))
    assert len(views) == 3  # quote-only events are not discarded
    assert views[1].quote == a
    assert views[1].quote.ask == 101
    assert views[2].quote == b
    assert views[1].quote == a  # later update cannot mutate a prior snapshot


def test_reader_does_not_look_ahead_or_read_future_before_trade_is_yielded():
    consumed = []
    def source():
        for event in [tick(1), tick(2, "trade"), tick(3, ask=500)]:
            consumed.append(event.seq)
            yield event
    views = replay().consume(source())
    next(views)
    current = next(views)
    assert consumed == [1, 2]
    assert current.quote.ask == 101


def test_future_suffix_changes_do_not_change_past_views():
    prefix = [tick(1), tick(2, "trade")]
    first = list(replay().consume(prefix + [tick(3, ask=500)]))
    second = list(replay().consume(prefix + [tick(3, ask=50)]))
    assert first[:2] == second[:2]


def test_chunk_boundaries_do_not_reset_quote_state():
    events = [tick(1), tick(2, "trade"), tick(3, ask=110), tick(4, "trade")]
    expected = list(replay().consume(events))
    chunked = replay()
    actual = list(chunked.consume(events[:2])) + list(chunked.consume(events[2:]))
    assert actual == expected


def test_missing_quote_is_not_fabricated_from_trade_price():
    view = replay().accept(tick(1, "trade"))
    assert view.event.price == 100
    assert view.quote is None and view.quote_age_ns is None
    assert view.quote_status == "missing"


def test_quotes_are_scoped_by_instrument_and_venue():
    state = replay()
    state.accept(tick(1))
    assert state.accept(tick(2, "trade", code="000660")).quote is None
    assert state.accept(tick(3, "trade", venue="another_feed")).quote is None


def test_quote_age_boundary_is_explicit():
    state = replay(max_age=10)
    state.accept(tick(1, ns=100))
    assert state.accept(tick(2, "trade", ns=110)).quote_status == "observed"
    stale = state.accept(tick(3, "trade", ns=111))
    assert stale.quote_status == "stale" and stale.quote_age_ns == 11
    assert stale.quote.ask == 101  # retained for diagnostics, not fabricated or filled


@pytest.mark.parametrize("invalid", [
    tick(1, ns=10), tick(0, ns=10), tick(True, ns=10), tick(2, ns=-1),
    tick(2, ns=9), replace(tick(2), received_ns=0.5),
    replace(tick(2, ns=10), session_id="restarted"),
    replace(tick(2, ns=10), source="other_source"), replace(tick(2, ns=10), kind="bad"),
])
def test_bad_sequence_or_source_is_rejected_without_mutating_state(invalid):
    state = replay()
    state.accept(tick(1, ns=10))
    with pytest.raises(TickOrderError):
        state.accept(invalid)
    assert state.accept(tick(2, "trade", ns=10)).quote.seq == 1


def test_equal_quote_values_are_distinct_events_not_duplicates():
    views = list(replay().consume([tick(1), tick(2)]))
    assert [view.event.seq for view in views] == [1, 2]


def test_sequence_gaps_can_represent_filtered_symbols_but_not_reverse_order():
    state = replay()
    state.accept(tick(1))
    assert state.accept(tick(100, "trade")).quote.seq == 1
    with pytest.raises(TickOrderError):
        state.accept(tick(99))
