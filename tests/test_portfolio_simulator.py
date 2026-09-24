"""공유 자본/예약/주문 의도 경계의 합성 회귀. 실제 raw/OCX 접근 없음."""
from dataclasses import FrozenInstanceError, asdict, replace
from decimal import Decimal, Inexact, Rounded, localcontext
from itertools import chain

import pytest

from engine.tick_ordering import OrderedTick
from engine.portfolio_session import PortfolioRunFailed, replay_portfolio_chunk, run_portfolio
from execution.portfolio_simulator import (
    CancelIntent, OrderIntent, PortfolioSimulator, RiskLimits, TERMINAL,
)
from execution.tick_simulator import TickSimulator


def config(**changes):
    base = dict(source="test", session_id="s", instruments={"B": "unknown", "A": "unknown"},
                cash=10000, fee_rate="0", max_quote_age_ns=100,
                buy_latency_ns=5, sell_latency_ns=5, cancel_latency_ns=2)
    return base | changes


def sim(**changes):
    return PortfolioSimulator(**config(**changes))


def quote(seq=1, ns=0, code="A", **changes):
    return replace(OrderedTick("test", "s", seq, ns, code, "unknown", "quote",
                               bid=99, ask=100, bid_size=3, ask_size=3), **changes)


def seed(s):
    s.on_event(quote())
    s.on_event(quote(2, code="B"))


def buy(s, name="a", code="A", quantity=1):
    return s.submit(OrderIntent(name, code, "buy", quantity))


def order(s, name):
    return next(o for o in s.orders if o.order_id == name)


def assert_conserved(s):
    snap = s.snapshot()
    assert snap.cash >= 0 and snap.reserved_cash >= 0 and snap.available_cash >= 0
    assert snap.cash == snap.available_cash + snap.reserved_cash
    positions = dict(snap.positions)
    assert all(n >= 0 for n in positions.values())
    for o in s.orders:
        filled = sum(f.quantity for f in s.fills if f.order_id == o.order_id)
        assert filled + o.remaining == o.quantity
        if o.status in TERMINAL:
            assert o.reserved_cash == 0 and o.reserved_quantity == 0
    for code, quantity in positions.items():
        assert sum(o.reserved_quantity for o in s.orders if o.code == code) <= quantity


def test_two_symbols_reserve_one_cash_pool_before_either_is_ready():
    s = sim(cash=150)
    seed(s)
    buy(s, "z", "B")
    buy(s, "a", "A")
    assert order(s, "a").reason == "admission:insufficient_cash"
    assert s.snapshot().available_cash == 50
    s.advance(5)
    assert [(f.code, f.quantity) for f in s.fills] == [("B", 1)]
    assert dict(s.snapshot().positions) == {"A": 0, "B": 1}
    assert_conserved(s)


def test_first_fill_makes_later_symbol_order_insufficient_cash():
    s = sim(cash=150, buy_latency_ns=0)
    seed(s)
    buy(s, "first")
    buy(s, "second", "B")
    assert order(s, "first").status == "filled"
    assert order(s, "second").status == "rejected"
    assert order(s, "second").reason == "admission:insufficient_cash"
    assert s.snapshot().cash == 50


def test_partial_fill_cancel_preserves_remainder_and_delayed_reservation_release():
    s = sim(cash=500)
    s.on_event(quote(ask_size=1))
    buy(s, quantity=3)
    s.advance(5)
    assert (order(s, "a").remaining, order(s, "a").status) == (2, "partially_filled")
    assert (s.snapshot().cash, s.snapshot().reserved_cash, s.snapshot().available_cash) == (400, 200, 200)
    assert s.cancel("a") and not s.cancel("a")
    assert order(s, "a").status == "cancel_pending"
    s.advance(6)
    assert s.snapshot().reserved_cash == 200
    s.advance(7)
    assert order(s, "a").status == "cancelled" and order(s, "a").remaining == 2
    assert s.snapshot().reserved_cash == 0
    assert dict(s.snapshot().positions)["A"] == 1
    assert_conserved(s)


