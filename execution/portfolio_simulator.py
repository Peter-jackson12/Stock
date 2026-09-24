"""합성 다종목 틱 연구용 공유 계좌. 기존 TickSimulator의 대체물이 아니다.

단일 source/session, 종목당 명시된 venue 하나, 공매도 없는 시장가 주문만 지원한다.
주문 제출 순번으로 자원을 배분하며 수신 공통 seq로 외부 이벤트를 처리한다.
기존 재생/호가 검증/정확한 현금 산술을 재사용한다. 실제 증권사 연결은 없다.

reservation_v1: 접수 때 전량의 현금(ask + fee) 또는 매도 수량을 예약한다.
매칭 직전 남은 전량을 재평가하여 부족하면 잔여 주문을 rejected로 종료한다.
이미 발생한 부분체결은 취소하지 않는다. 취소 대기 중 예약/체결 가능성은 유지된다.
missing/stale/invalid 호가는 접수 때 거절, 접수 후에는 체결 보류다.

총 노출 한도는 fresh ask * (보유량 + 미체결 매수량)의 합이다. 수수료를 제외한
매수 대체 원가 한도이며 equity/PnL/청산가 평가가 아니다. 한도를 켠 경우 관련
종목 중 하나라도 평가 불가이면 신규 매수를 막고, 매도는 이 한도로 막지 않는다.
전략별 별도 엔진은 별도 자본이며, 엔진 간 자본 공유/중재는 제공하지 않는다.
"""
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping

from engine.tick_ordering import OrderedTick, ReceiveOrderReplay, TickView
from execution.quote_validation import QuoteCheck, check_ordered_quote
from execution.tick_simulator import (
    MAX_QUANTITY, _bounded_decimal, _money_add, _money_mul, _ns,
    _quote_numeric_envelope,
)

ZERO = Decimal(0)
TERMINAL = frozenset({"filled", "cancelled", "expired", "rejected"})
TRANSITIONS = {
    "created": frozenset({"pending", "rejected"}),
    "pending": frozenset({"active", "cancel_pending", "cancelled", "expired"}),
    "active": frozenset({"partially_filled", "filled", "cancel_pending", "rejected", "expired"}),
    "partially_filled": frozenset({"partially_filled", "filled", "cancel_pending", "rejected", "expired"}),
    "cancel_pending": frozenset({"cancel_pending", "filled", "cancelled", "rejected", "expired"}),
}


def _decimal(value, name):
    if type(value) not in (str, int, float, Decimal):
        raise ValueError(f"invalid {name}")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid {name}") from exc
    _bounded_decimal(result, name)
    if result < 0:
        raise ValueError(f"negative {name}")
    return result


def _sum_money(values):
    result = ZERO
    for value in values:
        result = _money_add(result, value)
    return result


@dataclass(frozen=True)
class RiskLimits:
    """None은 해당 한도 비활성화다. 값은 실거래 권고가 아닌 실험 설정이다."""
    max_position_per_symbol: Mapping[str, int] | None = None
    max_gross_exposure: Decimal | None = None
    max_open_orders: int | None = None
    guard_symbol_orders: bool = True

    def __post_init__(self):
        limits = self.max_position_per_symbol
        if limits is not None and not isinstance(limits, Mapping):
            raise ValueError("position limits must be a mapping")
        limits = dict(limits or {})
        if any(not isinstance(code, str) or not code or type(n) is not int or
               not 0 <= n <= MAX_QUANTITY for code, n in limits.items()):
            raise ValueError("invalid position limit")
        if self.max_open_orders is not None and (type(self.max_open_orders) is not int or self.max_open_orders < 0):
            raise ValueError("invalid open order limit")
        if type(self.guard_symbol_orders) is not bool:
            raise ValueError("guard_symbol_orders must be bool")
        object.__setattr__(self, "max_position_per_symbol", MappingProxyType(dict(sorted(limits.items()))))
        if self.max_gross_exposure is not None:
            object.__setattr__(self, "max_gross_exposure", _decimal(self.max_gross_exposure, "exposure limit"))

    def settings(self):
        return dict(max_position_per_symbol=dict(self.max_position_per_symbol),
                    max_gross_exposure=self.max_gross_exposure,
                    max_open_orders=self.max_open_orders, guard_symbol_orders=self.guard_symbol_orders)


@dataclass(frozen=True)
class OrderIntent:
    order_id: str
    code: str
    side: str
    quantity: int
    reason: str = ""


