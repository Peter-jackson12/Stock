"""6,912개 유한 parameter/program 조합. unique reachable state 수와는 다르다.

모든 public command 뒤 oracle differential + 독립 ledger invariant를 검사한다.
CI의 -s 출력은 실행한 조합 수와 checkpoint 수를 별도로 남긴다.
"""
import ast
from fractions import Fraction
from itertools import product
from pathlib import Path
from time import perf_counter

from execution_audit_support import Pair, config, quote
from execution_oracle import Specification


def finish(name, count, checkpoints, expected, started):
    assert count == expected
    print(f"ORACLE_MATRIX {name}: traces={count} checkpoints={checkpoints} seconds={perf_counter()-started:.3f}")


def test_oracle_has_only_standard_library_dependencies():
    tree = ast.parse(Path(__file__).with_name("execution_oracle.py").read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module)
    assert modules == {"dataclasses", "fractions"}


def test_oracle_hand_calculated_two_sided_fee_ledger():
    ref = Specification(config(cash="6.60", fee_rate="0.1"))
    for command in (("event", quote()), ("submit", "B", "buy", 2), ("submit", "S", "sell", 2)):
        ref.apply(command)
    assert ref.state.cash == Fraction("3.6") and ref.state.position == 0
    assert ref.state.fills == (("B", "buy", 2, Fraction(3), 0, Fraction("0.6"), 1),
                               ("S", "sell", 2, Fraction(2), 0, Fraction("0.4"), 1))
    # 同じ source を使わず、資金不足の正解も手計算で固定する。
    poor = Specification(config(cash="3.02", fee_rate="0.01"))
    poor.apply(("event", quote()))
    poor.apply(("submit", "B", "buy", 1))
    assert poor.state.fills == () and poor.state.cash == Fraction("3.02")


def test_exhaustive_order_lifecycle():
    started, count, checks = perf_counter(), 0, 0
    grid = product(("buy", "sell"), (1, 2), range(3), range(3), (False, True),
                   (1, 2), (1, 2), (1, 2, 3), range(3))
    for side, qty, delay, cancel_delay, cancel, nb, na, close, age in grid:
        pair = Pair(config(**{side + "_latency_ns": delay}, cancel_latency_ns=cancel_delay,
                           max_quote_age_ns=age))
        if side == "sell":
            pair.apply("event", quote()).apply("submit", "seed", "buy", 2)
            pair.apply("event", quote(2, bid_size=nb, ask_size=na))
        else:
            pair.apply("event", quote(bid_size=nb, ask_size=na))
        pair.apply("submit", "target", side, qty)
        if cancel:
            pair.apply("cancel", "target")
        pair.apply("close", close)
        count += 1
        checks += pair.checkpoints
    finish("lifecycle", count, checks, 2592, started)


def test_exhaustive_two_order_resource_competition():
    started, count, checks = perf_counter(), 0, 0
    grid = product(product(("buy", "sell"), repeat=2), product((1, 2), repeat=2),
                   range(3), range(3), (0, 3, 6), ((2, 3, 1, 2), (3, 4, 2, 1)), (False, True))
    for sides, amounts, buy_delay, sell_delay, residual_cash, shape, seed in grid:
        bid, ask, nb, na = shape
        pair = Pair(config(cash=residual_cash + (ask if seed else 0), buy_latency_ns=buy_delay,
                           sell_latency_ns=sell_delay, cancel_latency_ns=1, max_quote_age_ns=10))
        pair.apply("event", quote(bid=bid, ask=ask))
        if seed:
            pair.apply("submit", "seed", "buy", 1)
        pair.apply("advance", 2)
        pair.apply("event", quote(2, 2, bid=bid, ask=ask, bid_size=nb, ask_size=na))
        for identity, side, qty in zip(("z", "a"), sides, amounts):
            pair.apply("submit", identity, side, qty)
        pair.apply("advance", 3)
        pair.apply("event", quote(3, 3, bid=bid+1, ask=ask+1, bid_size=nb, ask_size=na))
        pair.apply("cancel", "z").apply("advance", 4).apply("close", 6)
        count += 1
        checks += pair.checkpoints
    finish("resources", count, checks, 1728, started)


def test_exhaustive_quote_timing_and_stale_boundaries():
    started, count, checks = perf_counter(), 0, 0
    grid = product(product(((2, 3), (4, 5)), repeat=2), (1, 2), (1, 2),
                   range(3), range(3), range(3), ("quote", "trade"))
    for prices, nb, na, time, delay, age, kind in grid:
        (b1, a1), (b2, a2) = prices
        pair = Pair(config(buy_latency_ns=delay, max_quote_age_ns=age))
        pair.apply("event", quote(bid=b1, ask=a1, bid_size=nb, ask_size=na))
        pair.apply("submit", "B", "buy", 2)
        pair.apply("event", quote(2, time, kind=kind, bid=b2, ask=a2,
                                  bid_size=nb, ask_size=na, price=999, volume=2, is_buy=True))
        pair.apply("advance", 3).apply("close", 4)
        count += 1
        checks += pair.checkpoints
    finish("quote_clock", count, checks, 864, started)


def test_exhaustive_three_command_words():
    started, count, checks = perf_counter(), 0, 0
    alphabet = ("qa", "qb", "invalid", "trade", "buy", "sell", "cancel_first", "advance")
    for delay, word in product(range(3), product(alphabet, repeat=3)):
        pair = Pair(config(buy_latency_ns=delay, sell_latency_ns=delay, cancel_latency_ns=delay))
        now, seq, ids = 0, 0, []
        for letter in word:
            if letter in ("qa", "qb", "invalid", "trade"):
                seq += 1
                e = quote(seq, now)
                if letter == "qb":
                    e = quote(seq, now, bid=4, ask=5, bid_size=1, ask_size=1)
                elif letter == "invalid":
                    e = quote(seq, now, ask=1)
                elif letter == "trade":
                    e = quote(seq, now, kind="trade", price=999, volume=2, is_buy=True)
                pair.apply("event", e)
            elif letter in ("buy", "sell"):
                identity = f"order-{len(ids)}"
                ids.append(identity)
                pair.apply("submit", identity, letter, 2)
            elif letter == "cancel_first":
                if ids:  # empty-order program symbol is explicitly a no-op.
                    pair.apply("cancel", ids[0])
            else:
                now += 1
                pair.apply("advance", now)
        pair.apply("close", now + 3)
        count += 1
        checks += pair.checkpoints
    finish("words", count, checks, 1536, started)


def test_exhaustive_cash_fee_partial_fill_boundaries():
    started, count, checks = perf_counter(), 0, 0
    grid = product(("0", "2.99", "3", "3.02", "3.03", "6", "6.06", "6.60"),
                   ("0", "0.01", "0.1"), (1, 2), (1, 2), (False, True))
    for cash, fee, qty, size, refresh in grid:
        pair = Pair(config(cash=cash, fee_rate=fee))
        pair.apply("event", quote(ask_size=size, bid_size=size))
        pair.apply("submit", "B", "buy", qty)
        if refresh:
            pair.apply("event", quote(2, 1, ask_size=size, bid_size=size))
        pair.apply("submit", "S", "sell", qty).apply("close", 3)
        count += 1
        checks += pair.checkpoints
    finish("fees", count, checks, 192, started)