def test_cancel_pending_can_fill_before_ack_and_terminal_never_fills_again():
    s = sim(cancel_latency_ns=10)
    s.on_event(quote(ask_size=1))
    buy(s, quantity=2)
    s.cancel("a")
    s.advance(5)
    assert order(s, "a").status == "cancel_pending" and order(s, "a").remaining == 1
    s.on_event(quote(2, 6))
    assert order(s, "a").status == "filled"
    assert not s.cancel("a")
    s.on_event(quote(3, 11))
    assert sum(f.quantity for f in s.fills) == 2
    assert [t.status for t in s.transitions][:3] == ["created", "pending", "cancel_pending"]


def test_cancel_ack_globally_precedes_fill_and_releases_competing_cash():
    s = sim(cash=150, cancel_latency_ns=5)
    s.on_event(quote(code="B", bid=49, ask=50))
    s.on_event(quote(2, code="A"))
    buy(s, "b", "B")  # First priority must still see later A's cancellation.
    buy(s, "a", "A")
    s.cancel("a")
    s.on_event(quote(3, 1, "B", bid=99, ask=100))
    s.advance(5)
    assert order(s, "a").status == "cancelled"
    assert order(s, "b").status == "filled"
    assert s.fills[0].price == 100
    assert s.snapshot().cash == 50


def test_common_clock_processes_old_quotes_before_same_timestamp_new_quote():
    s = sim()
    seed(s)
    buy(s, "b", "B")
    buy(s, "a", "A")
    s.on_event(quote(3, 5, "B", ask=900))
    assert [(f.code, f.price, f.quote_seq) for f in s.fills] == [
        ("B", Decimal(100), 2), ("A", Decimal(100), 1)]
    assert [o.submission_seq for o in s.orders] == [1, 2]


def test_same_timestamp_input_common_seq_not_symbol_sorting():
    s = sim(cash=150, buy_latency_ns=0)
    def strategy(view, snapshot):
        return [OrderIntent(str(view.event.seq), view.event.code, "buy", 1)]
    replay_portfolio_chunk(s, [quote(code="B"), quote(2, code="A")], strategy)
    assert [(f.code, f.time_ns) for f in s.fills] == [("B", 0)]
    assert order(s, "2").status == "rejected"


def test_same_symbol_liquidity_budget_is_shared_when_duplicate_guard_disabled():
    s = sim(risk=RiskLimits(guard_symbol_orders=False))
    s.on_event(quote(ask_size=3))
    buy(s, "z", quantity=2)
    buy(s, "a", quantity=3)
    s.advance(5)
    assert [(f.order_id, f.quantity) for f in s.fills] == [("z", 2), ("a", 1)]
    s.on_event(quote(2, 6, kind="trade", price=100))
    s.advance(7)
    assert len(s.fills) == 2
    s.on_event(quote(3, 8))
    assert [(f.order_id, f.quantity) for f in s.fills] == [("z", 2), ("a", 1), ("a", 2)]
    assert_conserved(s)


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_duplicate_and_conflicting_symbol_order_guard(side):
    s = sim()
    seed(s)
    buy(s)
    s.submit(OrderIntent("other", "A", side, 1))
    assert order(s, "other").reason == "admission:duplicate_or_conflicting_order"
    assert not s.cancel("other")
    with pytest.raises(ValueError, match="unique"):
        buy(s, "other", "B")


def test_sell_reservations_prevent_double_sale_and_other_symbol_is_independent():
    s = sim(buy_latency_ns=0, risk=RiskLimits(guard_symbol_orders=False))
    seed(s)
    buy(s, quantity=2)
    s.submit(OrderIntent("sell-one", "A", "sell", 2))
    s.submit(OrderIntent("sell-two", "A", "sell", 1))
    s.submit(OrderIntent("naked", "B", "sell", 1))
    assert order(s, "sell-two").reason == "admission:insufficient_holdings"
    assert order(s, "naked").reason == "admission:insufficient_holdings"
    s.advance(5)
    assert dict(s.snapshot().positions) == {"A": 0, "B": 0}
    assert order(s, "sell-one").status == "filled"
    assert_conserved(s)


