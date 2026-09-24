"""Opt-in smoke-quality policy evaluator for synthetic validation only.

This module does not read raw files and is not wired into prefix qualification
or NXT smoke. It evaluates an already ordered envelope stream and quarantines
only an exact pair consisting of:

1. one observed one-sided zero-quote candidate, and
2. the immediately following mirrored parse_error control.

Everything else remains fail-closed. In particular, trade_direction_unverified
is disqualifying. Quarantined quotes remain non-executable under the existing
quote-validation contract.
"""
from __future__ import annotations

from collections import Counter

from collector.raw_v2 import CaptureControl
from collector.zero_quote_policy_experiment import (
    ASK_ISSUE,
    BID_ISSUE,
    classify_one_sided_zero_quote,
    classify_paired_zero_quote,
)
from engine.tick_ordering import OrderedTick


POLICY = "one_sided_zero_quote_quarantine_v0"
SAFE_CONTROL_TYPES = {"session_start", "session_note"}


def _issues_from_tick(envelope):
    raw = envelope.get("raw_fields") if isinstance(envelope, dict) else None
    value = raw.get("issues") if isinstance(raw, dict) else None
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        return ("malformed_normalized_issues",)
    return tuple(value)


def _issues_from_control(event):
    details = event.details if isinstance(event.details, dict) else {}
    value = details.get("issues")
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        return ("unspecified_parse_error",)
    return tuple(value) or ("unspecified_parse_error",)


class ZeroQuoteSmokePolicy:
    """Sequential fail-closed evaluator for the opt-in smoke-quality hypothesis."""

    def __init__(self):
        self.raw_records = 0
        self.tick_records = 0
        self.control_records = 0
        self.clean_tick_records = 0
        self.safe_control_records = 0
        self.zero_quote_candidate_ticks = 0
        self.quarantined_tick_records = 0
        self.quarantined_parse_error_records = 0
        self.quarantined_by_side = Counter()
        self.disqualifying_tick_records = 0
        self.disqualifying_control_records = 0
        self.disqualifying_issues = Counter()
        self._pending = None

    def _reject_pending(self, reason):
        if self._pending is None:
            return
        decision = classify_one_sided_zero_quote(self._pending)
        self.disqualifying_tick_records += 1
        self.disqualifying_issues[reason] += 1
        if decision.issue:
            self.disqualifying_issues[decision.issue] += 1
        self._pending = None

    def _accept_control(self, envelope, event):
        self.control_records += 1
        if event.control_type in SAFE_CONTROL_TYPES:
            self.safe_control_records += 1
            return
        self.disqualifying_control_records += 1
        if event.control_type == "parse_error":
            for issue in _issues_from_control(event):
                self.disqualifying_issues[issue] += 1
        else:
            self.disqualifying_issues[f"control:{event.control_type}"] += 1

    def _accept_tick(self, envelope, event):
        self.tick_records += 1
        issues = _issues_from_tick(envelope)
        if not issues:
            self.clean_tick_records += 1
            return
        decision = classify_one_sided_zero_quote(envelope)
        if decision.accepted:
            self.zero_quote_candidate_ticks += 1
            self._pending = envelope
            return
        self.disqualifying_tick_records += 1
        for issue in issues:
            self.disqualifying_issues[issue] += 1

    def accept(self, envelope):
        if not isinstance(envelope, dict):
            raise TypeError("envelope object required")
        event = envelope.get("event")
        if not isinstance(event, (OrderedTick, CaptureControl)):
            raise TypeError("OrderedTick or CaptureControl event required")

        self.raw_records += 1

        if self._pending is not None:
            pair = classify_paired_zero_quote(self._pending, envelope)
            if pair.accepted:
                # Current record must be the mirrored parse_error by classifier contract.
                self.control_records += 1
                self.quarantined_tick_records += 1
                self.quarantined_parse_error_records += 1
                self.quarantined_by_side[pair.side] += 1
                self._pending = None
                return
            self._reject_pending("zero_quote_candidate_without_exact_mirrored_parse_error")

        if isinstance(event, CaptureControl):
            self._accept_control(envelope, event)
        else:
            self._accept_tick(envelope, event)

    def result(self):
        # EOF with a candidate but no exact mirrored control is fail-closed.
        self._reject_pending("zero_quote_candidate_without_exact_mirrored_parse_error")
        eligible = not (self.disqualifying_tick_records or self.disqualifying_control_records)
        return {
            "policy": POLICY,
            "smoke_quality_eligible": eligible,
            "counts": {
                "raw_records": self.raw_records,
                "tick_records": self.tick_records,
                "control_records": self.control_records,
                "clean_tick_records": self.clean_tick_records,
                "safe_control_records": self.safe_control_records,
            },
            "quarantine": {
                "zero_quote_candidate_ticks": self.zero_quote_candidate_ticks,
                "quarantined_tick_records": self.quarantined_tick_records,
                "quarantined_parse_error_records": self.quarantined_parse_error_records,
                "by_side": dict(sorted(self.quarantined_by_side.items())),
                "candidate_is_execution_eligible": False,
            },
            "disqualifying": {
                "tick_records": self.disqualifying_tick_records,
                "control_records": self.disqualifying_control_records,
                "issue_counts": dict(sorted(self.disqualifying_issues.items())),
            },
            "contracts": {
                "strict_prefix_qualification_unchanged": True,
                "nxt_smoke_eligibility_unchanged": True,
                "trade_direction_unverified_remains_disqualifying": True,
                "only_exact_mirrored_zero_quote_pair_is_quarantined": True,
            },
        }
