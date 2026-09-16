"""Read closed raw-v1 datasets for explicitly approximate research only.

Events are NOT OrderedTick: cross-table receipt sequence was never recorded.
At each recorded second, trades precede quotes as an analysis policy, not a
claim about arrival order. All events and original SQLite values are retained.
Use the context manager to release the read transaction even on early exit.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import heapq
import sqlite3


class RawV1Error(ValueError):
    pass


@dataclass(frozen=True)
class LegacyEvent:
    file: str
    table: str
    rowid: int
    second: int
    code: str
    fields: tuple
    order_quality: str = "second_only"
    venue: str = "unknown"


@dataclass(frozen=True)
class LegacyView:
    event: LegacyEvent
    quote: LegacyEvent | None
    quote_age_seconds: int | None
    quote_status: str
    policy: str = "prior_second_v1"


def _second(value):
    if not isinstance(value, str) or len(value) != 6 or not value.isascii() or not value.isdigit():
        raise RawV1Error("expected recorded HHMMSS text")
    h, m, s = int(value[:2]), int(value[2:4]), int(value[4:])
    if h > 23 or m > 59 or s > 59:
        raise RawV1Error("invalid recorded HHMMSS")
    return h * 3600 + m * 60 + s


def _stream(conn, path, table, columns):
    previous = -1
    # Identifiers are internal constants. No new indexes or source mutations.
    cursor = conn.execute(f"SELECT rowid, {columns} FROM {table} ORDER BY rowid")
    try:
        for row in cursor:
            rowid, clock, code, *values = row
            second = _second(clock)
            if second < previous:
                raise RawV1Error(f"recorded clock reversed: {table} rowid={rowid}; reject this run")
            if not isinstance(code, str) or not code:
                raise RawV1Error(f"missing code: {table} rowid={rowid}")
            previous = second
            yield LegacyEvent(str(path), table, rowid, second, code, (clock, code, *values))
    finally:
        cursor.close()


def _views(trades, quotes, max_age):
    latest = {}
    # Stable merge puts ALL trades before quotes in equal-second buckets.
    # Within each table, SQLite rowid order survives, including identical rows.
    for event in heapq.merge(trades, quotes, key=lambda e: (e.second, e.table == "raw_quotes")):
        if event.table == "raw_quotes":
            latest[event.code] = event
        quote = latest.get(event.code)
        age = event.second - quote.second if quote else None
        status = "missing" if quote is None else "stale" if age > max_age else "observed"
        yield LegacyView(event, quote, age, status)


@contextmanager
def read_raw_v1(path, *, allow_second_only=False, max_quote_age_seconds=1):
    """Stream a caller-finalized, closed input; never run over an active capture.

    Default rejects imprecise data. Explicit opt-in yields a research ordering,
    not accurate fills or venue attribution. Age is a difference of recorded
    second buckets, not an exact elapsed duration. Values are not price-validated.
    A late validation error invalidates the whole run, including emitted prefix.
    """
    if allow_second_only is not True:
        raise RawV1Error("raw v1 has no common sequence; research opt-in required")
    if type(max_quote_age_seconds) is not int or max_quote_age_seconds < 0:
        raise RawV1Error("max_quote_age_seconds must be a nonnegative integer")
    path = Path(path).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
    trades = quotes = views = None
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")  # both cursors share one SQLite read snapshot
        trades = _stream(conn, path, "raw_trades", "t_time,code,price,vol,is_buy")
        quotes = _stream(conn, path, "raw_quotes", "q_time,code,offer_p,offer_v,bid_p,bid_v")
        views = _views(trades, quotes, max_quote_age_seconds)
        yield views
    finally:
        for generator in (views, trades, quotes):
            if generator is not None:
                generator.close()
        conn.close()
