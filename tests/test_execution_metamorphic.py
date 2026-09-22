"""참인 변형 관계와 참이 아닌 advance 분할 반례를 분리한다."""
from dataclasses import replace
from decimal import Context, Decimal, ROUND_HALF_EVEN, ROUND_FLOOR, ROUND_CEILING, localcontext
from fractions import Fraction
from itertools import permutations, product
from time import perf_counter

import pytest

from engine.tick_session import replay_chunk
from execution.tick_simulator import TickSimulator
from execution.reality_contract import simulation_contract
from tests.execution_audit_support import Pair, config, economic, invoke, observable, quote
from tests.execution_oracle import Specification


def execute(cfg, commands):
    pair = Pair(cfg)
    for command in commands:
        pair.apply(*command)
    return pair


def test_id_alpha_renaming_preserves_submission_order_economics():
    for delay in range(3):
        reference = None
        for ids in permutations(("z-last", "a-first", "m-middle")):
            commands = [("event", quote(ask_size=2))]
            commands += [("submit", identity, "buy", 1) for identity in ids]
            commands += [("event", quote(2, 1, ask=4)), ("close", 4)]
            pair = execute(config(buy_latency_ns=delay, max_quote_age_ns=10, cash=20), commands)
            result = economic(pair.sim)
            if reference is None:
                reference = result
            assert result == reference
            # delay=2이면 초기 quote를 소비하기 전에 새 quote로 교체된다.
            # 두 snapshot의 budget을 합산하지 않으므로 그 경우 2주만 체결된다.
            expected_count = 2 if delay == 2 else 3
            assert [f.order_id for f in pair.sim.fills] == list(ids[:expected_count])


def test_future_suffix_cannot_rewrite_committed_prefix_fills():
    for delay, next_ask, kind in product(range(3), (2, 6), ("quote", "trade")):
        pair = execute(config(buy_latency_ns=delay, max_quote_age_ns=10),
                       [("event", quote(ask_size=1)), ("submit", "B", "buy", 2), ("advance", 1)])
        prefix = tuple(pair.sim.fills)
        pair.apply("event", quote(2, 2, kind=kind, bid=1, ask=next_ask, price=999))
        pair.apply("close", 5)
        assert tuple(f for f in pair.sim.fills if f.time_ns <= 1) == prefix


def scripted_orders(sequence):
    return {1: [("submit", "B", "buy", 3)], 3: [("submit", "S", "sell", 1)],
            4: [("cancel", "B")]}.get(sequence, [])


def partitions(events):
    for mask in range(1 << (len(events) - 1)):
        chunks, start = [], 0
        for index in range(1, len(events)):
            if mask & (1 << (index - 1)):
                chunks.append(events[start:index])
                start = index
        yield chunks + [events[start:]]


def test_all_chunk_partitions_preserve_economics_without_extra_clock_calls():
    events = [quote(1, 0, ask_size=1), quote(2, 1, kind="trade", price=99),
              quote(3, 1, bid=3, ask=4), quote(4, 2, kind="trade", price=99), quote(5, 3)]
    for delay in range(3):
        cfg = config(cash=30, buy_latency_ns=delay, sell_latency_ns=delay,
                     cancel_latency_ns=1, max_quote_age_ns=10)
        expected = Pair(cfg)
        for event in events:
            expected.apply("event", event)
            for command in scripted_orders(event.seq):
                expected.apply(*command)
        expected.apply("close", 6)
        for chunks in partitions(events):
            sim = TickSimulator(**cfg)
            def strategy(view, account):
                for command in scripted_orders(view.event.seq):
                    invoke(simulator=account, command=command)
            for chunk in chunks:
                replay_chunk(sim, chunk, strategy)
            sim.close(6)
            assert observable(sim) == expected.oracle.export()


def test_unmodeled_trade_payload_does_not_change_fixed_order_execution():
    expected = None
    for price, volume, direction, depth in product((1, 999), (1, 10000), (False, True), (None, (999, 999, 999))):
        commands = [("event", quote()), ("submit", "B", "buy", 2),
                    ("event", quote(2, 1, kind="trade", price=price, volume=volume,
                                    is_buy=direction, bid_sizes=depth, ask_sizes=depth)), ("close", 4)]
        pair = execute(config(buy_latency_ns=2), commands)
        result = observable(pair.sim)
        if expected is None:
            expected = result
        assert result == expected
    # 이 관계는 고정 주문 simulator에만 해당한다. NXT 전략은 trade 입력을 사용한다.


