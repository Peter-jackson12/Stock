"""Synthetic-only regressions for the one-sided zero quote policy experiment."""
from __future__ import annotations

from dataclasses import replace

import pytest

from collector.raw_v2 import CaptureControl
from collector.zero_quote_policy_experiment import (
    ASK_ISSUE,
    BID_ISSUE,
    classify_one_sided_zero_quote,
    classify_paired_zero_quote,
    experimental_smoke_disposition,
)
from engine.tick_ordering import OrderedTick, ReceiveOrderReplay
from execution.quote_validation import check_ordered_quote


UTC = "2026-09-21T00:00:00+00:00"


def quote(*, side="ask", issues=None, raw_changes=None, event_changes=None):
    event = OrderedTick(
        source="kiwoom",
        session_id="s",
        seq=1,
        received_ns=10,
        code="0001P0",
        venue="unknown",
        kind="quote",
        bid=8440,
        ask=None,
        bid_size=2,
        ask_size=0,
        market_second=31803,
        bid_sizes=(2, 2, 2),
        ask_sizes=(0, 0, 0),
    )
    fids = {
        "21": "085000",
        "41": "-0", "42": "-0", "43": "-0",
        "51": "-8440", "52": "-8430", "53": "-8420",
        "61": "0", "62": "0", "63": "0",
        "71": "2", "72": "2", "73": "2",
    }
    if side == "bid":
        event = replace(
            event,
            code="0005C0",
            ask=11030,
            bid=None,
            ask_size=15,
            bid_size=0,
            ask_sizes=(15, 15, 15),
            bid_sizes=(0, 0, 0),
        )
        fids.update({
            "41": "-11030", "42": "-11040", "43": "-11050",
            "51": "-0", "52": "-0", "53": "-0",
            "61": "15", "62": "15", "63": "15",
            "71": "0", "72": "0", "73": "0",
        })
    if event_changes:
        event = replace(event, **event_changes)
    if raw_changes:
        fids.update(raw_changes)
    issue = ASK_ISSUE if side == "ask" else BID_ISSUE
    raw = {
        "normalization": "kiwoom_fids_prototype_1",
        "real_type": "주식호가잔량",
        "price_policy": "signed_magnitude",
        "direction_policy": "signed_volume",
        "fids": fids,
        "issues": [issue] if issues is None else list(issues),
    }
    return {
        "event": event,
        "received_at_utc": UTC,
        "raw_fields": raw,
        "exchange_ts_raw": "085000",
        "source_time_precision": "second",
    }


def parse_error(tick, *, changes=None):
    event = tick["event"]
    details = {
        "code": event.code,
        "issues": list(tick["raw_fields"]["issues"]),
    }
    control = CaptureControl(
        source=event.source,
        session_id=event.session_id,
        seq=event.seq + 1,
        received_ns=event.received_ns,
        control_type="parse_error",
        details=details,
    )
    if changes:
        if "details" in changes:
            control = replace(control, details=changes["details"])
        else:
            control = replace(control, **changes)
    return {
        "event": control,
        "received_at_utc": UTC,
        "raw_fields": {},
        "exchange_ts_raw": None,
        "source_time_precision": "unknown",
    }


@pytest.mark.parametrize(("side", "expected_issue"), [
    ("ask", ASK_ISSUE),
    ("bid", BID_ISSUE),
])
def test_observed_one_sided_negative_zero_pattern_is_candidate(side, expected_issue):
    envelope = quote(side=side)
    decision = classify_one_sided_zero_quote(envelope)

    assert decision.accepted is True
    assert decision.side == side
    assert decision.issue == expected_issue
    assert decision.disposition == "missing_non_executable_quote_candidate"
    assert experimental_smoke_disposition(envelope) == "quarantine_non_executable_quote"


@pytest.mark.parametrize(("side", "reason"), [
    ("ask", "invalid_ask"),
    ("bid", "invalid_bid"),
])
def test_candidate_quote_remains_non_executable(side, reason):
    envelope = quote(side=side)
    event = envelope["event"]
    replay = ReceiveOrderReplay(source=event.source, session_id=event.session_id, max_quote_age_ns=0)
    view = replay.accept(event)

    check = check_ordered_quote(view)
    assert check.book is None
    assert check.reason == reason


