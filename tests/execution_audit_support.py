"""감사 fixture와 외부 관측 비교. oracle 본체와 production 의존성을 분리한다."""
from collections import Counter
from dataclasses import replace
from fractions import Fraction

from engine.tick_ordering import OrderedTick
from execution.tick_simulator import TickSimulator
from execution_oracle import Specification


def config(**changes):
    return dict(source="oracle", session_id="synthetic", code="T", venue="unknown",
                cash=8, fee_rate="0", max_quote_age_ns=2, buy_latency_ns=0,
                sell_latency_ns=0, cancel_latency_ns=0) | changes


def quote(seq=1, ns=0, **changes):
    event = OrderedTick("oracle", "synthetic", seq, ns, "T", "unknown", "quote",
                        bid=2, ask=3, bid_size=2, ask_size=2)
    return replace(event, **changes)


def invoke(sim, command):
    op, *args = command
    return getattr(sim, "on_event" if op == "event" else op)(*args)


def observable(sim):
    orders = tuple((o.order_id, o.side, o.quantity, o.remaining, o.ready_ns, o.cancel_ns, o.status)
                   for o in sim.orders.values())
    fills = tuple((f.order_id, f.side, f.quantity, Fraction(str(f.price)), f.time_ns,
                   Fraction(str(f.fee)), f.quote_seq) for f in sim.fills)
    return (sim.now, Fraction(str(sim.cash)), sim.position, orders, fills, sim.closed)


def economic(sim):
    """알파 변환에서 order ID를 제외한 순서 있는 경제적 관측."""
    now, cash, position, orders, fills, closed = observable(sim)
    return (now, cash, position, tuple(o[1:] for o in orders),
            tuple(f[1:] for f in fills), closed)


class Pair:
    def __init__(self, cfg):
        self.cfg = cfg
        self.oracle = Specification(cfg)
        self.sim = TickSimulator(**cfg)
        self.trace = []
        self.quotes = {}
        self.checkpoints = 0
        self.close_ns = None

    def apply(self, *command):
        self.trace.append(command)
        if command[0] == "event" and command[1].kind == "quote":
            self.quotes[command[1].seq] = command[1]
        if command[0] == "close":
            self.close_ns = command[1]
        # oracle를 먼저 독립적으로 전이시킨다. SUT 출력의 golden 캡처는 없다.
        expected_return = self.oracle.apply(command)
        actual_return = invoke(self.sim, command)
        actual, expected = observable(self.sim), self.oracle.export()
        assert actual == expected, (self.cfg, self.trace, "production", actual, "oracle", expected)
        if command[0] in ("submit", "cancel"):
            assert actual_return == expected_return
        self.assert_invariants()
        self.checkpoints += 1
        return self

    def assert_invariants(self):
        """oracle와 별개의 fill ledger 보존식; production private 상태는 읽지 않는다."""
        sim = self.sim
        assert sim.cash >= 0 and sim.position >= 0
        cash = Fraction(str(self.cfg["cash"]))
        position = 0
        quantities, liquidity = Counter(), Counter()
        for fill in sim.fills:
            order = sim.orders[fill.order_id]
            assert fill.quantity > 0 and fill.side == order.side
            assert fill.time_ns >= order.ready_ns
            assert order.cancel_ns is None or fill.time_ns < order.cancel_ns
            assert self.close_ns is None or fill.time_ns < self.close_ns
            q = self.quotes[fill.quote_seq]
            assert 0 <= fill.time_ns - q.received_ns <= self.cfg["max_quote_age_ns"]
            assert 0 < Fraction(str(q.bid)) < Fraction(str(q.ask))
            assert Fraction(str(fill.price)) == Fraction(str(q.ask if fill.side == "buy" else q.bid))
            quantities[fill.order_id] += fill.quantity
            liquidity[fill.quote_seq, fill.side] += fill.quantity
            size = q.ask_size if fill.side == "buy" else q.bid_size
            assert liquidity[fill.quote_seq, fill.side] <= size
            gross = Fraction(str(fill.price)) * fill.quantity
            fee = Fraction(str(fill.fee))
            assert fee == gross * Fraction(str(self.cfg["fee_rate"]))
            cash += -gross - fee if fill.side == "buy" else gross - fee
            position += fill.quantity if fill.side == "buy" else -fill.quantity
            assert cash >= 0 and position >= 0
        assert cash == Fraction(str(sim.cash)) and position == sim.position
        for order in sim.orders.values():
            assert quantities[order.order_id] + order.remaining == order.quantity
            assert 0 <= order.remaining <= order.quantity
            if order.status == "filled":
                assert order.remaining == 0
            if sim.closed:
                assert order.status in ("filled", "cancelled", "expired")
