"""Synthetic regressions for the opt-in zero-quote smoke-quality policy."""
from __future__ import annotations

from dataclasses import replace

from collector.raw_v2 import CaptureControl
from collector.zero_quote_smoke_policy import ZeroQuoteSmokePolicy
from engine.tick_ordering import OrderedTick


UTC = "2026-09-21T00:00:00+00:00"


def quote(*, side="ask", seq=1, ns=10, issues=None):
    event = OrderedTick(
        source="kiwoom", session_id="s", seq=seq, received_ns=ns,
        code="0001P0" if side == "ask" else "0005C0",
        venue="unknown", kind="quote", market_second=31803,
        bid=8440, ask=None, bid_size=2, ask_size=0,
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
    return {
        "event": event,
        "received_at_utc": UTC,
        "raw_fields": {
            "normalization": "kiwoom_fids_prototype_1",
            "real_type": "주식호가잔량",
            "price_policy": "signed_magnitude",
            "direction_policy": "signed_volume",
            "fids": fids,
            "issues": [issue] if issues is None else list(issues),
        },
        "exchange_ts_raw": "085000",
        "source_time_precision": "second",
    }


def parse_error(tick, *, seq=None, ns=None, code=None, issues=None):
    event = tick["event"]
    control = CaptureControl(
        source=event.source,
        session_id=event.session_id,
        seq=event.seq + 1 if seq is None else seq,
        received_ns=event.received_ns if ns is None else ns,
        control_type="parse_error",
        details={
            "code": event.code if code is None else code,
            "issues": list(tick["raw_fields"]["issues"]) if issues is None else list(issues),
        },
    )
    return {
        "event": control,
        "received_at_utc": UTC,
        "raw_fields": {},
        "exchange_ts_raw": None,
        "source_time_precision": "unknown",
    }


def clean_trade(seq=1, ns=10):
    event = OrderedTick(
        "kiwoom", "s", seq, ns, "005930", "unknown", "trade",
        price=10000, volume=10, is_buy=True, market_second=32401,
    )
    return {
        "event": event,
        "received_at_utc": UTC,
        "raw_fields": {
            "normalization": "kiwoom_fids_prototype_1",
            "real_type": "주식체결",
            "price_policy": "signed_magnitude",
            "direction_policy": "signed_volume",
            "fids": {"20": "090001", "10": "-10000", "15": "+10"},
            "issues": [],
        },
        "exchange_ts_raw": "090001",
        "source_time_precision": "second",
    }


def unknown_direction_trade(seq=1, ns=10):
    envelope = clean_trade(seq, ns)
    envelope["event"] = replace(envelope["event"], is_buy=None)
    envelope["raw_fields"]["fids"]["15"] = " 10"
    envelope["raw_fields"]["issues"] = ["trade_direction_unverified"]
    return envelope


def safe_control(seq=1, ns=1):
    return {
        "event": CaptureControl("kiwoom", "s", seq, ns, "session_start", {}),
        "received_at_utc": UTC,
        "raw_fields": {},
        "exchange_ts_raw": None,
        "source_time_precision": "unknown",
    }


def evaluate(*envelopes):
    policy = ZeroQuoteSmokePolicy()
    for envelope in envelopes:
        policy.accept(envelope)
    return policy.result()


def test_exact_ask_zero_quote_pair_is_quarantined_and_quality_eligible():
    tick = quote(side="ask")
    result = evaluate(tick, parse_error(tick))

    assert result["smoke_quality_eligible"] is True
    assert result["quarantine"]["quarantined_tick_records"] == 1
    assert result["quarantine"]["quarantined_parse_error_records"] == 1
    assert result["quarantine"]["by_side"] == {"ask": 1}
    assert result["quarantine"]["candidate_is_execution_eligible"] is False
    assert result["disqualifying"]["issue_counts"] == {}


def test_exact_bid_zero_quote_pair_is_quarantined():
    tick = quote(side="bid")
    result = evaluate(tick, parse_error(tick))

    assert result["smoke_quality_eligible"] is True
    assert result["quarantine"]["by_side"] == {"bid": 1}


def test_clean_records_and_safe_control_coexist_with_quarantine():
    start = safe_control()
    tick = quote(seq=2, ns=2)
    clean = clean_trade(seq=4, ns=3)
    result = evaluate(start, tick, parse_error(tick), clean)

    assert result["smoke_quality_eligible"] is True
    assert result["counts"] == {
        "raw_records": 4,
        "tick_records": 2,
        "control_records": 2,
        "clean_tick_records": 1,
        "safe_control_records": 1,
    }


def test_two_independent_zero_quote_pairs_can_be_quarantined():
    ask = quote(side="ask", seq=1, ns=10)
    bid = quote(side="bid", seq=3, ns=20)
    result = evaluate(ask, parse_error(ask), bid, parse_error(bid))

    assert result["smoke_quality_eligible"] is True
    assert result["quarantine"]["quarantined_tick_records"] == 2
    assert result["quarantine"]["by_side"] == {"ask": 1, "bid": 1}


def test_candidate_without_parse_error_fails_closed_at_eof():
    result = evaluate(quote())

    assert result["smoke_quality_eligible"] is False
    assert result["disqualifying"]["tick_records"] == 1
    assert result["disqualifying"]["issue_counts"] == {
        "out_of_range_fid_41": 1,
        "zero_quote_candidate_without_exact_mirrored_parse_error": 1,
    }


def test_candidate_followed_by_clean_tick_fails_closed_but_clean_tick_is_still_processed():
    candidate = quote()
    result = evaluate(candidate, clean_trade(seq=2, ns=20))

    assert result["smoke_quality_eligible"] is False
    assert result["counts"]["clean_tick_records"] == 1
    assert result["disqualifying"]["tick_records"] == 1


def test_mismatched_parse_error_is_not_quarantined_and_both_records_disqualify():
    tick = quote()
    result = evaluate(tick, parse_error(tick, code="wrong"))

    assert result["smoke_quality_eligible"] is False
    assert result["quarantine"]["quarantined_tick_records"] == 0
    assert result["disqualifying"]["tick_records"] == 1
    assert result["disqualifying"]["control_records"] == 1
    assert result["disqualifying"]["issue_counts"]["out_of_range_fid_41"] == 2


def test_trade_direction_unverified_pair_remains_disqualifying():
    tick = unknown_direction_trade()
    result = evaluate(tick, parse_error(tick))

    assert result["smoke_quality_eligible"] is False
    assert result["quarantine"]["quarantined_tick_records"] == 0
    assert result["disqualifying"]["tick_records"] == 1
    assert result["disqualifying"]["control_records"] == 1
    assert result["disqualifying"]["issue_counts"] == {"trade_direction_unverified": 2}
    assert result["contracts"]["trade_direction_unverified_remains_disqualifying"] is True


def test_extra_issue_on_zero_quote_never_enters_pending_quarantine():
    tick = quote(issues=["out_of_range_fid_41", "invalid_or_missing_fid_21"])
    result = evaluate(tick, parse_error(tick))

    assert result["smoke_quality_eligible"] is False
    assert result["quarantine"]["zero_quote_candidate_ticks"] == 0
    assert result["disqualifying"]["tick_records"] == 1
    assert result["disqualifying"]["control_records"] == 1


def test_non_parse_error_control_is_disqualifying():
    control = {
        "event": CaptureControl("kiwoom", "s", 1, 1, "callback_error", {"code": "005930"}),
        "received_at_utc": UTC,
        "raw_fields": {},
        "exchange_ts_raw": None,
        "source_time_precision": "unknown",
    }
    result = evaluate(control)

    assert result["smoke_quality_eligible"] is False
    assert result["disqualifying"]["control_records"] == 1
    assert result["disqualifying"]["issue_counts"] == {"control:callback_error": 1}