@dataclass(frozen=True)
class CancelIntent:
    order_id: str


@dataclass(frozen=True)
class PortfolioOrder:
    order_id: str
    code: str
    venue: str
    side: str
    quantity: int
    remaining: int
    submitted_ns: int
    ready_ns: int
    submission_seq: int
    status: str = "created"
    cancel_ns: int | None = None
    reserved_cash: Decimal = ZERO
    reserved_quantity: int = 0
    reason: str = "created"


@dataclass(frozen=True)
class PortfolioFill:
    order_id: str
    code: str
    venue: str
    time_ns: int
    side: str
    quantity: int
    price: Decimal
    fee: Decimal
    quote_seq: int


@dataclass(frozen=True)
class OrderTransition:
    transition_seq: int
    time_ns: int
    order_id: str
    previous: str | None
    status: str
    reason: str
    remaining: int
    reserved_cash: Decimal
    reserved_quantity: int


@dataclass(frozen=True)
class PortfolioSnapshot:
    now: int
    cash: Decimal
    reserved_cash: Decimal
    available_cash: Decimal
    gross_exposure_at_ask: Decimal | None
    exposure_status: str
    positions: tuple[tuple[str, int], ...]
    orders: tuple[PortfolioOrder, ...]
    fills: tuple[PortfolioFill, ...]
    closed: bool


