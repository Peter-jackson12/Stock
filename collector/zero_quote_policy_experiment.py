"""Synthetic-only experiment for one-sided zero quote semantics.

This module does not change normalization, raw-v2 qualification, or smoke
eligibility. It recognizes only the exact bounded-evidence pattern observed in
the 2026-09-21 morning prefix:

- signed_magnitude quote normalization,
- exactly one of out_of_range_fid_41 / out_of_range_fid_51,
- same-side top-three raw prices are "-0",
- same-side top-three raw sizes are numeric zero,
- normalized same-side top-of-book is missing/nonpositive,
- opposite top-of-book price and size remain positive.

A recognized record is still NON-EXECUTABLE. The experiment asks only whether
that issue could later be represented as a market-state absence instead of a
dataset-corruption signal. trade_direction_unverified and every mixed/unknown
issue remain disqualifying.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from collector.raw_v2 import CaptureControl
from engine.tick_ordering import OrderedTick


ASK_ISSUE = "out_of_range_fid_41"
BID_ISSUE = "out_of_range_fid_51"
EXPERIMENT = "one_sided_zero_quote_missing_v0"


@dataclass(frozen=True)
class ZeroQuoteDecision:
    accepted: bool
    side: str | None
    issue: str | None
    disposition: str
    reason: str


@dataclass(frozen=True)
class PairedZeroQuoteDecision:
    accepted: bool
    side: str | None
    tick_seq: int | None
    control_seq: int | None
    disposition: str
    reason: str


def _event(envelope):
    if not isinstance(envelope, dict):
        return None
    value = envelope.get("event")
    return value if isinstance(value, OrderedTick) else None


def _raw(envelope):
    if not isinstance(envelope, dict):
        return None
    value = envelope.get("raw_fields")
    return value if isinstance(value, dict) else None


def _issues(raw):
    value = raw.get("issues") if isinstance(raw, dict) else None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _fids(raw):
    value = raw.get("fids") if isinstance(raw, dict) else None
    if not isinstance(value, dict):
        return None
    if any(not isinstance(key, str) or (item is not None and not isinstance(item, str))
           for key, item in value.items()):
        return None
    return value


def _exact_negative_zero(value):
    return isinstance(value, str) and value.strip() == "-0"


def _integer_zero(value):
    if not isinstance(value, str):
        return False
    try:
        return int(value.strip()) == 0
    except (TypeError, ValueError):
        return False


def _positive_number(value):
    if type(value) not in (int, float, str, Decimal):
        return False
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return False
    return number.is_finite() and number > 0


def _positive_int(value):
    return type(value) is int and value > 0


def _zero_depth(levels):
    return (
        isinstance(levels, tuple)
        and len(levels) >= 3
        and all(type(value) is int and value == 0 for value in levels[:3])
    )


def _positive_raw_price(value):
    if not isinstance(value, str):
        return False
    try:
        number = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return False
    return number.is_finite() and abs(number) > 0


def classify_one_sided_zero_quote(envelope) -> ZeroQuoteDecision:
    """Classify only the observed one-sided -0/zero-depth quote pattern."""
    event = _event(envelope)
    raw = _raw(envelope)
    if event is None or raw is None or event.kind != "quote":
        return ZeroQuoteDecision(False, None, None, "disqualifying", "quote event required")
    if raw.get("normalization") != "kiwoom_fids_prototype_1":
        return ZeroQuoteDecision(False, None, None, "disqualifying", "known Kiwoom normalization required")
    if raw.get("price_policy") != "signed_magnitude":
        return ZeroQuoteDecision(False, None, None, "disqualifying", "signed_magnitude policy required")

    issues = _issues(raw)
    fids = _fids(raw)
    if issues is None or fids is None:
        return ZeroQuoteDecision(False, None, None, "disqualifying", "well-formed raw issues/fids required")

    if issues == (ASK_ISSUE,):
        side = "ask"
        price_fids, size_fids, opposite_price_fid = ("41", "42", "43"), ("61", "62", "63"), "51"
        same_price, same_size, same_depth = event.ask, event.ask_size, event.ask_sizes
        opposite_price, opposite_size = event.bid, event.bid_size
    elif issues == (BID_ISSUE,):
        side = "bid"
        price_fids, size_fids, opposite_price_fid = ("51", "52", "53"), ("71", "72", "73"), "41"
        same_price, same_size, same_depth = event.bid, event.bid_size, event.bid_sizes
        opposite_price, opposite_size = event.ask, event.ask_size
    else:
        return ZeroQuoteDecision(
            False, None, None, "disqualifying",
            "only one observed top-of-book zero-price issue is allowed",
        )

    if not all(_exact_negative_zero(fids.get(fid)) for fid in price_fids):
        return ZeroQuoteDecision(False, side, issues[0], "disqualifying", "top-three raw prices are not all -0")
    if not all(_integer_zero(fids.get(fid)) for fid in size_fids):
        return ZeroQuoteDecision(False, side, issues[0], "disqualifying", "top-three raw sizes are not all zero")
    if same_price is not None:
        return ZeroQuoteDecision(False, side, issues[0], "disqualifying", "normalized missing price required")
    if type(same_size) is not int or same_size != 0 or not _zero_depth(same_depth):
        return ZeroQuoteDecision(False, side, issues[0], "disqualifying", "normalized same-side zero depth required")
    if not _positive_raw_price(fids.get(opposite_price_fid)):
        return ZeroQuoteDecision(False, side, issues[0], "disqualifying", "positive opposite raw top price required")
    if not _positive_number(opposite_price) or not _positive_int(opposite_size):
        return ZeroQuoteDecision(False, side, issues[0], "disqualifying", "positive opposite normalized book required")

    return ZeroQuoteDecision(
        True,
        side,
        issues[0],
        "missing_non_executable_quote_candidate",
        "strict one-sided -0 price with zero same-side depth and positive opposite book",
    )


def classify_paired_zero_quote(tick_envelope, control_envelope) -> PairedZeroQuoteDecision:
    """Require an exact mirrored parse_error before treating the pair as a candidate."""
    tick = _event(tick_envelope)
    decision = classify_one_sided_zero_quote(tick_envelope)
    if tick is None or not decision.accepted:
        return PairedZeroQuoteDecision(
            False, decision.side, getattr(tick, "seq", None), None,
            "disqualifying", "tick is not an accepted zero-quote candidate",
        )
    if not isinstance(control_envelope, dict):
        return PairedZeroQuoteDecision(False, decision.side, tick.seq, None, "disqualifying", "control envelope required")
    control = control_envelope.get("event")
    if not isinstance(control, CaptureControl) or control.control_type != "parse_error":
        return PairedZeroQuoteDecision(
            False, decision.side, tick.seq, getattr(control, "seq", None),
            "disqualifying", "paired parse_error control required",
        )

    raw = _raw(tick_envelope)
    issues = list(_issues(raw) or ())
    details = control.details
    paired = (
        control.seq == tick.seq + 1
        and control.received_ns == tick.received_ns
        and details.get("code") == tick.code
        and details.get("issues") == issues
    )
    if not paired:
        return PairedZeroQuoteDecision(
            False, decision.side, tick.seq, control.seq,
            "disqualifying", "parse_error does not exactly mirror the tick issue",
        )
    return PairedZeroQuoteDecision(
        True,
        decision.side,
        tick.seq,
        control.seq,
        "paired_missing_non_executable_quote_candidate",
        "tick/control pair exactly matches the experimental one-sided zero-quote contract",
    )


def experimental_smoke_disposition(envelope) -> str:
    """Return experimental disposition without altering stored data or eligibility."""
    raw = _raw(envelope)
    issues = _issues(raw) if raw is not None else None
    if issues == ():
        return "clean"
    decision = classify_one_sided_zero_quote(envelope)
    if decision.accepted:
        return "quarantine_non_executable_quote"
    return "disqualifying"