def test_position_cap_counts_unfilled_buy_reservations():
    s = sim(risk=RiskLimits(max_position_per_symbol={"A": 3}, guard_symbol_orders=False))
    s.on_event(quote())
    buy(s, "one", quantity=2)
    buy(s, "two", quantity=2)
    assert order(s, "two").reason == "admission:max_position_per_symbol"
    s.advance(5)
    buy(s, "three", quantity=2)
    assert order(s, "three").reason == "admission:max_position_per_symbol"


def test_gross_exposure_cap_includes_holdings_and_open_buy_quantities():
    s = sim(risk=RiskLimits(max_gross_exposure="300", guard_symbol_orders=False))
    seed(s)
    buy(s, "a", "A", 2)
    buy(s, "b", "B", 1)
    buy(s, "c", "B", 1)
    assert order(s, "c").reason == "admission:max_gross_exposure"
    assert s.snapshot().gross_exposure_at_ask == 300
    s.advance(5)
    assert s.snapshot().gross_exposure_at_ask == 300
    assert_conserved(s)


def test_stale_other_position_blocks_buy_exposure_check_but_does_not_block_sell():
    s = sim(max_quote_age_ns=2, buy_latency_ns=0, sell_latency_ns=0,
            risk=RiskLimits(max_gross_exposure=1000))
    seed(s)
    buy(s, "a", "A")
    s.on_event(quote(3, 3, "B"))
    assert s.snapshot().gross_exposure_at_ask is None
    assert s.snapshot().exposure_status == "unpriced"
    buy(s, "b", "B")
    assert order(s, "b").reason == "admission:unpriced_exposure"
    # Fresh A can be sold; B's stale quote is irrelevant to a risk-reducing sell.
    s.on_event(quote(4, 6, "A"))
    s.submit(OrderIntent("exit", "A", "sell", 1))
    assert order(s, "exit").status == "filled"
    assert s.snapshot().gross_exposure_at_ask == 0


def test_max_open_order_limit_counts_cancel_pending_not_rejected_or_cancelled():
    s = sim(cancel_latency_ns=0, risk=RiskLimits(max_open_orders=1))
    seed(s)
    buy(s)
    buy(s, "blocked", "B")
    assert order(s, "blocked").reason == "admission:max_open_orders"
    s.cancel("a")
    buy(s, "allowed", "B")
    assert order(s, "allowed").status == "pending"


def test_quote_repricing_can_reject_only_remainder_and_release_reserve():
    s = sim(cash=250)
    s.on_event(quote(ask_size=1))
    buy(s, quantity=2)
    s.advance(5)
    assert s.snapshot().cash == 150 and order(s, "a").remaining == 1
    s.on_event(quote(2, 6, bid=199, ask=200))
    assert order(s, "a").reason == "matching:insufficient_cash"
    assert order(s, "a").remaining == 1
    assert len(s.fills) == 1 and dict(s.snapshot().positions)["A"] == 1
    assert s.snapshot().available_cash == 150
    assert_conserved(s)


def test_quote_repricing_rechecks_exposure_limit_at_execution():
    s = sim(risk=RiskLimits(max_gross_exposure=150))
    s.on_event(quote())
    buy(s)
    s.on_event(quote(2, 1, ask=200))
    s.advance(5)
    assert not s.fills
    assert order(s, "a").reason == "matching:max_gross_exposure"


@pytest.mark.parametrize("change,reason", [
    ({"ask": 0}, "quote_invalid_ask"), ({"ask_size": None}, "quote_invalid_ask_size"),
    ({"bid": 101}, "quote_locked_or_crossed"),
])
def test_invalid_quote_at_admission_is_explicit_business_reject(change, reason):
    s = sim()
    s.on_event(quote(**change))
    buy(s)
    assert order(s, "a").reason == "admission:" + reason
    assert not s.fills and s.snapshot().reserved_cash == 0


def test_missing_and_stale_quotes_reject_admission_without_fallback():
    s = sim(max_quote_age_ns=1)
    buy(s, "missing")
    assert order(s, "missing").reason == "admission:quote_missing"
    s.on_event(quote())
    s.advance(2)
    buy(s, "stale")
    assert order(s, "stale").reason == "admission:quote_stale"


