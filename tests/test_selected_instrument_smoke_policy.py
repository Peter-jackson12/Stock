"""Synthetic selected-instrument smoke-quality policy regressions."""
from __future__ import annotations

from dataclasses import replace

import pytest

from collector.raw_v2 import CaptureControl
from collector.selected_instrument_smoke_policy import SelectedInstrumentSmokePolicy
from engine.tick_ordering import OrderedTick


UTC = "2026-09-21T00:00:00+00:00"
SELECTED = {"005930": "unknown"}


def envelope(event, *, issues, fids=None):
    return {
        "event": event,
        "received_at_utc": UTC,
        "raw_fields": {
            "normalization": "kiwoom_fids_prototype_1",
            "real_type": "주식체결" if event.kind == "trade" else "주식호가잔량",
            "price_policy": "signed_magnitude",
            "direction_policy": "signed_volume",
            "fids": {} if fids is None else dict(fids),
            "issues": list(issues),
        },
        "exchange_ts_raw": "090001",
        "source_time_precision": "second",
    }


def trade(seq, ns, *, code="005930", is_buy=True, issues=()):
    event = OrderedTick(
        "kiwoom", "s", seq, ns, code, "unknown", "trade",
        price=10000, volume=10, is_buy=is_buy, market_second=32401,
    )
    fids = {"20": "090001", "10": "-10000", "15": "+10" if is_buy is not None else " 10"}
    return envelope(event, issues=issues, fids=fids)


def zero_quote(seq, ns, *, code="005930", side="ask"):
    event = OrderedTick(
        "kiwoom", "s", seq, ns, code, "unknown", "quote",
        bid=8440, ask=None, bid_size=2, ask_size=0, market_second=31803,
        bid_sizes=(2, 2, 2), ask_sizes=(0, 0, 0),
    )
    fids = {
        "21": "085000",
        "41": "-0", "42": "-0", "43": "-0",
        "51": "-8440", "52": "-8430", "53": "-8420",
        "61": "0", "62": "0", "63": "0",
        "71": "2", "72": "2", "73": "2",
    }
    issue = "out_of_range_fid_41"
    if side == "bid":
        issue = "out_of_range_fid_51"
        event = replace(
            event, ask=11030, bid=None, ask_size=15, bid_size=0,
            ask_sizes=(15, 15, 15), bid_sizes=(0, 0, 0),
        )
        fids.update({
            "41": "-11030", "42": "-11040", "43": "-11050",
            "51": "-0", "52": "-0", "53": "-0",
            "61": "15", "62": "15", "63": "15",
            "71": "0", "72": "0", "73": "0",
        })
    return envelope(event, issues=(issue,), fids=fids)


def issue_quote(seq, ns, *, code="111111", issue="invalid_or_missing_fid_21"):
    event = OrderedTick(
        "kiwoom", "s", seq, ns, code, "unknown", "quote",
        bid=100, ask=101, bid_size=1, ask_size=1, market_second=32401,
        bid_sizes=(1, 1, 1), ask_sizes=(1, 1, 1),
    )
    return envelope(event, issues=(issue,), fids={"21": "", "41": "-101", "51": "-100"})


def parse_error(tick, *, seq=None, ns=None, code=None, issues=None):
    event = tick["event"]
    values = list(tick["raw_fields"]["issues"]) if issues is None else list(issues)
    control = CaptureControl(
        event.source, event.session_id,
        event.seq + 1 if seq is None else seq,
        event.received_ns if ns is None else ns,
        "parse_error",
        {"code": event.code if code is None else code, "issues": values},
    )
    return {
        "event": control,
        "received_at_utc": UTC,
        "raw_fields": {},
        "exchange_ts_raw": None,
        "source_time_precision": "unknown",
    }


def safe_control(seq=1, ns=1):
    return {
        "event": CaptureControl("kiwoom", "s", seq, ns, "session_start", {}),
        "received_at_utc": UTC,
        "raw_fields": {},
        "exchange_ts_raw": None,
        "source_time_precision": "unknown",
    }


