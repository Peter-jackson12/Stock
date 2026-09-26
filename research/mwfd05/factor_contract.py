"""MWFD-05 frozen factor contract F01–F07 (schema binding freeze).

Reference implementations fix the semantics before any factor is materialized on
MWFD-04 data. Stage 1 materialization must call these functions (or be proven
equivalent to them) and must not change a formula, endpoint, freshness, missing or
support rule after seeing outcomes.

Decision point d = one trade row of a cell stream (seq s_d, receipt time t_d = received_ns).
Every trade row is an evaluation point (Module A cell summaries and Module C joins);
this does not create candidate risk state, so Module B stays BLOCKED.

Causal cutoff for every factor: only events with seq < s_d. The decision trade row itself
is EXCLUDED from all windows (pure pre-decision state). Clock = collector receipt
received_ns (monotonic ns), not exchange time.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math
from typing import Mapping, Sequence

CONTRACT_VERSION = "mwfd_05_factor_contract_v1"
WINDOW_NS = 30_000_000_000
GRID_STEP_NS = 1_000_000_000
GRID_POINTS = 31                     # t_d, t_d−1s, ..., t_d−30s → 30 log returns
MAX_QUOTE_AGE_NS = 2_000_000_000     # same freshness as the MWFD-04 execution gate
REGULAR_SESSION_START_SECOND = 32400 # 09:00:00 market second; earlier quotes are pre-open auction
MAX_UNKNOWN_DIRECTION_VOLUME_SHARE = 0.5
BPS = 10_000.0

# missing reasons (a factor value is either a finite number or exactly one of these)
MISSING_NO_QUOTE = "NO_QUOTE_BEFORE_DECISION"
MISSING_STALE_QUOTE = "STALE_QUOTE"
MISSING_INVALID_QUOTE = "INVALID_QUOTE"
MISSING_INCOMPLETE_DEPTH = "INCOMPLETE_DEPTH"
MISSING_ZERO_DEPTH = "ZERO_TOTAL_DEPTH"
MISSING_TRUNCATED_WINDOW = "TRUNCATED_WINDOW"
MISSING_PRE_OPEN_REFERENCE = "PRE_OPEN_REFERENCE"
MISSING_NO_KNOWN_DIRECTION = "NO_KNOWN_DIRECTION_VOLUME"
MISSING_UNKNOWN_DIRECTION_SHARE = "UNKNOWN_DIRECTION_SHARE_EXCEEDED"


@dataclass(frozen=True)
class FactorValue:
    value: float | None
    missing: str | None = None

    def __post_init__(self):
        if (self.value is None) == (self.missing is None):
            raise ValueError("exactly one of value/missing")
        if self.value is not None and not math.isfinite(self.value):
            raise ValueError("factor value must be finite")


def _missing(reason: str) -> FactorValue:
    return FactorValue(None, reason)


# ── F01 / F03: 10-level depth from the latest execution-depth quote (seq < s_d) ──

def _depth_quote(depth: Mapping | None, decision_ns: int) -> FactorValue | None:
    if depth is None:
        return _missing(MISSING_NO_QUOTE)
    if decision_ns - depth["received_ns"] > MAX_QUOTE_AGE_NS:
        return _missing(MISSING_STALE_QUOTE)
    return None


def f01_ask_depth_notional_10(depth: Mapping | None, decision_ns: int) -> FactorValue:
    """Σ_{i=1..10} ask_price_i × ask_size_i (KRW); ask side must be COMPLETE."""
    failure = _depth_quote(depth, decision_ns)
    if failure:
        return failure
    if depth["ask_status"] != "COMPLETE" or depth["ask_depth_notional_10"] is None:
        return _missing(MISSING_INCOMPLETE_DEPTH)
    return FactorValue(float(depth["ask_depth_notional_10"]))


def f03_depth_imbalance_10(depth: Mapping | None, decision_ns: int) -> FactorValue:
    """(B − A) / (B + A), B/A = 10-level bid/ask depth notional; both sides COMPLETE."""
    failure = _depth_quote(depth, decision_ns)
    if failure:
        return failure
    if (depth["ask_status"] != "COMPLETE" or depth["bid_status"] != "COMPLETE"
            or depth["ask_depth_notional_10"] is None or depth["bid_depth_notional_10"] is None):
        return _missing(MISSING_INCOMPLETE_DEPTH)
    bid, ask = float(depth["bid_depth_notional_10"]), float(depth["ask_depth_notional_10"])
    if bid + ask <= 0:
        return _missing(MISSING_ZERO_DEPTH)
    return FactorValue((bid - ask) / (bid + ask))


# ── F02: relative spread on the validated top of book in force at the decision ──

def _top(quote: Mapping | None, decision_ns: int):
    if quote is None:
        return None, _missing(MISSING_NO_QUOTE)
    if decision_ns - quote["received_ns"] > MAX_QUOTE_AGE_NS:
        return None, _missing(MISSING_STALE_QUOTE)
    bid, ask = quote.get("bid"), quote.get("ask")
    if not quote.get("quote_eligible") or bid is None or ask is None or not 0 < bid < ask:
        return None, _missing(MISSING_INVALID_QUOTE)
    return (float(bid), float(ask)), None


def f02_relative_spread_bps(quote: Mapping | None, decision_ns: int) -> FactorValue:
    """(ask − bid) / ((ask + bid) / 2) × 10,000 on the latest quote row with seq < s_d, age ≤ 2 s."""
    top, failure = _top(quote, decision_ns)
    if failure:
        return failure
    bid, ask = top
    return FactorValue((ask - bid) / ((ask + bid) / 2.0) * BPS)


# ── F04 / F05: trades in [t_d − 30 s, decision) — start inclusive, decision row excluded ──

def window_trades(trades: Sequence[Mapping], decision_seq: int, decision_ns: int) -> list[Mapping]:
    start = decision_ns - WINDOW_NS
    return [t for t in trades if t["seq"] < decision_seq and start <= t["received_ns"] <= decision_ns]


def _window_observed(stream_start_ns: int, decision_ns: int) -> bool:
    return decision_ns - WINDOW_NS >= stream_start_ns


def f04_buy_sell_imbalance_30s(trades: Sequence[Mapping], decision_seq: int, decision_ns: int,
                               stream_start_ns: int) -> FactorValue:
    """(buy_vol − sell_vol) / (buy_vol + sell_vol); unknown-direction volume excluded from both."""
    if not _window_observed(stream_start_ns, decision_ns):
        return _missing(MISSING_TRUNCATED_WINDOW)
    rows = window_trades(trades, decision_seq, decision_ns)
    buy = sum(t["trade_volume"] for t in rows if t["is_buy"] is True)
    sell = sum(t["trade_volume"] for t in rows if t["is_buy"] is False)
    unknown = sum(t["trade_volume"] for t in rows if t["is_buy"] is None)
    total = buy + sell + unknown
    if buy + sell == 0:
        return _missing(MISSING_NO_KNOWN_DIRECTION)
    if unknown > MAX_UNKNOWN_DIRECTION_VOLUME_SHARE * total:
        return _missing(MISSING_UNKNOWN_DIRECTION_SHARE)
    return FactorValue((buy - sell) / (buy + sell))


def f05_traded_notional_30s(trades: Sequence[Mapping], decision_seq: int, decision_ns: int,
                            stream_start_ns: int) -> FactorValue:
    """Σ price × volume (KRW) over the same window, all directions; 0 is a valid value."""
    if not _window_observed(stream_start_ns, decision_ns):
        return _missing(MISSING_TRUNCATED_WINDOW)
    rows = window_trades(trades, decision_seq, decision_ns)
    return FactorValue(float(sum(t["trade_price"] * t["trade_volume"] for t in rows)))


# ── F06 / F07: midpoint series from quote rows only ──

class MidpointSeries:
    """M(t) = midpoint of the latest quote row with received_ns ≤ t (and seq < s_d).

    Defined only when that quote row is itself valid (0 < bid < ask) and in the regular
    session (market_second ≥ 09:00:00). An invalid latest quote is NOT skipped back to an
    older valid one. Between quote events the book is carried forward (no update = unchanged).
    """

    def __init__(self, quotes: Sequence[Mapping]):
        self.quotes = sorted(quotes, key=lambda q: q["seq"])
        self.times = [q["received_ns"] for q in self.quotes]

    def latest(self, t_ns: int, decision_seq: int) -> Mapping | None:
        index = bisect_right(self.times, t_ns)
        while index > 0 and self.quotes[index - 1]["seq"] >= decision_seq:
            index -= 1
        return self.quotes[index - 1] if index else None

    def mid(self, t_ns: int, decision_seq: int):
        quote = self.latest(t_ns, decision_seq)
        if quote is None:
            return None, MISSING_NO_QUOTE
        bid, ask = quote.get("bid"), quote.get("ask")
        if not quote.get("quote_eligible") or bid is None or ask is None or not 0 < bid < ask:
            return None, MISSING_INVALID_QUOTE
        if quote["market_second"] < REGULAR_SESSION_START_SECOND:
            return None, MISSING_PRE_OPEN_REFERENCE
        return (float(bid) + float(ask)) / 2.0, quote


def _decision_mid(series: MidpointSeries, decision_seq: int, decision_ns: int):
    mid, quote = series.mid(decision_ns, decision_seq)
    if mid is None:
        return None, quote
    if decision_ns - quote["received_ns"] > MAX_QUOTE_AGE_NS:
        return None, MISSING_STALE_QUOTE
    return mid, None


def f06_midpoint_return_30s_bps(series: MidpointSeries, decision_seq: int, decision_ns: int,
                                stream_start_ns: int) -> FactorValue:
    """(M(t_d) / M(t_d − 30 s) − 1) × 10,000; M(t_d) must be fresh (≤ 2 s), reference carried forward."""
    if not _window_observed(stream_start_ns, decision_ns):
        return _missing(MISSING_TRUNCATED_WINDOW)
    now, failure = _decision_mid(series, decision_seq, decision_ns)
    if failure:
        return _missing(failure)
    ref, info = series.mid(decision_ns - WINDOW_NS, decision_seq)
    if ref is None:
        return _missing(info)
    return FactorValue((now / ref - 1.0) * BPS)


def f07_midpoint_realized_vol_30s_bps(series: MidpointSeries, decision_seq: int, decision_ns: int,
                                      stream_start_ns: int) -> FactorValue:
    """sqrt(Σ_{k=1..30} r_k²) × 10,000, r_k = ln(M(t_d − (k−1)s) / M(t_d − k s)); not annualized.

    All 31 grid midpoints must be defined; M(t_d) must be fresh. No updates → 0 (valid).
    """
    if not _window_observed(stream_start_ns, decision_ns):
        return _missing(MISSING_TRUNCATED_WINDOW)
    now, failure = _decision_mid(series, decision_seq, decision_ns)
    if failure:
        return _missing(failure)
    mids = [now]
    for k in range(1, GRID_POINTS):
        mid, info = series.mid(decision_ns - k * GRID_STEP_NS, decision_seq)
        if mid is None:
            return _missing(info)
        mids.append(mid)
    squares = sum(math.log(mids[k - 1] / mids[k]) ** 2 for k in range(1, GRID_POINTS))
    return FactorValue(math.sqrt(squares) * BPS)


FACTOR_SPEC = {
    "F01": {"name": "ask_depth_notional_10_krw", "formula": "sum_{i=1..10} ask_price_i * ask_size_i",
            "source": "MWFD-02 execution_depth quote with max seq < s_d (gate_cache quote_seq)", "unit": "KRW",
            "freshness": "t_d - quote_received_ns <= 2 s", "missing": [MISSING_NO_QUOTE, MISSING_STALE_QUOTE, MISSING_INCOMPLETE_DEPTH],
            "stage0_status": "SUPPORTED_DIRECT",
            "selection_note": "at signaled decisions (Module C) values are >= 100,000,000 KRW by gate construction"},
    "F02": {"name": "relative_spread_bps", "formula": "(ask - bid) / ((ask + bid) / 2) * 10000",
            "source": "latest quote row with seq < s_d (feature cache bid/ask; validated book)", "unit": "bps",
            "freshness": "<= 2 s", "missing": [MISSING_NO_QUOTE, MISSING_STALE_QUOTE, MISSING_INVALID_QUOTE],
            "note": "existing spread_pct uses bid denominator and is NOT F02"},
    "F03": {"name": "depth_imbalance_10", "formula": "(B10 - A10) / (B10 + A10), notional",
            "source": "same depth quote as F01; bid and ask COMPLETE", "unit": "ratio [-1, 1]",
            "freshness": "<= 2 s", "missing": [MISSING_NO_QUOTE, MISSING_STALE_QUOTE, MISSING_INCOMPLETE_DEPTH, MISSING_ZERO_DEPTH],
            "note": "existing obi_ratio = bid3/ask3 size ratio is NOT F03"},
    "F04": {"name": "buy_sell_imbalance_30s", "formula": "(buy_vol - sell_vol) / (buy_vol + sell_vol)",
            "window": "trades with seq < s_d and received_ns in [t_d - 30 s, t_d]; decision trade excluded",
            "unknown_side": "excluded from numerator and denominator; missing if unknown volume > 50% of window volume",
            "minimum_support": "buy_vol + sell_vol > 0; full 30 s window observed in the cell stream",
            "missing": [MISSING_TRUNCATED_WINDOW, MISSING_NO_KNOWN_DIRECTION, MISSING_UNKNOWN_DIRECTION_SHARE],
            "note": "existing buy_ratio_by_ticks['15'] is a 15-trade window and is NOT F04"},
    "F05": {"name": "traded_notional_30s_krw", "formula": "sum price * volume", "window": "same as F04, all directions",
            "minimum_support": "full 30 s window observed; zero trades -> 0 KRW (valid)", "unit": "KRW",
            "missing": [MISSING_TRUNCATED_WINDOW], "note": "existing recent_volume_by_ticks['15'] is NOT F05"},
    "F06": {"name": "midpoint_return_30s_bps", "formula": "(M(t_d) / M(t_d - 30 s) - 1) * 10000 (simple return)",
            "midpoint": "M(t) from latest quote row with received_ns <= t and seq < s_d; valid book; market_second >= 09:00:00; carried forward between quote events",
            "freshness": "M(t_d) quote age <= 2 s; reference carried forward",
            "missing": [MISSING_TRUNCATED_WINDOW, MISSING_NO_QUOTE, MISSING_INVALID_QUOTE, MISSING_STALE_QUOTE, MISSING_PRE_OPEN_REFERENCE],
            "note": "prior_high / breakout_margin_pct is NOT F06"},
    "F07": {"name": "midpoint_realized_vol_30s_bps", "formula": "sqrt(sum_{k=1..30} ln(M(t_d-(k-1)s)/M(t_d-ks))^2) * 10000, not annualized",
            "grid": "31 points at 1 s spacing ending at t_d; midpoint carried forward", "minimum_support": "all 31 grid midpoints defined; no quote change -> 0 (valid)",
            "freshness": "M(t_d) quote age <= 2 s", "missing": [MISSING_TRUNCATED_WINDOW, MISSING_NO_QUOTE, MISSING_INVALID_QUOTE, MISSING_STALE_QUOTE, MISSING_PRE_OPEN_REFERENCE]},
}