def test_stale_quote_after_admission_withholds_fill_then_recovers():
    s = sim(max_quote_age_ns=4)
    s.on_event(quote())
    buy(s)
    s.advance(5)
    assert order(s, "a").status == "active" and not s.fills
    assert s.snapshot().reserved_cash == 100
    s.on_event(quote(2, 6, ask=102))
    assert s.fills[0].time_ns == 6 and s.fills[0].price == 102
    assert_conserved(s)


@pytest.mark.parametrize("bad", [
    quote(3, 5, session_id="other"), quote(2, 5), quote(3, 5, code="C"),
    quote(3, 5, venue="other"), quote(3, 5, ask="1e65"), quote(3, -1),
])
def test_invalid_event_cannot_fire_any_symbols_due_timers(bad):
    s = sim()
    seed(s)
    buy(s, "a", "A")
    buy(s, "b", "B")
    before = s.snapshot(), s.transitions
    with pytest.raises(ValueError):
        s.on_event(bad)
    assert (s.snapshot(), s.transitions) == before
    s.on_event(quote(3, 5))
    assert len(s.fills) == 2


def test_close_is_exclusive_expires_orders_releases_all_reservations_keeps_holdings():
    s = sim(buy_latency_ns=0, sell_latency_ns=5)
    seed(s)
    buy(s)
    s.submit(OrderIntent("exit", "A", "sell", 1))
    s.close(5)
    assert order(s, "exit").status == "expired"
    assert dict(s.snapshot().positions)["A"] == 1
    assert s.snapshot().reserved_cash == 0
    assert order(s, "exit").reserved_quantity == 0
    assert len(s.fills) == 1 and s.fills[0].time_ns < 5
    for fn in (lambda: s.advance(6), lambda: buy(s, "x"), lambda: s.cancel("exit"),
               lambda: s.on_event(quote(3, 6)), lambda: s.close(6)):
        with pytest.raises(ValueError):
            fn()
    assert_conserved(s)


def test_exact_fees_reservations_and_cash_ignore_ambient_decimal_rounding():
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = ctx.traps[Rounded] = True
        s = sim(cash="205", fee_rate="0.01", buy_latency_ns=0, sell_latency_ns=0)
        s.on_event(quote(ask=101))
        buy(s, quantity=2)
        assert s.snapshot().cash == Decimal("0.98")
        assert s.fills[0].fee == Decimal("2.02")
        s.submit(OrderIntent("exit", "A", "sell", 2))
        assert s.snapshot().cash == Decimal("197.00")
        assert not ctx.flags[Inexact] and not ctx.flags[Rounded]


def test_strategy_receives_detached_immutable_state_not_mutable_simulator():
    s = sim()
    seed(s)
    snap = s.snapshot()
    with pytest.raises(FrozenInstanceError):
        snap.cash = Decimal(1)
    buy(s)
    assert not snap.orders and snap.reserved_cash == 0
    with pytest.raises(FrozenInstanceError):
        s.orders[0].remaining = 0
    altered = s.settings()
    altered["instruments"]["C"] = "unknown"
    assert "C" not in s.settings()["instruments"]
    assert_conserved(s)


@pytest.mark.parametrize("bad", [
    {"cash": "NaN"}, {"cash": -1}, {"fee_rate": True}, {"fee_rate": 1},
    {"buy_latency_ns": True}, {"instruments": {}}, {"instruments": {"A": ""}},
    {"risk": RiskLimits(max_position_per_symbol={"C": 1})},
])
def test_bad_configuration_fails_before_events(bad):
    with pytest.raises(ValueError):
        sim(**bad)


@pytest.mark.parametrize("bad", [
    {"max_open_orders": True}, {"max_open_orders": -1}, {"guard_symbol_orders": 1},
    {"max_position_per_symbol": {"A": True}}, {"max_gross_exposure": "Infinity"},
])
def test_bad_risk_limits_are_not_silently_coerced(bad):
    with pytest.raises(ValueError):
        RiskLimits(**bad)