def unsafe_control(seq, ns):
    return {
        "event": CaptureControl("kiwoom", "s", seq, ns, "callback_error", {"code": "111111"}),
        "received_at_utc": UTC,
        "raw_fields": {},
        "exchange_ts_raw": None,
        "source_time_precision": "unknown",
    }


def evaluate(*records, instruments=SELECTED):
    policy = SelectedInstrumentSmokePolicy(instruments)
    for record in records:
        policy.accept(record)
    return policy.result()


def test_selected_clean_trade_is_eligible():
    result = evaluate(trade(1, 1))

    assert result["selected_smoke_quality_eligible"] is True
    assert result["selected_input_present"] is True
    assert result["counts"]["selected_clean_ticks"] == 1


@pytest.mark.parametrize("side", ["ask", "bid"])
def test_selected_exact_zero_quote_pair_is_quarantined(side):
    tick = zero_quote(1, 1, side=side)
    result = evaluate(tick, parse_error(tick))

    assert result["selected_smoke_quality_eligible"] is True
    assert result["quarantine"]["selected_zero_quote_pairs"] == 1
    assert result["quarantine"]["selected_zero_quote_by_side"] == {side: 1}
    assert result["quarantine"]["zero_quote_execution_permission_granted"] is False


def test_selected_trade_direction_pair_is_disqualifying():
    tick = trade(1, 1, is_buy=None, issues=("trade_direction_unverified",))
    result = evaluate(tick, parse_error(tick))

    assert result["selected_smoke_quality_eligible"] is False
    assert result["disqualifying"]["selected_issue_pairs"] == 1
    assert result["disqualifying"]["selected_issue_counts"] == {"trade_direction_unverified": 1}
    assert result["disqualifying"]["selected_issue_examples"] == [{
        "tick_seq": 1,
        "control_seq": 2,
        "received_ns": 1,
        "received_at_utc": UTC,
        "exchange_ts_raw": "090001",
        "code": "005930",
        "venue": "unknown",
        "kind": "trade",
        "market_second": 32401,
        "issues": ["trade_direction_unverified"],
        "reason": "selected_issue_pair_disqualifying",
        "raw_fids": {"10": "-10000", "15": " 10", "20": "090001"},
        "normalized": {"price": 10000, "volume": 10, "is_buy": None},
        "paired_parse_error": True,
    }]
    assert result["disqualifying"]["selected_issue_example_limit"] == 10
    assert result["contracts"]["selected_trade_direction_unverified_is_disqualifying"] is True


def test_unselected_trade_direction_pair_is_ignored_for_selected_smoke_only():
    other = trade(1, 1, code="111111", is_buy=None, issues=("trade_direction_unverified",))
    selected = trade(3, 2)
    result = evaluate(other, parse_error(other), selected)

    assert result["selected_smoke_quality_eligible"] is True
    assert result["quarantine"]["unselected_issue_pairs_ignored"] == 1
    assert result["quarantine"]["unselected_ignored_issue_counts"] == {
        "trade_direction_unverified": 1
    }
    assert result["counts"]["selected_clean_ticks"] == 1
    assert result["contracts"]["whole_prefix_research_quality_upgraded"] is False


def test_unselected_unapproved_issue_pair_remains_global_failure():
    other = issue_quote(1, 1)
    selected = trade(3, 2)
    result = evaluate(other, parse_error(other), selected)

    assert result["selected_smoke_quality_eligible"] is False
    assert result["quarantine"]["unselected_issue_pairs_ignored"] == 0
    assert result["disqualifying"]["unselected_unapproved_issue_pairs"] == 1
    assert result["disqualifying"]["unsafe_controls"] == 0
    assert result["disqualifying"]["global_issue_counts"] == {
        "unselected_unapproved_issue:invalid_or_missing_fid_21": 1
    }


@pytest.mark.parametrize("change", [
    {"code": "wrong"},
    {"ns": 2},
    {"issues": ["other"]},
])
def test_unselected_issue_requires_exact_mirrored_pair(change):
    other = issue_quote(1, 1)
    result = evaluate(other, parse_error(other, **change))

    assert result["selected_smoke_quality_eligible"] is False
    assert result["disqualifying"]["unpaired_issue_ticks"] == 1
    assert result["disqualifying"]["unsafe_controls"] == 1