def test_contract_description_is_not_mutable_execution_state():
    cfg = config(buy_latency_ns=1)
    sims = [TickSimulator(**cfg), TickSimulator(**cfg)]
    changed = simulation_contract(sims[0])
    changed["queue_position_model"] = "descriptive-test-only"
    changed["order_entry_latency_model"]["buy_ns"] = 999
    for sim in sims:
        for command in (("event", quote()), ("submit", "B", "buy", 2), ("close", 3)):
            invoke(sim, command)
    assert observable(sims[0]) == observable(sims[1])


def test_latency_crossing_quote_boundary_changes_fill_price():
    outputs = []
    for delay in (1, 2):
        pair = execute(config(buy_latency_ns=delay, max_quote_age_ns=10),
                       [("event", quote()), ("submit", "B", "buy", 1),
                        ("event", quote(2, 1, bid=4, ask=5)), ("close", 4)])
        outputs.append([(f.time_ns, f.price) for f in pair.sim.fills])
    assert outputs == [[(1, Decimal(3))], [(2, Decimal(5))]]


def test_reducing_max_age_below_ready_age_removes_fill():
    outputs = []
    for age in (2, 1):
        pair = execute(config(buy_latency_ns=2, max_quote_age_ns=age),
                       [("event", quote()), ("submit", "B", "buy", 1), ("close", 3)])
        outputs.append(pair.sim.position)
    assert outputs == [1, 0]


def test_fee_monotonicity_only_for_single_buy_fixed_book_no_replenishment():
    for cash, size, delay in product(("2.99", "3", "3.03", "6", "6.60"), (1, 2), range(3)):
        quantities = []
        for fee in ("0", "0.01", "0.1", "0.2"):
            pair = execute(config(cash=cash, fee_rate=fee, buy_latency_ns=delay, max_quote_age_ns=10),
                           [("event", quote(ask_size=size)), ("submit", "B", "buy", 2), ("close", 4)])
            quantities.append(pair.sim.position)
        assert quantities == sorted(quantities, reverse=True)


def test_only_new_quote_sequence_replenishes_partial_remainders():
    pair = execute(config(cash=30, max_quote_age_ns=10),
                   [("event", quote(ask_size=1)), ("submit", "B", "buy", 3)])
    pair.apply("event", quote(2, 0, kind="trade", price=99, volume=999))
    pair.apply("advance", 0).apply("advance", 1)
    assert pair.sim.position == 1 and pair.sim.orders["B"].remaining == 2
    pair.apply("event", quote(3, 1, ask_size=1))
    assert pair.sim.position == 2 and pair.sim.orders["B"].remaining == 1
    pair.apply("close", 4)
    assert pair.sim.position == 2 and pair.sim.orders["B"].status == "expired"


@pytest.mark.parametrize("changes", [{"ask": 2}, {"ask": 1}, {"ask": 0}, {"bid": None},
                                    {"ask": "NaN"}, {"ask_size": 0}, {"bid_size": -1},
                                    {"ask_size": "1.5"}])
def test_invalid_quote_withholds_fill_instead_of_reusing_previous_valid_quote(changes):
    pair = execute(config(buy_latency_ns=2, max_quote_age_ns=10),
                   [("event", quote()), ("submit", "B", "buy", 2),
                    ("event", quote(2, 1, **changes)), ("advance", 2)])
    assert pair.sim.fills == []
    pair.apply("event", quote(3, 3)).apply("close", 4)
    assert pair.sim.position == 2 and all(f.quote_seq == 3 for f in pair.sim.fills)


def test_waiting_sell_is_not_revisited_inside_later_buy_submission():
    pair = execute(config(cash=6, max_quote_age_ns=10),
                   [("event", quote()), ("submit", "S", "sell", 1), ("submit", "B", "buy", 1)])
    assert pair.sim.cash == 3 and pair.sim.position == 1
    assert pair.sim.orders["S"].remaining == 1
    pair.apply("advance", 0)
    assert pair.sim.cash == 5 and pair.sim.position == 0
    assert [f.order_id for f in pair.sim.fills] == ["B", "S"]


def test_waiting_buy_is_not_revisited_inside_later_sell_submission():
    pair = execute(config(cash=6, max_quote_age_ns=10),
                   [("event", quote()), ("submit", "seed", "buy", 2),
                    ("event", quote(2, 0, bid=4, ask=5)),
                    ("submit", "B", "buy", 1), ("submit", "S", "sell", 2)])
    assert pair.sim.cash == 8 and pair.sim.position == 0
    assert pair.sim.orders["B"].remaining == 1
    pair.apply("advance", 0)
    assert pair.sim.cash == 3 and pair.sim.position == 1
    assert [f.order_id for f in pair.sim.fills] == ["seed", "S", "B"]