def test_limits_copy_caller_mapping_and_have_no_investment_defaults():
    limits = {"A": 1}
    risk = RiskLimits(max_position_per_symbol=limits)
    limits["A"] = 999
    assert risk.max_position_per_symbol["A"] == 1
    assert RiskLimits().max_gross_exposure is None and RiskLimits().max_open_orders is None


def synthetic_strategy(view, snapshot):
    seq = view.event.seq
    if seq == 2:
        return [OrderIntent("a", "A", "buy", 2), OrderIntent("b", "B", "buy", 2)]
    if seq == 3:
        return [CancelIntent("a")]
    return []


def test_chunking_repetition_and_future_suffix_preserve_prefix_results():
    prefix = [quote(ask_size=1), quote(2, code="B"), quote(3, 5, "B")]
    a, b = sim(), sim()
    records_a = replay_portfolio_chunk(a, prefix, synthetic_strategy)
    records_b = list(chain.from_iterable(replay_portfolio_chunk(b, [e], synthetic_strategy) for e in prefix))
    assert records_a == records_b and a.snapshot() == b.snapshot() and a.transitions == b.transitions
    prefix_fills, prefix_orders = a.fills, a.orders
    replay_portfolio_chunk(a, [quote(4, 9, ask=1000)], synthetic_strategy)
    replay_portfolio_chunk(b, [quote(4, 9, ask=101)], synthetic_strategy)
    assert a.fills[:len(prefix_fills)] == b.fills[:len(prefix_fills)] == prefix_fills
    assert prefix_orders[0].remaining == 1  # Historical frozen snapshots remain historical.


def test_pure_driver_returns_reproducible_separate_result_schema_no_fabricated_pnl():
    events = [quote(ask_size=1), quote(2, code="B"), quote(3, 5, "B")]
    args = dict(simulator_config=config(), close_ns=10, strategy=synthetic_strategy, strategy_id="fixture-v1")
    a, b = run_portfolio(events, **args), run_portfolio(iter(events), **args)
    assert a == b
    assert a["status"] == "completed_with_open_position" and a["input_complete"]
    assert a["realized_pnl"] is a["unrealized_pnl"] is a["equity"] is None
    assert a["account"]["positions"] == [["A", 1], ["B", 2]]
    assert len(a["code_sha256"]) == 5 and len(a["reproducibility_key"]) == 64
    assert len(a["order_intents"]) == 3 and not a["raw_identity_verified"]


def test_driver_failure_preserves_earlier_fills_and_attempted_intent_diagnostics():
    def bad_strategy(view, snapshot):
        return [OrderIntent("a", "A", "buy", 1), CancelIntent("nonexistent")]
    with pytest.raises(PortfolioRunFailed) as caught:
        run_portfolio([quote()], simulator_config=config(buy_latency_ns=0), close_ns=10,
                      strategy=bad_strategy, strategy_id="bad-fixture")
    report = caught.value.report
    assert report["status"] == "failed" and report["diagnostics_only"] and not report["input_complete"]
    assert len(report["account"]["fills"]) == 1
    assert len(report["order_intents"]) == 2
    assert not report["account"]["closed"]


@pytest.mark.parametrize("intents", [None, {}, set(), (i for i in []), ["not-an-intent"]])
def test_driver_rejects_unordered_or_malformed_intent_collection(intents):
    with pytest.raises(PortfolioRunFailed, match="ordered list/tuple"):
        run_portfolio([quote()], simulator_config=config(), close_ns=10,
                      strategy=lambda view, snapshot: intents, strategy_id="bad-ordering")


def test_driver_exclusive_close_input_violation_is_not_success():
    with pytest.raises(PortfolioRunFailed) as caught:
        run_portfolio([quote(ns=10)], simulator_config=config(), close_ns=10,
                      strategy=lambda view, snapshot: [], strategy_id="empty")
    assert caught.value.report["event_count"] == 1
    assert caught.value.report["processed_event_count"] == 0
    assert caught.value.report["diagnostics_only"]


