"""Execution-depth cache를 trade decision point의 causal entry gate로 join한다."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping

from engine.tick_ordering import OrderedTick
from research.fast_backtest.execution_depth import (
    ExecutionDepthRecord,
    MAX_QUOTE_AGE_NS,
    THRESHOLD_KRW,
)


@dataclass(frozen=True)
class EntryFeasibilityState:
    seq: int
    received_ns: int
    status: str
    reason: str
    quote_seq: int | None
    quote_received_ns: int | None
    ask_depth_notional_10: int | None

    def to_dict(self) -> dict:
        return asdict(self)


def _quote_state(record: ExecutionDepthRecord) -> tuple[str, str]:
    if record.top_status != "VALID":
        return "UNKNOWN", record.top_reason or "invalid_top"
    if record.ask_status != "COMPLETE":
        return "UNKNOWN", record.ask_reason or "incomplete_ask_depth"
    if record.ask_depth_notional_10 is None:
        return "UNKNOWN", "missing_ask_depth_notional"
    if record.ask_depth_notional_10 >= THRESHOLD_KRW:
        return "PASS", "ask10_notional_gte_threshold"
    return "FAIL", "ask10_notional_below_threshold"


def join_entry_feasibility(
    events: Iterable[OrderedTick],
    depth_records: Iterable[ExecutionDepthRecord],
    *,
    max_quote_age_ns: int = MAX_QUOTE_AGE_NS,
) -> dict[int, EntryFeasibilityState]:
    """각 trade에 현재까지 관측된 최신 quote depth 상태를 붙인다.

    invalid/partial 새 quote도 최신 상태를 대체한다. 미래 quote backfill은 허용하지 않는다.
    """
    if type(max_quote_age_ns) is not int or max_quote_age_ns < 0:
        raise ValueError("max_quote_age_ns must be nonnegative")
    depths = iter(depth_records)
    next_depth = next(depths, None)
    latest: ExecutionDepthRecord | None = None
    states: dict[int, EntryFeasibilityState] = {}
    last_seq = 0
    identity: tuple[str, str, str, str] | None = None

    for event in events:
        if event.seq <= last_seq:
            raise ValueError("events must preserve receive order")
        last_seq = event.seq
        current = (event.source, event.session_id, event.code, event.venue)
        if identity is None:
            identity = current
        elif current != identity:
            raise ValueError("feasibility join requires one source/session/code/venue cell")

        if event.kind == "quote":
            if next_depth is None or next_depth.seq != event.seq:
                raise ValueError("every quote event requires an exact execution-depth row")
            if (
                next_depth.source,
                next_depth.session_id,
                next_depth.code,
                next_depth.venue,
                next_depth.received_ns,
            ) != (
                event.source,
                event.session_id,
                event.code,
                event.venue,
                event.received_ns,
            ):
                raise ValueError("execution-depth row identity mismatch")
            latest = next_depth
            next_depth = next(depths, None)
            continue

        if latest is None:
            status, reason = "UNKNOWN", "missing_quote"
        elif event.received_ns - latest.received_ns > max_quote_age_ns:
            status, reason = "UNKNOWN", "stale_quote"
        else:
            status, reason = _quote_state(latest)
        states[event.seq] = EntryFeasibilityState(
            seq=event.seq,
            received_ns=event.received_ns,
            status=status,
            reason=reason,
            quote_seq=latest.seq if latest else None,
            quote_received_ns=latest.received_ns if latest else None,
            ask_depth_notional_10=latest.ask_depth_notional_10 if latest else None,
        )

    if next_depth is not None:
        raise ValueError("execution-depth rows contain a quote absent from event input")
    if not states:
        raise ValueError("feasibility join requires at least one trade decision point")
    return states


def feasibility_summary(states: Mapping[int, EntryFeasibilityState]) -> dict:
    counts = Counter()
    reasons = Counter()
    for seq, state in states.items():
        if seq != state.seq or state.status not in {"PASS", "FAIL", "UNKNOWN"}:
            raise ValueError("invalid feasibility state")
        counts[state.status] += 1
        reasons[state.reason] += 1
    evaluated = sum(counts.values())
    return {
        "evaluated": evaluated,
        "PASS": counts["PASS"],
        "FAIL": counts["FAIL"],
        "UNKNOWN": counts["UNKNOWN"],
        "pass_rate": counts["PASS"] / evaluated if evaluated else None,
        "reasons": dict(sorted(reasons.items())),
    }