def test_advance_subdivision_is_not_a_valid_metamorphic_relation():
    cfg = config(cash=3, buy_latency_ns=1, max_quote_age_ns=1)
    prefix = [("event", quote()), ("submit", "S", "sell", 1), ("submit", "B", "buy", 1)]
    direct = execute(cfg, prefix + [("advance", 2), ("close", 3)])
    split = execute(cfg, prefix + [("advance", 1), ("advance", 2), ("close", 3)])
    assert [(f.side, f.time_ns) for f in direct.sim.fills] == [("buy", 1)]
    assert [(f.side, f.time_ns) for f in split.sim.fills] == [("buy", 1), ("sell", 1)]
    assert (direct.sim.cash, direct.sim.position) == (0, 1)
    assert (split.sim.cash, split.sim.position) == (2, 0)


def low_precision_case():
    cfg = config(cash="1.01", fee_rate="0.005")
    commands = [("event", quote(bid="1", ask="1.01", bid_size=1, ask_size=1)),
                ("submit", "B", "buy", 1)]
    with localcontext(Context(prec=3, rounding=ROUND_HALF_EVEN)):
        sim, ref = TickSimulator(**cfg), Specification(cfg)
        for command in commands:
            ref.apply(command)
            invoke(sim, command)
    return sim, ref


def test_known_low_precision_solvency_counterexample_is_reproduced():
    sim, ref = low_precision_case()
    # 독립 손계산: 1.01 * 1.005 = 1.01505 > 1.01, 따라서 매수 불가.
    # PR #11의 동일 반례를 유지한다. d431308의 strict XPASS 확인 후 정상 회귀로 전환.
    assert ref.state.fills == () and ref.state.cash == Decimal("1.01")
    assert sim.position == 0 and sim.cash == Decimal("1.01")
    assert sim.fills == []


def test_known_low_precision_must_preserve_cash_and_match_exact_oracle():
    sim, ref = low_precision_case()
    assert sim.cash >= 0
    assert observable(sim) == ref.export()


def _fixture_decimal_text(value):
    """이 행렬의 유리수만 12자리 고정 격자로 옮긴다. SUT helper/Decimal 연산 없음."""
    units = value * 10 ** 12
    assert units.denominator == 1 and units >= 0
    whole, fraction = divmod(units.numerator, 10 ** 12)
    return f"{whole}.{fraction:012d}"


def test_numeric_bounded_precision_rounding_and_exact_cost_matrix():
    started, count, checkpoints = perf_counter(), 0, 0
    grid = product((1, 3, 6, 12, 28), (ROUND_HALF_EVEN, ROUND_FLOOR, ROUND_CEILING),
                   (("2", "3"), ("1", "1.01"), ("0.00006", "0.00007")),
                   ("0", "0.005", "0.2"), (1, 2, 3), (-1, 0, 1), (1, 2))
    for prec, rounding, (bid, ask), fee, qty, offset, size in grid:
        exact = qty * Fraction(ask) * (1 + Fraction(fee))
        cash = _fixture_decimal_text(exact + Fraction(offset, 10 ** 12))
        with localcontext(Context(prec=prec, rounding=rounding)):
            pair = Pair(config(cash=cash, fee_rate=fee, max_quote_age_ns=10))
            book = dict(bid=bid, ask=ask, bid_size=size, ask_size=size)
            pair.apply("event", quote(**book)).apply("submit", "B", "buy", qty)
            pair.apply("event", quote(2, 1, **book)).apply("event", quote(3, 2, **book))
            # Buy capacity is tested before selling can add cash; partial fills
            # use three bounded snapshots, and the remainder is then cancelled.
            expected_bought = qty - 1 if offset < 0 else qty
            assert pair.sim.position == expected_bought
            pair.apply("cancel", "B").apply("submit", "S", "sell", qty)
            pair.apply("event", quote(4, 3, **book)).apply("event", quote(5, 4, **book))
            pair.apply("close", 6)
            assert pair.sim.position == 0
            count += 1
            checkpoints += pair.checkpoints
    assert (count, checkpoints) == (2430, 21870)
    print(f"NUMERIC_MATRIX affordability: traces={count} checkpoints={checkpoints} seconds={perf_counter()-started:.3f}")


def test_numeric_fee_monotonicity_across_bounded_contexts():
    count, checkpoints = 0, 0
    for prec, rounding, cash in product((1, 3, 28), (ROUND_HALF_EVEN, ROUND_FLOOR, ROUND_CEILING),
                                        ("1.01", "2.0301", "3.1")):
        quantities = []
        for fee in ("0", "0.005", "0.1", "0.2"):
            with localcontext(Context(prec=prec, rounding=rounding)):
                pair = execute(config(cash=cash, fee_rate=fee),
                               [("event", quote(bid="1", ask="1.01", ask_size=3)),
                                ("submit", "B", "buy", 3), ("close", 2)])
                quantities.append(pair.sim.position)
                count += 1
                checkpoints += pair.checkpoints
        assert quantities == sorted(quantities, reverse=True)
    assert (count, checkpoints) == (108, 324)
    print(f"NUMERIC_MATRIX monotonicity: traces={count} checkpoints={checkpoints}")