def test_pair_sequence_mismatch_is_rejected_as_stream_structure_error():
    other = issue_quote(1, 1)
    policy = SelectedInstrumentSmokePolicy(SELECTED)
    policy.accept(other)
    with pytest.raises(ValueError, match="contiguous"):
        policy.accept(parse_error(other, seq=3))


def test_unselected_issue_without_pair_at_eof_is_global_failure():
    result = evaluate(issue_quote(1, 1))

    assert result["selected_smoke_quality_eligible"] is False
    assert result["disqualifying"]["unpaired_issue_ticks"] == 1


def test_unsafe_control_anywhere_is_global_failure():
    result = evaluate(trade(1, 1), unsafe_control(2, 2))

    assert result["selected_smoke_quality_eligible"] is False
    assert result["disqualifying"]["unsafe_controls"] == 1
    assert result["disqualifying"]["global_issue_counts"] == {"control:callback_error": 1}
    assert result["contracts"]["unsafe_controls_are_global"] is True


def test_safe_control_and_unselected_clean_ticks_do_not_block_selected_smoke():
    result = evaluate(
        safe_control(1, 1),
        trade(2, 2, code="111111"),
        trade(3, 3),
    )

    assert result["selected_smoke_quality_eligible"] is True
    assert result["counts"]["safe_controls"] == 1
    assert result["counts"]["unselected_clean_ticks"] == 1
    assert result["counts"]["selected_clean_ticks"] == 1


def test_selected_input_absence_is_reported_without_inventing_failure():
    result = evaluate(trade(1, 1, code="111111"))

    assert result["selected_smoke_quality_eligible"] is True
    assert result["selected_input_present"] is False


def test_venue_mismatch_means_unselected():
    result = evaluate(trade(1, 1), instruments={"005930": "NXT"})

    assert result["selected_smoke_quality_eligible"] is True
    assert result["selected_input_present"] is False
    assert result["counts"]["unselected_clean_ticks"] == 1


@pytest.mark.parametrize("instruments", [{}, None, {"": "unknown"}, {"005930": ""}])
def test_invalid_selected_instrument_config_is_rejected(instruments):
    with pytest.raises(ValueError):
        SelectedInstrumentSmokePolicy(instruments)


def test_sequence_gap_is_rejected():
    policy = SelectedInstrumentSmokePolicy(SELECTED)
    policy.accept(trade(1, 1))
    with pytest.raises(ValueError, match="contiguous"):
        policy.accept(trade(3, 2))


def test_received_clock_reversal_is_rejected():
    policy = SelectedInstrumentSmokePolicy(SELECTED)
    policy.accept(trade(1, 2))
    with pytest.raises(ValueError, match="received_ns"):
        policy.accept(trade(2, 1))


def test_source_session_change_is_rejected():
    policy = SelectedInstrumentSmokePolicy(SELECTED)
    policy.accept(trade(1, 1))
    changed = trade(2, 2)
    changed["event"] = replace(changed["event"], session_id="other")
    with pytest.raises(ValueError, match="identity"):
        policy.accept(changed)



def test_selected_disqualifying_examples_are_bounded_to_ten():
    policy = SelectedInstrumentSmokePolicy(SELECTED)
    for index in range(12):
        seq = index * 2 + 1
        tick = trade(
            seq,
            index + 1,
            is_buy=None,
            issues=("trade_direction_unverified",),
        )
        policy.accept(tick)
        policy.accept(parse_error(tick))
    result = policy.result()

    assert result["disqualifying"]["selected_issue_pairs"] == 12
    assert result["disqualifying"]["selected_issue_counts"] == {
        "trade_direction_unverified": 12
    }
    examples = result["disqualifying"]["selected_issue_examples"]
    assert len(examples) == 10
    assert [item["tick_seq"] for item in examples] == list(range(1, 20, 2))
    assert all(item["paired_parse_error"] is True for item in examples)
