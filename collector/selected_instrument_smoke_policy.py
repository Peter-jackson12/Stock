"""Selected-instrument smoke-quality policy evaluator.

Synthetic/pure policy layer only. It does not read SQLite, change strict prefix
qualification, or authorize NXT smoke.

The intended use is a single-strategy pipeline smoke where only explicitly
selected CODE=VENUE ticks reach the strategy. The whole ordered stream is still
observed for structural/control quality.

Policy v2:
- selected clean ticks: allowed,
- selected exact one-sided zero-quote + mirrored parse_error pair: quarantined,
- selected trade_direction_unverified is disqualifying by default,
- only explicit unknown-direction quarantine mode may quarantine the observed
  unsigned-FID15 raw shape,
- unselected normalized-issue ticks: ignored for selected-strategy quality only
  when immediately followed by an exact mirrored parse_error,
- any unpaired/mismatched issue or unsafe control: globally disqualifying.

This never upgrades whole-prefix research quality and never grants execution
permission to one-sided quotes.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation

from collector.raw_v2 import CaptureControl
from collector.zero_quote_policy_experiment import (
    ASK_ISSUE,
    BID_ISSUE,
    classify_one_sided_zero_quote,
    classify_paired_zero_quote,
)
from engine.tick_ordering import OrderedTick
from strategies.nxt_breakout.direction_window import (
    POLICY as QUARANTINE_UNKNOWN_DIRECTION_POLICY,
)

STRICT_UNKNOWN_DIRECTION_POLICY = "strict"
DIRECTION_ISSUE = "trade_direction_unverified"


POLICY = "selected_instrument_smoke_quality_v2"
SAFE_CONTROL_TYPES = {"session_start", "session_note"}
MAX_SELECTED_DISQUALIFYING_EXAMPLES = 10
EXAMPLE_FIDS = ("10", "14", "15", "20", "21", "27", "28", "41", "51")
IGNORABLE_UNSELECTED_ISSUES = {
    ASK_ISSUE,
    BID_ISSUE,
    "trade_direction_unverified",
}


def _tick_issues(envelope):
    raw = envelope.get("raw_fields") if isinstance(envelope, dict) else None
    value = raw.get("issues") if isinstance(raw, dict) else None
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        return None
    return tuple(value)


def _control_issues(event):
    details = event.details if isinstance(event.details, dict) else {}
    value = details.get("issues")
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        return None
    return tuple(value)


def _validate_unknown_direction_policy(value):
    if value not in (
        STRICT_UNKNOWN_DIRECTION_POLICY,
        QUARANTINE_UNKNOWN_DIRECTION_POLICY,
    ):
        raise ValueError("unknown selected unknown-direction policy")
    return value


def _selected_direction_quarantine_candidate(envelope):
    event = envelope.get("event") if isinstance(envelope, dict) else None
    raw = envelope.get("raw_fields") if isinstance(envelope, dict) else None
    issues = _tick_issues(envelope)
    if (
        not isinstance(event, OrderedTick)
        or event.kind != "trade"
        or issues != (DIRECTION_ISSUE,)
        or event.is_buy is not None
        or type(event.volume) is not int
        or event.volume <= 0
        or not isinstance(raw, dict)
        or raw.get("normalization") != "kiwoom_fids_prototype_1"
        or raw.get("real_type") != "주식체결"
        or raw.get("price_policy") != "signed_magnitude"
        or raw.get("direction_policy") != "signed_volume"
    ):
        return False
    try:
        price = Decimal(str(event.price))
    except (InvalidOperation, ValueError):
        return False
    if not price.is_finite() or price <= 0:
        return False
    fids = raw.get("fids")
    if not isinstance(fids, dict):
        return False
    raw_volume = fids.get("15")
    if not isinstance(raw_volume, str):
        return False
    text = raw_volume.strip()
    if not text or text[0] in "+-":
        return False
    try:
        parsed = int(text)
    except ValueError:
        return False
    return parsed > 0 and parsed == event.volume


def _exact_mirrored_pair(tick_envelope, control):
    tick = tick_envelope.get("event") if isinstance(tick_envelope, dict) else None
    issues = _tick_issues(tick_envelope)
    if (
        not isinstance(tick, OrderedTick)
        or issues is None
        or not issues
        or not isinstance(control, CaptureControl)
        or control.control_type != "parse_error"
    ):
        return False
    control_issues = _control_issues(control)
    details = control.details if isinstance(control.details, dict) else {}
    return (
        control.seq == tick.seq + 1
        and control.received_ns == tick.received_ns
        and details.get("code") == tick.code
        and control_issues == issues
    )


class SelectedInstrumentSmokePolicy:
    """Fail-closed sequential evaluator over the complete ordered prefix stream."""

    def __init__(self, instruments, *, unknown_direction_policy=STRICT_UNKNOWN_DIRECTION_POLICY):
        if (
            not isinstance(instruments, dict)
            or not instruments
            or any(
                not isinstance(code, str)
                or not code.strip()
                or not isinstance(venue, str)
                or not venue.strip()
                for code, venue in instruments.items()
            )
        ):
            raise ValueError("nonempty CODE=VENUE mapping required")
        self.instruments = dict(instruments)
        self.unknown_direction_policy = _validate_unknown_direction_policy(
            unknown_direction_policy
        )
        self.expected_seq = 1
        self.last_received_ns = 0
        self.identity = None

        self.raw_records = 0
        self.tick_records = 0
        self.control_records = 0
        self.selected_tick_records = 0
        self.unselected_tick_records = 0
        self.selected_clean_ticks = 0
        self.unselected_clean_ticks = 0
        self.safe_controls = 0

        self.selected_zero_quote_pairs = 0
        self.selected_zero_quote_by_side = Counter()
        self.selected_unknown_direction_pairs = 0
        self.unselected_issue_pairs_ignored = 0
        self.unselected_ignored_issues = Counter()

        self.selected_disqualifying_pairs = 0
        self.selected_disqualifying_issues = Counter()
        self.selected_disqualifying_examples = []
        self.unselected_unapproved_issue_pairs = 0
        self.unpaired_issue_ticks = 0
        self.unsafe_controls = 0
        self.global_disqualifying_issues = Counter()

        self._pending = None

    def _selected(self, event):
        return self.instruments.get(event.code) == event.venue

    def _validate_order(self, event):
        if event.seq != self.expected_seq:
            raise ValueError("contiguous full-stream sequence required")
        if event.received_ns < self.last_received_ns:
            raise ValueError("nondecreasing received_ns required")
        identity = (event.source, event.session_id)
        if self.identity is None:
            self.identity = identity
        elif identity != self.identity:
            raise ValueError("single source/session identity required")
        self.expected_seq += 1
        self.last_received_ns = event.received_ns

    def _record_selected_disqualifying_example(self, envelope, issues, *, control=None, reason):
        if len(self.selected_disqualifying_examples) >= MAX_SELECTED_DISQUALIFYING_EXAMPLES:
            return
        event = envelope["event"]
        raw = envelope.get("raw_fields") if isinstance(envelope, dict) else None
        fids = raw.get("fids") if isinstance(raw, dict) else None
        raw_fids = (
            {key: fids[key] for key in EXAMPLE_FIDS if key in fids}
            if isinstance(fids, dict) else {}
        )
        normalized_names = (
            ("price", "volume", "is_buy")
            if event.kind == "trade"
            else ("bid", "ask", "bid_size", "ask_size")
        )
        normalized = {
            name: getattr(event, name)
            for name in normalized_names
        }
        self.selected_disqualifying_examples.append({
            "tick_seq": event.seq,
            "control_seq": control.seq if isinstance(control, CaptureControl) else None,
            "received_ns": event.received_ns,
            "received_at_utc": envelope.get("received_at_utc"),
            "exchange_ts_raw": envelope.get("exchange_ts_raw"),
            "code": event.code,
            "venue": event.venue,
            "kind": event.kind,
            "market_second": event.market_second,
            "issues": list(issues),
            "reason": reason,
            "raw_fids": raw_fids,
            "normalized": normalized,
            "paired_parse_error": isinstance(control, CaptureControl),
        })

    def _reject_pending(self, reason):
        if self._pending is None:
            return
        envelope, selected, issues = self._pending
        self.unpaired_issue_ticks += 1
        self.global_disqualifying_issues[reason] += 1
        for issue in issues:
            self.global_disqualifying_issues[issue] += 1
        if selected:
            self._record_selected_disqualifying_example(
                envelope, issues, reason=reason
            )
            for issue in issues:
                self.selected_disqualifying_issues[issue] += 1
        self._pending = None

    def _accept_clean_tick(self, event):
        if self._selected(event):
            self.selected_tick_records += 1
            self.selected_clean_ticks += 1
        else:
            self.unselected_tick_records += 1
            self.unselected_clean_ticks += 1

    def _start_issue_tick(self, envelope, event, issues):
        selected = self._selected(event)
        if selected:
            self.selected_tick_records += 1
        else:
            self.unselected_tick_records += 1
        self._pending = (envelope, selected, issues)

    def _consume_pair(self, control):
        envelope, selected, issues = self._pending
        event = envelope["event"]
        if not _exact_mirrored_pair(envelope, control):
            return False

        self.control_records += 1
        if selected:
            zero = classify_one_sided_zero_quote(envelope)
            paired_zero = classify_paired_zero_quote(
                envelope,
                {
                    "event": control,
                    "received_at_utc": "",
                    "raw_fields": {},
                    "exchange_ts_raw": None,
                    "source_time_precision": "unknown",
                },
            )
            if zero.accepted and paired_zero.accepted:
                self.selected_zero_quote_pairs += 1
                self.selected_zero_quote_by_side[zero.side] += 1
            elif (
                self.unknown_direction_policy == QUARANTINE_UNKNOWN_DIRECTION_POLICY
                and _selected_direction_quarantine_candidate(envelope)
            ):
                self.selected_unknown_direction_pairs += 1
            else:
                self.selected_disqualifying_pairs += 1
                self._record_selected_disqualifying_example(
                    envelope,
                    issues,
                    control=control,
                    reason="selected_issue_pair_disqualifying",
                )
                for issue in issues:
                    self.selected_disqualifying_issues[issue] += 1
        else:
            if set(issues) <= IGNORABLE_UNSELECTED_ISSUES:
                self.unselected_issue_pairs_ignored += 1
                for issue in issues:
                    self.unselected_ignored_issues[issue] += 1
            else:
                self.unselected_unapproved_issue_pairs += 1
                for issue in issues:
                    self.global_disqualifying_issues[
                        "unselected_unapproved_issue:" + issue
                    ] += 1

        self._pending = None
        return True

    def _accept_control(self, event):
        self.control_records += 1
        if event.control_type in SAFE_CONTROL_TYPES:
            self.safe_controls += 1
            return
        self.unsafe_controls += 1
        if event.control_type == "parse_error":
            issues = _control_issues(event)
            if not issues:
                self.global_disqualifying_issues["unspecified_parse_error"] += 1
            else:
                for issue in issues:
                    self.global_disqualifying_issues[issue] += 1
        else:
            self.global_disqualifying_issues[f"control:{event.control_type}"] += 1

    def accept(self, envelope):
        if not isinstance(envelope, dict):
            raise TypeError("envelope object required")
        event = envelope.get("event")
        if not isinstance(event, (OrderedTick, CaptureControl)):
            raise TypeError("OrderedTick or CaptureControl required")

        self._validate_order(event)
        self.raw_records += 1

        if self._pending is not None:
            if isinstance(event, CaptureControl) and self._consume_pair(event):
                return
            self._reject_pending("issue_tick_without_exact_mirrored_parse_error")

        if isinstance(event, CaptureControl):
            self._accept_control(event)
            return

        self.tick_records += 1
        issues = _tick_issues(envelope)
        if issues is None:
            selected = self._selected(event)
            if selected:
                self.selected_tick_records += 1
                self.selected_disqualifying_issues["malformed_normalized_issues"] += 1
            else:
                self.unselected_tick_records += 1
            self.global_disqualifying_issues["malformed_normalized_issues"] += 1
            return
        if not issues:
            self._accept_clean_tick(event)
            return
        self._start_issue_tick(envelope, event, issues)

    def result(self):
        self._reject_pending("issue_tick_without_exact_mirrored_parse_error")
        eligible = not (
            self.selected_disqualifying_pairs
            or self.selected_disqualifying_issues
            or self.unselected_unapproved_issue_pairs
            or self.unpaired_issue_ticks
            or self.unsafe_controls
            or self.global_disqualifying_issues
        )
        return {
            "policy": POLICY,
            "selected_instruments": dict(sorted(self.instruments.items())),
            "unknown_direction_policy": self.unknown_direction_policy,
            "selected_smoke_quality_eligible": eligible,
            "selected_input_present": self.selected_tick_records > 0,
            "counts": {
                "raw_records": self.raw_records,
                "tick_records": self.tick_records,
                "control_records": self.control_records,
                "selected_tick_records": self.selected_tick_records,
                "unselected_tick_records": self.unselected_tick_records,
                "selected_clean_ticks": self.selected_clean_ticks,
                "unselected_clean_ticks": self.unselected_clean_ticks,
                "safe_controls": self.safe_controls,
            },
            "quarantine": {
                "selected_zero_quote_pairs": self.selected_zero_quote_pairs,
                "selected_zero_quote_by_side": dict(sorted(self.selected_zero_quote_by_side.items())),
                "selected_unknown_direction_pairs": self.selected_unknown_direction_pairs,
                "unselected_issue_pairs_ignored": self.unselected_issue_pairs_ignored,
                "unselected_ignored_issue_counts": dict(sorted(self.unselected_ignored_issues.items())),
                "zero_quote_execution_permission_granted": False,
                "unknown_direction_immediate_entry_permission_granted": False,
            },
            "disqualifying": {
                "selected_issue_pairs": self.selected_disqualifying_pairs,
                "selected_issue_counts": dict(sorted(self.selected_disqualifying_issues.items())),
                "selected_issue_examples": list(self.selected_disqualifying_examples),
                "selected_issue_example_limit": MAX_SELECTED_DISQUALIFYING_EXAMPLES,
                "unselected_unapproved_issue_pairs": self.unselected_unapproved_issue_pairs,
                "unpaired_issue_ticks": self.unpaired_issue_ticks,
                "unsafe_controls": self.unsafe_controls,
                "global_issue_counts": dict(sorted(self.global_disqualifying_issues.items())),
            },
            "contracts": {
                "whole_prefix_research_quality_upgraded": False,
                "strict_prefix_qualification_unchanged": True,
                "nxt_smoke_gate_unchanged": True,
                "unselected_issues_require_exact_mirrored_pair": True,
                "unselected_ignored_issues_are_whitelisted": True,
                "selected_zero_quote_is_non_executable_quarantine_only": True,
                "selected_unknown_direction_default_strict": True,
                "selected_unknown_direction_requires_explicit_policy": True,
                "selected_unknown_direction_requires_unsigned_fid15": True,
                "selected_unknown_direction_requires_observed_kiwoom_trade_shape": True,
                "selected_unknown_direction_requires_strategy_window_quarantine": True,
                "selected_trade_direction_unverified_is_disqualifying": (
                    self.unknown_direction_policy == STRICT_UNKNOWN_DIRECTION_POLICY
                ),
                "unsafe_controls_are_global": True,
            },
        }
