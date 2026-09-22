"""참인 변형 관계와 참이 아닌 advance 분할 반례를 분리한다."""
from dataclasses import replace
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from itertools import permutations, product

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
    assert ref.state.fills == () and ref.state.cash == Decimal("1.01")
    assert sim.position == 1 and sim.cash == Decimal("-0.01")
    assert sim.fills[0].fee == Decimal("0.00505")


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="NUM-1: 유효하지만 낮은 Decimal precision에서 현금 보존/독립 oracle 위반; 미수정")
def test_known_low_precision_must_preserve_cash_and_match_exact_oracle():
    sim, ref = low_precision_case()
    assert sim.cash >= 0
    assert observable(sim) == ref.export()