def test_sufficiently_funded_single_symbol_economics_match_existing_simulator():
    old_args = config()
    old_args.pop("instruments")
    old = TickSimulator(**old_args, code="A", venue="unknown")
    new = sim()
    for s in (old, new):
        s.on_event(quote(ask_size=1))
    old.submit("a", "buy", 2)
    buy(new, quantity=2)
    for event in (quote(2, 5), quote(3, 6, kind="trade", price=100)):
        old.on_event(event)
        new.on_event(event)
    old.submit("exit", "sell", 2)
    new.submit(OrderIntent("exit", "A", "sell", 2))
    old.close(15)
    new.close(15)
    assert old.cash == new.snapshot().cash
    assert old.position == dict(new.snapshot().positions)["A"]
    fields = ("order_id", "time_ns", "side", "quantity", "price", "fee", "quote_seq")
    assert [tuple(getattr(f, k) for k in fields) for f in old.fills] == [
        tuple(getattr(f, k) for k in fields) for f in new.fills]


@pytest.mark.parametrize("fee", ["0", "0.003"])
@pytest.mark.parametrize("latency", [0, 2])
def test_mixed_asset_trace_matches_independent_fraction_ledger_and_liquidity(fee, latency):
    from fractions import Fraction
    from collections import defaultdict
    s = sim(cash=1000, fee_rate=fee, buy_latency_ns=latency, sell_latency_ns=latency,
            max_quote_age_ns=3, cancel_latency_ns=1, risk=RiskLimits(guard_symbol_orders=False))
    seen_quotes = {}
    for seq in range(1, 13):
        code = "A" if seq % 2 else "B"
        event = quote(seq, seq, code, bid=99 + seq, ask=100 + seq, ask_size=1 + seq % 2)
        if seq % 3 == 0:
            event = replace(event, kind="trade", price=100 + seq)
        else:
            seen_quotes[seq] = event
        s.on_event(event)
        if seq % 4 == 1:
            buy(s, str(seq), code, 2)
        elif seq % 4 == 2:
            s.submit(OrderIntent(str(seq), "A", "sell", 1))
        else:
            live = [o for o in s.orders if o.status not in TERMINAL]
            if live:
                s.cancel(live[0].order_id)
        cash = Fraction(1000)
        positions, consumed = defaultdict(int), defaultdict(int)
        for fill in s.fills:
            gross = Fraction(fill.price) * fill.quantity
            charge = gross * Fraction(fee)
            assert Fraction(fill.fee) == charge
            cash += -gross - charge if fill.side == "buy" else gross - charge
            positions[fill.code] += fill.quantity if fill.side == "buy" else -fill.quantity
            q = seen_quotes[fill.quote_seq]
            assert q.code == fill.code and q.received_ns <= fill.time_ns <= q.received_ns + 3
            assert fill.price == (q.ask if fill.side == "buy" else q.bid)
            consumed[(fill.quote_seq, fill.side)] += fill.quantity
            assert consumed[(fill.quote_seq, fill.side)] <= (q.ask_size if fill.side == "buy" else q.bid_size)
        assert Fraction(s.snapshot().cash) == cash
        assert dict(s.snapshot().positions) == {c: positions[c] for c in ("A", "B")}
        assert_conserved(s)
    s.close(15)
    assert_conserved(s)


@pytest.mark.parametrize("invalid", [
    OrderIntent("bad", "A", "buy", 1, reason=object()),
    OrderIntent("bad", "A", "buy", float("nan")),
    OrderIntent(object(), "A", "buy", 1),
])
def test_unserializable_intent_cannot_hide_prior_fills_or_failure_report(invalid):
    def strategy(view, snapshot):
        return [OrderIntent("valid", "A", "buy", 1), invalid]
    with pytest.raises(PortfolioRunFailed) as caught:
        run_portfolio([quote()], simulator_config=config(buy_latency_ns=0), close_ns=10,
                      strategy=strategy, strategy_id="malformed-payload")
    report = caught.value.report
    assert report["diagnostics_only"] and not report["input_complete"]
    assert len(report["account"]["fills"]) == 1
    assert len(report["account"]["orders"]) == 1
    assert len(report["order_intents"]) == 2
    assert report["order_intents"][-1]["kind"] == "invalid_intent"
    assert report["order_intents"][-1]["serialization_error"] in ("TypeError", "ValueError")
