"""Deterministic single-instrument, long-only market-order research simulator.

Timers precede external events at the same timestamp; cancellation wins ties.
Zero-latency submissions execute after the triggering event, never before it.
Each new quote sequence replenishes displayed liquidity (an explicit simulation
assumption); trades/timers do not replenish it. No queue position or market impact.
"""
from dataclasses import dataclass
from decimal import Decimal

from engine.tick_ordering import ReceiveOrderReplay
from execution.quote_validation import check_ordered_quote


@dataclass
class SimOrder:
    order_id: str
    side: str
    quantity: int
    remaining: int
    ready_ns: int
    status: str = "pending"
    cancel_ns: int | None = None


@dataclass(frozen=True)
class SimFill:
    order_id: str
    time_ns: int
    side: str
    quantity: int
    price: Decimal
    fee: Decimal
    quote_seq: int


def _ns(value):
    if type(value) is not int or value < 0:
        raise ValueError("time/latency must be a nonnegative integer")
    return value


class TickSimulator:
    def __init__(self, *, source, session_id, code, venue, cash,
                 max_quote_age_ns, buy_latency_ns, sell_latency_ns,
                 cancel_latency_ns, fee_rate=Decimal("0")):
        self.replay = ReceiveOrderReplay(source=source, session_id=session_id,
                                        max_quote_age_ns=max_quote_age_ns)
        if not code or not venue:
            raise ValueError("code and venue required")
        self.code, self.venue = code, venue
        self.cash, self.fee_rate = Decimal(str(cash)), Decimal(str(fee_rate))
        if not self.cash.is_finite() or self.cash < 0:
            raise ValueError("invalid cash")
        if not self.fee_rate.is_finite() or not 0 <= self.fee_rate < 1:
            raise ValueError("invalid fee rate")
        self.latency = {"buy": _ns(buy_latency_ns), "sell": _ns(sell_latency_ns)}
        self.cancel_latency = _ns(cancel_latency_ns)
        self.now = 0
        self.position = 0
        self.orders = {}
        self.fills = []
        self.audit = []
        self.closed = False
        self._view = None
        self._quote_seq = None
        self._left = {"buy": 0, "sell": 0}

    def submit(self, order_id, side, quantity):
        if self.closed:
            raise ValueError("session closed")
        if not isinstance(order_id, str) or not order_id or order_id in self.orders:
            raise ValueError("unique nonempty order id required")
        if side not in self.latency or type(quantity) is not int or quantity <= 0:
            raise ValueError("valid side and positive integer quantity required")
        order = SimOrder(order_id, side, quantity, quantity, self.now + self.latency[side])
        self.orders[order_id] = order
        self.audit.append((self.now, order_id, "submitted"))
        self._match()
        return order_id

    def cancel(self, order_id):
        if self.closed:
            raise ValueError("session closed")
        order = self.orders[order_id]
        if order.status in ("filled", "cancelled", "expired") or order.cancel_ns is not None:
            return False
        order.cancel_ns = self.now + self.cancel_latency
        self.audit.append((self.now, order_id, "cancel_requested"))
        self._match()
        return True

    def _match(self):
        for order in self.orders.values():
            if order.status in ("filled", "cancelled", "expired"):
                continue
            if order.cancel_ns is not None and order.cancel_ns <= self.now:
                order.status = "cancelled"
                self.audit.append((self.now, order.order_id, "cancelled"))
        if self._view is None:
            return
        q = self._view.quote
        if q is None or self.now - q.received_ns > self.replay.max_quote_age_ns:
            return
        checked = check_ordered_quote(self._view)
        if checked.book is None:
            return
        book = checked.book
        for order in self.orders.values():
            if order.status in ("filled", "cancelled", "expired") or order.ready_ns > self.now:
                continue
            price = book.ask if order.side == "buy" else book.bid
            capacity = (int(self.cash // (price * (1 + self.fee_rate)))
                        if order.side == "buy" else self.position)
            quantity = min(order.remaining, self._left[order.side], capacity)
            if quantity <= 0:
                continue
            gross = quantity * price
            fee = gross * self.fee_rate
            self.cash += -gross - fee if order.side == "buy" else gross - fee
            self.position += quantity if order.side == "buy" else -quantity
            self._left[order.side] -= quantity
            order.remaining -= quantity
            order.status = "filled" if order.remaining == 0 else "partial"
            self.fills.append(SimFill(order.order_id, self.now, order.side, quantity, price, fee, q.seq))
            self.audit.append((self.now, order.order_id, order.status))

    def advance(self, time_ns):
        _ns(time_ns)
        if self.closed or time_ns < self.now:
            raise ValueError("closed session or clock reversal")
        deadlines = sorted({deadline for o in self.orders.values()
                            if o.status not in ("filled", "cancelled", "expired")
                            for deadline in (o.ready_ns, o.cancel_ns)
                            if deadline is not None and self.now < deadline <= time_ns})
        for deadline in deadlines:
            self.now = deadline
            self._match()
        self.now = time_ns
        self._match()

    def on_event(self, event):
        if self.closed or event.received_ns < self.now:
            raise ValueError("closed session or event behind replay clock")
        if (event.code, event.venue) != (self.code, self.venue):
            raise ValueError("single-instrument simulator cannot merge instruments/venues")
        # Validate source/order before processing any pending deadlines.
        view = self.replay.accept(event)
        self.advance(event.received_ns)
        self._view = view
        if event.kind == "quote":
            self._quote_seq = event.seq
            checked = check_ordered_quote(view)
            self._left = ({"buy": checked.book.ask_size, "sell": checked.book.bid_size}
                          if checked.book else {"buy": 0, "sell": 0})
        self._match()
        return view

    def close(self, time_ns):
        """Close is exclusive: no fills at or after the closing boundary.

        Advance through earlier deadlines, expire remainders, retain open holdings.
        Never fabricate an end-of-session liquidation at the last known price.
        """
        _ns(time_ns)
        if self.closed or time_ns <= self.now:
            raise ValueError("exclusive close must be strictly after the processed clock")
        if time_ns > self.now:
            self.advance(time_ns - 1)
        self.now = time_ns
        for order in self.orders.values():
            if order.status not in ("filled", "cancelled", "expired"):
                order.status = "expired"
                self.audit.append((self.now, order.order_id, "expired"))
        self.closed = True