class PortfolioSimulator:
    """하나의 시계와 공유 자본을 관리하는 opt-in 연구 엔진."""

    def __init__(self, *, source, session_id, instruments, cash, fee_rate,
                 max_quote_age_ns, buy_latency_ns, sell_latency_ns,
                 cancel_latency_ns, risk=None):
        if not isinstance(instruments, Mapping) or not instruments:
            raise ValueError("nonempty code-to-venue mapping required")
        if any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
               for k, v in instruments.items()):
            raise ValueError("nonempty code/venue strings required")
        self._instruments = dict(sorted(instruments.items()))
        self._risk = RiskLimits() if risk is None else risk
        if type(self._risk) is not RiskLimits:
            raise ValueError("risk must be RiskLimits")
        if set(self._risk.max_position_per_symbol) - set(self._instruments):
            raise ValueError("risk limit for undeclared symbol")
        self._replay = ReceiveOrderReplay(source=source, session_id=session_id,
                                         max_quote_age_ns=max_quote_age_ns)
        self._cash = _decimal(cash, "cash")
        self._initial_cash = self._cash
        self._fee_rate = _decimal(fee_rate, "fee_rate")
        if self._fee_rate >= 1:
            raise ValueError("fee_rate must be less than one")
        self._latency = {"buy": _ns(buy_latency_ns), "sell": _ns(sell_latency_ns)}
        self._cancel_latency = _ns(cancel_latency_ns)
        self._now, self._closed = 0, False
        self._positions = {code: 0 for code in self._instruments}
        self._orders, self._priority = {}, []
        self._fills, self._transitions = [], []
        # Do not match against replay.quotes: accept() sees a new quote before
        # timers at that timestamp have run. Only applied views are executable.
        self._views = {}
        self._liquidity = {code: {"buy": 0, "sell": 0} for code in self._instruments}

    def settings(self):
        return dict(source=self._replay.source, session_id=self._replay.session_id,
                    instruments=dict(self._instruments), cash=self._initial_cash,
                    fee_rate=self._fee_rate, max_quote_age_ns=self._replay.max_quote_age_ns,
                    buy_latency_ns=self._latency["buy"], sell_latency_ns=self._latency["sell"],
                    cancel_latency_ns=self._cancel_latency, risk=self._risk.settings(),
                    contract="portfolio_reservation_v1")

    @property
    def orders(self):
        return tuple(self._orders[key] for key in self._priority)

    @property
    def fills(self):
        return tuple(self._fills)

    @property
    def transitions(self):
        return tuple(self._transitions)

    def _live(self, exclude=None):
        return tuple(o for o in self.orders if o.status not in TERMINAL and o.order_id != exclude)

    def _exposure(self, orders):
        quantities = dict(self._positions)
        for order in orders:
            if order.side == "buy":
                quantities[order.code] += order.remaining
        exposure = ZERO
        for code in self._instruments:
            if quantities[code] == 0:
                continue
            checked = self._quote(code)
            if checked.book is None:
                return None
            exposure = _money_add(exposure, _money_mul(checked.book.ask, Decimal(quantities[code])))
        _bounded_decimal(exposure, "exposure", ledger=True)
        return exposure

    def snapshot(self):
        exposure = self._exposure(self._live())
        reserved = _sum_money(o.reserved_cash for o in self._live())
        return PortfolioSnapshot(self._now, self._cash, reserved,
                                 _money_add(self._cash, reserved.copy_negate()),
                                 exposure, "priced_at_fresh_ask" if exposure is not None else "unpriced",
                                 tuple(self._positions.items()), self.orders, self.fills, self._closed)

    def _log(self, previous, order):
        self._transitions.append(OrderTransition(
            len(self._transitions) + 1, self._now, order.order_id, previous,
            order.status, order.reason, order.remaining, order.reserved_cash, order.reserved_quantity))

    def _transition(self, order, status, reason, **changes):
        if status not in TRANSITIONS.get(order.status, ()):
            raise AssertionError(f"invalid transition {order.status} -> {status}")
        if status in TERMINAL:
            changes.update(reserved_cash=ZERO, reserved_quantity=0)
        changed = replace(order, status=status, reason=reason, **changes)
        self._orders[order.order_id] = changed
        self._log(order.status, changed)
        return changed

    def _quote(self, code):
        view = self._views.get(code)
        if view is None or view.quote is None:
            return QuoteCheck(None, "missing")
        age = self._now - view.quote.received_ns
        status = "stale" if age > self._replay.max_quote_age_ns else "observed"
        return check_ordered_quote(TickView(view.event, view.quote, age, status))

    def _risk_check(self, order, book):
        others = self._live(exclude=order.order_id)
        if order.side == "sell":
            reserved = sum(o.reserved_quantity for o in others if o.code == order.code)
            return (ZERO, "insufficient_holdings") if order.remaining > self._positions[order.code] - reserved else (ZERO, None)
        gross = _money_mul(book.ask, Decimal(order.remaining))
        reserve = _money_add(gross, _money_mul(gross, self._fee_rate))
        _bounded_decimal(reserve, "reservation", ledger=True)
        available = _money_add(self._cash, _sum_money(o.reserved_cash for o in others).copy_negate())
        if reserve > available:
            return ZERO, "insufficient_cash"
        projected = self._positions[order.code] + order.remaining + sum(
            o.remaining for o in others if o.code == order.code and o.side == "buy")
        if projected > self._risk.max_position_per_symbol.get(order.code, MAX_QUANTITY):
            return ZERO, "max_position_per_symbol"
        if self._risk.max_gross_exposure is not None:
            exposure = self._exposure((*others, order))
            if exposure is None:
                return ZERO, "unpriced_exposure"
            if exposure > self._risk.max_gross_exposure:
                return ZERO, "max_gross_exposure"
        return reserve, None

    def submit(self, intent):
        if self._closed:
            raise ValueError("session closed")
        if type(intent) is not OrderIntent:
            raise ValueError("OrderIntent required")
        if not isinstance(intent.order_id, str) or not intent.order_id or intent.order_id in self._orders:
            raise ValueError("unique nonempty order id required")
        if (not isinstance(intent.code, str) or not isinstance(intent.side, str)
                or intent.code not in self._instruments or intent.side not in self._latency):
            raise ValueError("declared symbol and valid side required")
        if not isinstance(intent.reason, str):
            raise ValueError("intent reason must be text")
        if type(intent.quantity) is not int or not 0 < intent.quantity <= MAX_QUANTITY:
            raise ValueError("quantity outside exact numeric envelope")
        order = PortfolioOrder(intent.order_id, intent.code, self._instruments[intent.code], intent.side,
                               intent.quantity, intent.quantity, self._now,
                               self._now + self._latency[intent.side], len(self._priority) + 1)
        existing = self._live()
        reason, reserve = None, ZERO
        if self._risk.max_open_orders is not None and len(existing) >= self._risk.max_open_orders:
            reason = "max_open_orders"
        elif self._risk.guard_symbol_orders and any(o.code == order.code for o in existing):
            reason = "duplicate_or_conflicting_order"
        else:
            checked = self._quote(order.code)
            if checked.book is None:
                reason = "quote_" + checked.reason
            else:
                reserve, reason = self._risk_check(order, checked.book)
        # Structural/numeric validation precedes order creation. Business rejects
        # are retained terminal orders, unlike malformed API calls.
        self._priority.append(order.order_id)
        self._orders[order.order_id] = order
        self._log(None, order)
        if reason:
            self._transition(order, "rejected", "admission:" + reason)
        else:
            self._transition(order, "pending", "admitted", reserved_cash=reserve,
                             reserved_quantity=order.remaining if order.side == "sell" else 0)
            self._match()
        return order.order_id

    def cancel(self, order_id):
        if self._closed:
            raise ValueError("session closed")
        order = self._orders[order_id]
        if order.status in TERMINAL or order.cancel_ns is not None:
            return False
        self._transition(order, "cancel_pending", "cancel_requested",
                         cancel_ns=self._now + self._cancel_latency)
        self._match()
        return True

    def _match(self):
        # Cancellation acknowledgements globally precede every fill, regardless
        # of instrument or submission priority.
        for order in self._live():
            if order.cancel_ns is not None and order.cancel_ns <= self._now:
                self._transition(order, "cancelled", "cancel_acknowledged")
        for order in self._live():
            if order.ready_ns > self._now:
                continue
            if order.status == "pending":
                order = self._transition(order, "active", "latency_elapsed")
            checked = self._quote(order.code)
            if checked.book is None:
                continue
            reserve, reason = self._risk_check(order, checked.book)
            if reason:
                self._transition(order, "rejected", "matching:" + reason)
                continue
            # Reprice the full remainder before consuming any liquidity. A cash
            # reservation is not a price guarantee or an exchange limit order.
            repriced = replace(order, reserved_cash=reserve)
            quantity = min(order.remaining, self._liquidity[order.code][order.side])
            if quantity == 0:
                if repriced.reserved_cash != order.reserved_cash:
                    repriced = replace(repriced, reason="reservation_repriced")
                    self._orders[order.order_id] = repriced
                    self._log(order.status, repriced)
                continue
            buying = order.side == "buy"
            price = checked.book.ask if buying else checked.book.bid
            gross = _money_mul(price, Decimal(quantity))
            fee = _money_mul(gross, self._fee_rate)
            debit = _money_add(gross, fee if buying else fee.copy_negate())
            cash = _money_add(self._cash, debit.copy_negate() if buying else debit)
            _bounded_decimal(cash, "resulting cash", ledger=True)
            remaining = order.remaining - quantity
            reservation = _money_add(reserve, debit.copy_negate()) if buying else ZERO
            if cash < 0 or reservation < 0:
                raise ArithmeticError("fill would violate reserved cash solvency")
            self._cash = cash
            self._positions[order.code] += quantity if buying else -quantity
            self._liquidity[order.code][order.side] -= quantity
            self._fills.append(PortfolioFill(order.order_id, order.code, order.venue, self._now,
                                              order.side, quantity, price, fee, self._views[order.code].quote.seq))
            status = ("filled" if remaining == 0 else "cancel_pending" if order.cancel_ns is not None
                      else "partially_filled")
            self._transition(repriced, status, "fill", remaining=remaining, reserved_cash=reservation,
                             reserved_quantity=remaining if not buying else 0)

    def advance(self, time_ns):
        _ns(time_ns)
        if self._closed or time_ns < self._now:
            raise ValueError("closed session or clock reversal")
        deadlines = sorted({d for o in self._live() for d in (o.ready_ns, o.cancel_ns)
                            if d is not None and self._now < d <= time_ns})
        for deadline in deadlines:
            self._now = deadline
            self._match()
        self._now = time_ns
        self._match()

    def on_event(self, event):
        if type(event) is not OrderedTick:
            raise ValueError("OrderedTick required")
        if self._closed or type(event.received_ns) is not int or event.received_ns < self._now:
            raise ValueError("closed session or event behind replay clock")
        if self._instruments.get(event.code) != event.venue:
            raise ValueError("undeclared instrument/venue")
        _quote_numeric_envelope(event)
        view = self._replay.accept(event)
        self.advance(event.received_ns)
        self._views[event.code] = view
        if event.kind == "quote":
            checked = check_ordered_quote(view)
            self._liquidity[event.code] = ({"buy": checked.book.ask_size, "sell": checked.book.bid_size}
                                           if checked.book else {"buy": 0, "sell": 0})
        self._match()
        return view

    def close(self, time_ns):
        _ns(time_ns)
        if self._closed or time_ns <= self._now:
            raise ValueError("exclusive close must be strictly after the processed clock")
        self.advance(time_ns - 1)
        self._now = time_ns
        for order in self._live():
            self._transition(order, "expired", "session_close")
        self._closed = True
