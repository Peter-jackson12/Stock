"""Top-of-book eligibility for simulation, not proof an order would fill.

Policy v1 requires a positive spread and positive integer size on BOTH sides.
Locked/crossed or one-sided quotes are withheld pending a separate market policy.
Original views remain unchanged. No abs(), zero fallback, or trade-price fallback.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class ValidatedBook:
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int


@dataclass(frozen=True)
class QuoteCheck:
    book: ValidatedBook | None
    reason: str
    policy: str = "positive_two_sided_top_v1"


def _positive(value, *, integer=False):
    if type(value) not in (str, int, float, Decimal):
        raise ValueError("missing or unsupported numeric value")
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError("nonpositive or nonfinite value")
    if integer and number != number.to_integral_value():
        raise ValueError("size must be an integer")
    return int(number) if integer else number


def _check(bid, ask, bid_size, ask_size):
    parsed = []
    for name, value, integer in (("bid", bid, False), ("ask", ask, False),
                                 ("bid_size", bid_size, True), ("ask_size", ask_size, True)):
        try:
            parsed.append(_positive(value, integer=integer))
        except (ValueError, InvalidOperation, OverflowError):
            return QuoteCheck(None, "invalid_" + name)
    if parsed[0] >= parsed[1]:
        return QuoteCheck(None, "locked_or_crossed")
    return QuoteCheck(ValidatedBook(*parsed), "eligible")


def _availability(view):
    if view.quote is None or view.quote_status == "missing":
        return QuoteCheck(None, "missing")
    if view.quote_status != "observed":
        return QuoteCheck(None, "stale" if view.quote_status == "stale" else "unknown_quote_status")
    return None


def check_ordered_quote(view):
    """Use a TickView produced by ReceiveOrderReplay (including its age policy)."""
    unavailable = _availability(view)
    if unavailable:
        return unavailable
    q = view.quote
    return _check(q.bid, q.ask, q.bid_size, q.ask_size)


def check_legacy_quote(view):
    """Use a LegacyView; eligibility does NOT upgrade second-only order quality.

    Only the first level is validated: this grants no permission to sweep deeper
    levels. Each original ladder is retained on the view for later validation.
    """
    unavailable = _availability(view)
    if unavailable:
        return unavailable
    if view.quote.table != "raw_quotes" or len(view.quote.fields) != 6:
        return QuoteCheck(None, "invalid_quote_record")
    _, _, ask, ask_size, bid, bid_size = view.quote.fields
    def first(value):
        return value.split(",", 1)[0] if isinstance(value, str) else None
    return _check(first(bid), first(ask), first(bid_size), first(ask_size))
