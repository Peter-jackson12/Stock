"""Minimal receive-order replay kernel; not a raw-v1 reader or a fill model.

Common sequence and monotonic receipt time must come from the collector. Never
manufacture them by merging the two legacy tables' rowids or second timestamps.
This in-memory envelope is not the complete proposed raw-v2 storage schema.
"""
from dataclasses import dataclass
from typing import Iterable, Iterator, Literal


class TickOrderError(ValueError):
    pass


@dataclass(frozen=True)
class OrderedTick:
    source: str
    session_id: str
    seq: int
    received_ns: int
    code: str
    venue: str
    kind: Literal["trade", "quote"]
    # Retain values; validation of price levels/volume belongs to another layer.
    price: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    # Optional, source-normalized strategy inputs; never infer wall time from ns.
    market_second: int | None = None
    volume: int | None = None
    is_buy: bool | None = None
    bid_sizes: tuple[int, ...] | None = None
    ask_sizes: tuple[int, ...] | None = None


@dataclass(frozen=True)
class TickView:
    event: OrderedTick
    quote: OrderedTick | None
    quote_age_ns: int | None
    quote_status: Literal["missing", "observed", "stale"]


class ReceiveOrderReplay:
    """One source/session per instance; feed chunks without resetting state.

    Every quote update is emitted as an event. 'observed' describes recency only,
    not executable liquidity or validity. A broker must validate price/size too.
    """
    def __init__(self, *, source: str, session_id: str, max_quote_age_ns: int):
        if not source or not session_id:
            raise TickOrderError("source/session_id required")
        if type(max_quote_age_ns) is not int or max_quote_age_ns < 0:
            raise TickOrderError("max_quote_age_ns must be a nonnegative integer")
        self.source, self.session_id = source, session_id
        self.max_quote_age_ns = max_quote_age_ns
        self.last_seq = None
        self.last_received_ns = None
        self.quotes: dict[tuple[str, str], OrderedTick] = {}

    def accept(self, event: OrderedTick) -> TickView:
        if (event.source, event.session_id) != (self.source, self.session_id):
            raise TickOrderError("cannot merge source/session timelines without an explicit policy")
        if type(event.seq) is not int or event.seq < 1:
            raise TickOrderError("collector sequence required; legacy rowid is not a common sequence")
        if self.last_seq is not None and event.seq <= self.last_seq:
            raise TickOrderError("duplicate or reversed sequence")
        if type(event.received_ns) is not int or event.received_ns < 0:
            raise TickOrderError("monotonic receipt time required")
        if self.last_received_ns is not None and event.received_ns < self.last_received_ns:
            raise TickOrderError("receipt clock moved backwards")
        if event.kind not in ("trade", "quote") or not event.code or not event.venue:
            raise TickOrderError("event kind/code/venue required; use explicit unknown venue when needed")
        # Validate before mutating; a rejected event must not poison the state.
        key = (event.code, event.venue)
        if event.kind == "quote":
            self.quotes[key] = event
        quote = self.quotes.get(key)
        age = event.received_ns - quote.received_ns if quote else None
        status = "missing" if quote is None else ("stale" if age > self.max_quote_age_ns else "observed")
        self.last_seq, self.last_received_ns = event.seq, event.received_ns
        return TickView(event, quote, age, status)

    def consume(self, events: Iterable[OrderedTick]) -> Iterator[TickView]:
        for event in events:
            yield self.accept(event)
