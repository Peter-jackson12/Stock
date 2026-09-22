"""테스트 전용 유한 execution specification. production import는 없다.

외부 규칙: 유효 top1, 취소 우선, 접수순 1회 자원 배분, 배타적 마감.
Fraction 부등식으로 가능한 정수 수량을 전부 열거한다. Decimal //, production
quote validator, replay clock, matching helper를 정답 생성에 사용하지 않는다.
시간 진행도 작은 정수 격자를 열거한다. 시각 구간은 32, 주문 수량은 8 이하다.

중요한 명세 선택: 한 배분 라운드에서 이미 지난 주문으로 되돌아가지 않는다.
공개 advance 호출은 deadline 라운드와 endpoint 라운드를 구분한다. 이는
기존 모호한 resource_policy의 호환성 해석이며 시장의 유일한 정답이 아니다.
EXECUTION_ORACLE_SPEC.md의 계약/구현 구분과 검증 한계를 함께 읽는다.
"""
from dataclasses import dataclass, replace
from fractions import Fraction


TERMINAL = frozenset(("filled", "cancelled", "expired"))


@dataclass(frozen=True)
class Order:
    identity: str
    side: str
    quantity: int
    remaining: int
    ready: int
    cancel: int | None = None
    status: str = "pending"


@dataclass(frozen=True)
class Book:
    sequence: int
    observed: int
    bid: Fraction
    ask: Fraction
    bid_size: int
    ask_size: int


@dataclass(frozen=True)
class State:
    time: int
    cash: Fraction
    position: int = 0
    book: Book | None = None
    bid_left: int = 0
    ask_left: int = 0
    orders: tuple = ()
    fills: tuple = ()
    closed: bool = False


def read_book(event):
    """독립적인 fixture 해석. 양쪽 유한 양수와 정수 잔량/양수 spread."""
    values = (event.bid, event.ask, event.bid_size, event.ask_size)
    try:
        if any(value is None or isinstance(value, bool) for value in values):
            return None
        bid, ask, nb, na = (Fraction(str(value)) for value in values)
        if not (0 < bid < ask and nb > 0 and na > 0 and nb.denominator == na.denominator == 1):
            return None
    except (ValueError, ZeroDivisionError):
        return None
    return Book(event.seq, event.received_ns, bid, ask, int(nb), int(na))


def allocate(state, fee, max_age):
    """불변 ledger의 접수순 fold. 각 주문의 feasible set 최대값을 선택한다."""
    orders = tuple(replace(order, status="cancelled")
                   if order.status not in TERMINAL and order.cancel is not None
                   and order.cancel <= state.time else order for order in state.orders)
    ledger = replace(state, orders=orders)
    book = ledger.book
    if book is None or ledger.time - book.observed > max_age:
        return ledger
    for index, order in enumerate(orders):
        if order.status in TERMINAL or order.ready > ledger.time:
            continue
        buying = order.side == "buy"
        price = book.ask if buying else book.bid
        visible = ledger.ask_left if buying else ledger.bid_left
        # 최대 8주: production의 capacity 나눗셈을 재현하지 않는 완전 열거.
        feasible = [n for n in range(order.remaining + 1)
                    if n <= visible and (n * price * (1 + fee) <= ledger.cash
                                         if buying else n <= ledger.position)]
        count = max(feasible, default=0)
        if not count:
            continue
        cost = count * price
        charge = cost * fee
        remaining = order.remaining - count
        revised = replace(order, remaining=remaining, status="filled" if remaining == 0 else "partial")
        fill = (order.identity, order.side, count, price, ledger.time, charge, book.sequence)
        ledger = replace(ledger,
            cash=ledger.cash + (-cost - charge if buying else cost - charge),
            position=ledger.position + (count if buying else -count),
            ask_left=ledger.ask_left - (count if buying else 0),
            bid_left=ledger.bid_left - (0 if buying else count),
            orders=ledger.orders[:index] + (revised,) + ledger.orders[index + 1:],
            fills=ledger.fills + (fill,))
    return ledger


class Specification:
    """검증된 작은 public command trace만 실행. 전략/DB/실피드는 범위 밖."""
    def __init__(self, config):
        self.state = State(0, Fraction(str(config["cash"])))
        self.fee = Fraction(str(config["fee_rate"]))
        self.age = config["max_quote_age_ns"]
        self.latency = {side: config[side + "_latency_ns"] for side in ("buy", "sell")}
        self.cancel_latency = config["cancel_latency_ns"]
        self.last_sequence = 0

    def pulse(self, time):
        self.state = allocate(replace(self.state, time=time), self.fee, self.age)

    def move(self, target):
        start = self.state.time
        assert not self.state.closed and 0 <= target - start <= 32
        # 호출 시작의 deadline 집합. 도중 terminal이 된 주문의 이미 예약된
        # 시각도 이 호출에서는 유지된다. 동일 deadline들은 한 라운드다.
        reserved = {t for order in self.state.orders if order.status not in TERMINAL
                    for t in (order.ready, order.cancel) if t is not None}
        for time in range(start + 1, target + 1):
            if time in reserved:
                self.pulse(time)
        self.pulse(target)  # deadline과 같더라도 별도의 공개 호출 endpoint.

    def apply(self, command):
        assert not self.state.closed
        op, *args = command
        if op == "event":
            event, = args
            assert event.seq > self.last_sequence and event.received_ns >= self.state.time
            self.move(event.received_ns)
            self.last_sequence = event.seq
            if event.kind == "quote":
                book = read_book(event)
                self.state = replace(self.state, book=book,
                                     bid_left=book.bid_size if book else 0,
                                     ask_left=book.ask_size if book else 0)
            else:
                assert event.kind == "trade"
            self.pulse(self.state.time)
        elif op == "submit":
            identity, side, quantity = args
            assert 1 <= quantity <= 8 and all(o.identity != identity for o in self.state.orders)
            order = Order(identity, side, quantity, quantity, self.state.time + self.latency[side])
            self.state = replace(self.state, orders=self.state.orders + (order,))
            self.pulse(self.state.time)
            return identity
        elif op == "cancel":
            identity, = args
            index = next(i for i, order in enumerate(self.state.orders) if order.identity == identity)
            order = self.state.orders[index]
            if order.status in TERMINAL or order.cancel is not None:
                return False
            order = replace(order, cancel=self.state.time + self.cancel_latency)
            self.state = replace(self.state, orders=self.state.orders[:index] + (order,) + self.state.orders[index + 1:])
            self.pulse(self.state.time)
            return True
        elif op == "advance":
            self.move(args[0])
        elif op == "close":
            boundary, = args
            assert boundary > self.state.time
            self.move(boundary - 1)
            orders = tuple(replace(order, status="expired") if order.status not in TERMINAL else order
                           for order in self.state.orders)
            self.state = replace(self.state, time=boundary, orders=orders, closed=True)
        else:
            raise AssertionError(f"unknown specification command: {op}")

    def export(self):
        state = self.state
        orders = tuple((o.identity, o.side, o.quantity, o.remaining, o.ready, o.cancel, o.status)
                       for o in state.orders)
        return (state.time, state.cash, state.position, orders, state.fills, state.closed)