@pytest.mark.parametrize("mutation", [
    "deep_price_nonzero",
    "same_side_size_positive",
    "opposite_price_missing",
    "opposite_size_zero",
    "wrong_price_policy",
    "extra_issue",
    "both_top_sides_zero",
])
def test_nearby_but_unobserved_patterns_remain_disqualifying(mutation):
    envelope = quote()
    if mutation == "deep_price_nonzero":
        envelope["raw_fields"]["fids"]["42"] = "-8450"
    elif mutation == "same_side_size_positive":
        envelope["raw_fields"]["fids"]["61"] = "1"
    elif mutation == "opposite_price_missing":
        envelope["raw_fields"]["fids"]["51"] = "-0"
    elif mutation == "opposite_size_zero":
        envelope["event"] = replace(envelope["event"], bid_size=0)
    elif mutation == "wrong_price_policy":
        envelope["raw_fields"]["price_policy"] = "positive_only"
    elif mutation == "extra_issue":
        envelope["raw_fields"]["issues"].append("invalid_or_missing_fid_21")
    elif mutation == "both_top_sides_zero":
        envelope["raw_fields"]["fids"].update({"51": "-0", "52": "-0", "53": "-0"})
        envelope["event"] = replace(envelope["event"], bid=None, bid_size=0, bid_sizes=(0, 0, 0))
        envelope["raw_fields"]["issues"] = [ASK_ISSUE, BID_ISSUE]

    decision = classify_one_sided_zero_quote(envelope)
    assert decision.accepted is False
    assert experimental_smoke_disposition(envelope) == "disqualifying"


def test_trade_direction_unverified_is_never_downgraded():
    event = OrderedTick(
        "kiwoom", "s", 1, 10, "487130", "unknown", "trade",
        price=10000, volume=1, is_buy=None, market_second=32406,
    )
    envelope = {
        "event": event,
        "received_at_utc": UTC,
        "raw_fields": {
            "normalization": "kiwoom_fids_prototype_1",
            "real_type": "주식체결",
            "price_policy": "signed_magnitude",
            "direction_policy": "signed_volume",
            "fids": {"20": "090001", "10": "-10000", "15": " 1"},
            "issues": ["trade_direction_unverified"],
        },
        "exchange_ts_raw": "090001",
        "source_time_precision": "second",
    }

    assert classify_one_sided_zero_quote(envelope).accepted is False
    assert experimental_smoke_disposition(envelope) == "disqualifying"


@pytest.mark.parametrize("side", ["ask", "bid"])
def test_exact_mirrored_parse_error_pair_is_candidate(side):
    tick = quote(side=side)
    control = parse_error(tick)
    decision = classify_paired_zero_quote(tick, control)

    assert decision.accepted is True
    assert decision.side == side
    assert decision.tick_seq == 1
    assert decision.control_seq == 2
    assert decision.disposition == "paired_missing_non_executable_quote_candidate"


@pytest.mark.parametrize("changes", [
    {"received_ns": 11},
    {"seq": 3},
    {"details": {"code": "wrong", "issues": [ASK_ISSUE]}},
    {"details": {"code": "0001P0", "issues": [ASK_ISSUE, "extra"]}},
])
def test_parse_error_pair_must_match_exactly(changes):
    tick = quote()
    decision = classify_paired_zero_quote(tick, parse_error(tick, changes=changes))
    assert decision.accepted is False
    assert decision.disposition == "disqualifying"


def test_clean_quote_stays_clean_and_is_not_experimental_candidate():
    envelope = quote()
    envelope["event"] = replace(
        envelope["event"],
        ask=8450,
        ask_size=3,
        ask_sizes=(3, 3, 3),
    )
    envelope["raw_fields"]["fids"].update({
        "41": "-8450", "42": "-8460", "43": "-8470",
        "61": "3", "62": "3", "63": "3",
    })
    envelope["raw_fields"]["issues"] = []

    assert classify_one_sided_zero_quote(envelope).accepted is False
    assert experimental_smoke_disposition(envelope) == "clean"