def test_numeric_precision_28_is_not_an_unbounded_safety_guarantee():
    price = "1.0000000000000000000000000001"
    with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
        # Historical capacity's rounded unit cost hid this exact over-budget amount.
        assert Decimal(price) * (1 + Decimal(0)) == Decimal(1)
        pair = execute(config(cash="1", fee_rate="0"),
                       [("event", quote(bid="0.9", ask=price)), ("submit", "B", "buy", 1)])
        assert pair.sim.fills == [] and pair.sim.cash == 1


@pytest.mark.parametrize("cash,bid,ask,fee", [
    ("4e64", "1e64", "2e64", "1e-64"),
    ("4e-64", "1e-64", "2e-64", "0"),
    ("9" * 64, "1", "8" * 64, "0"),
])
def test_numeric_envelope_endpoints_ignore_context_traps_and_exponents(cash, bid, ask, fee):
    with localcontext(Context(prec=1, rounding=ROUND_CEILING, Emin=-2, Emax=2, clamp=1)) as context:
        for signal in context.traps:
            context.traps[signal] = True
        context.clear_flags()
        pair = execute(config(cash=cash, fee_rate=fee),
                       [("event", quote(bid=bid, ask=ask)), ("submit", "B", "buy", 1),
                        ("submit", "S", "sell", 1), ("close", 2)])
        assert pair.sim.position == 0 and len(pair.sim.fills) == 2
        assert not any(context.flags.values())


@pytest.mark.parametrize("changes", [
    {"cash": "1e65"}, {"cash": "1e-65"}, {"cash": "9" * 65},
    {"fee_rate": "1e-65"}, {"fee_rate": "0.1" + "0" * 64},
])
def test_numeric_unsupported_constructor_values_fail_closed(changes):
    with pytest.raises(ValueError, match="numeric envelope"):
        TickSimulator(**config(**changes))


@pytest.mark.parametrize("changes", [
    {"ask": "1e65"}, {"bid": "1e-65"}, {"ask": "9" * 65},
    {"ask_size": 10 ** 18 + 1}, {"bid_size": 10 ** 18 + 1},
])
def test_numeric_out_of_range_quote_does_not_advance_or_poison_replay(changes):
    sim = TickSimulator(**config(buy_latency_ns=1))
    sim.on_event(quote())
    sim.submit("B", "buy", 1)
    before = observable(sim), list(sim.audit)
    with pytest.raises(ValueError, match="numeric envelope"):
        sim.on_event(quote(2, 1, **changes))
    assert (observable(sim), sim.audit) == before
    # Same sequence remains usable; the rejected event neither filled old orders
    # nor advanced the receive-order cursor.
    sim.on_event(quote(2, 1))
    assert sim.position == 1 and sim.fills[0].time_ns == 1


def test_numeric_quantity_envelope_is_checked_before_order_creation():
    sim = TickSimulator(**config())
    sim.on_event(quote())
    before = observable(sim), list(sim.audit)
    with pytest.raises(ValueError, match="numeric envelope"):
        sim.submit("B", "buy", 10 ** 18 + 1)
    assert (observable(sim), sim.audit) == before
    sim.submit("B", "buy", 1)
    assert sim.position == 1


def test_numeric_maximum_quantity_uses_integer_capacity_not_decimal_quotient_limit():
    n = 10 ** 18
    with localcontext(Context(prec=1)):
        sim = TickSimulator(**config(cash=3 * n))
        sim.on_event(quote(bid_size=n, ask_size=n))
        sim.submit("B", "buy", n)
        assert sim.cash == 0 and sim.position == n and sim.orders["B"].remaining == 0
        sim.submit("S", "sell", n)
        assert sim.position == 0 and sim.cash == 2 * n


def test_numeric_ledger_guard_does_not_commit_or_consume_a_rejected_fill(monkeypatch):
    import execution.tick_simulator as module
    sim = TickSimulator(**config(cash="10", fee_rate="0.01"))
    sim.on_event(quote(ask_size=2))
    # Synthetic reduced envelope makes the normally unreachable long-run limit
    # testable without enormous state or modifying the account balance directly.
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_LEDGER_DIGITS", 2)
        with pytest.raises(ValueError, match="resulting cash"):
            sim.submit("B", "buy", 2)
    assert sim.cash == 10 and sim.position == 0 and sim.fills == []
    assert sim.orders["B"].remaining == 2 and sim.orders["B"].status == "pending"
    sim.advance(0)
    assert sim.position == 2 and sim.cash == Decimal("3.94")
    assert len(sim.fills) == 1 and sim.fills[0].quantity == 2
