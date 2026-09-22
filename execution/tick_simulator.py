"""Deterministic single-instrument, long-only market-order research simulator.

Timers precede external events at the same timestamp; cancellation wins ties.
Zero-latency submissions execute after the triggering event, never before it.
Each new quote sequence replenishes displayed liquidity (an explicit simulation
assumption); trades/timers do not replenish it. No queue position or market impact.
Money uses exact finite-decimal integer coefficients, not ambient Decimal rounding.
The bounded numeric envelope is descriptive research policy, not currency rules.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from engine.tick_ordering import ReceiveOrderReplay
from execution.quote_validation import check_ordered_quote


MAX_INPUT_DIGITS = 64
MAX_INPUT_EXPONENT = 64
MAX_LEDGER_DIGITS = 512
MAX_LEDGER_EXPONENT = 128
MAX_QUANTITY = 10 ** 18


def _bounded_decimal(value, name, *, ledger=False):
    """Check representation bounds without normalize()/ambient rounding."""
    digits = MAX_LEDGER_DIGITS if ledger else MAX_INPUT_DIGITS
    exponent = MAX_LEDGER_EXPONENT if ledger else MAX_INPUT_EXPONENT
    parts = value.as_tuple()
    if (not value.is_finite() or len(parts.digits) > digits
            or not -exponent <= parts.exponent <= exponent):
        raise ValueError(f"{name} outside exact numeric envelope ({digits} digits, exponent +/-{exponent})")


def _parts(value):
    sign, digits, exponent = value.as_tuple()
    coefficient = int("".join(map(str, digits)))
    return (-coefficient if sign else coefficient), exponent


def _from_parts(coefficient, exponent):
    # Decimal(tuple) preserves all digits/exponent independently of context.
    return Decimal((int(coefficient < 0), tuple(map(int, str(abs(coefficient)))), exponent))


def _money_add(left, right):
    a, ae = _parts(left)
    b, be = _parts(right)
    exponent = min(ae, be)
    return _from_parts(a * 10 ** (ae - exponent) + b * 10 ** (be - exponent), exponent)


def _money_mul(left, right):
    a, ae = _parts(left)
    b, be = _parts(right)
    return _from_parts(a * b, ae + be)


def _money_floor_ratio(numerator, denominator):
    a, ae = _parts(numerator)
    b, be = _parts(denominator)
    exponent = min(ae, be)
    return (a * 10 ** (ae - exponent)) // (b * 10 ** (be - exponent))


def _quote_numeric_envelope(event):
    """Reject unsupported finite quote magnitudes before replay/timer mutation.

    Missing/nonpositive/nonfinite/malformed quotes still use the existing
    withhold-fill policy. Trade payloads are not executable quote prices.
    """
    if event.kind != "quote":
        return
    for name in ("bid", "ask", "bid_size", "ask_size"):
        value = getattr(event, name)
        if type(value) not in (str, int, float, Decimal):
            continue
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            continue
        if not number.is_finite() or number <= 0:
            continue
        if name.endswith("_size"):
            if number > MAX_QUANTITY:
                raise ValueError(f"{name} outside exact numeric envelope (maximum {MAX_QUANTITY})")
        else:
            _bounded_decimal(number, name)


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
        _bounded_decimal(self.cash, "cash")
        _bounded_decimal(self.fee_rate, "fee_rate")
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
        if quantity > MAX_QUANTITY:
            raise ValueError(f"quantity outside exact numeric envelope (maximum {MAX_QUANTITY})")
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
            buying = order.side == "buy"
            capacity = (_money_floor_ratio(self.cash, _money_mul(price, _money_add(Decimal(1), self.fee_rate)))
                        if buying else self.position)
            quantity = min(order.remaining, self._left[order.side], capacity)
            if quantity <= 0:
                continue
            gross = _money_mul(Decimal(quantity), price)
            fee = _money_mul(gross, self.fee_rate)
            cost = _money_add(gross, fee if buying else fee.copy_negate())
            # All arithmetic/validation precedes committing this fill. Never clamp
            # negative cash or repair a fill after consuming quantity/liquidity.
            if buying and cost > self.cash:
                raise ArithmeticError("exact buy cost exceeds pre-fill cash")
            next_cash = _money_add(self.cash, cost.copy_negate() if buying else cost)
            _bounded_decimal(next_cash, "resulting cash", ledger=True)
            if next_cash < 0:
                raise ArithmeticError("exact fill would violate cash solvency")
            self.cash = next_cash
            self.position += quantity if buying else -quantity
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
        _quote_numeric_envelope(event)
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
