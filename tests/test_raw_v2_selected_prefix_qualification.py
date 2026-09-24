"""Synthetic Windows regressions for selected-instrument prefix overlay."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from collector.raw_v2 import CaptureControl, OrderedTick, RawV2Writer
from collector.raw_v2_prefix_qualification import qualify_raw_v2_prefix
from collector.raw_v2_selected_prefix_qualification import qualify_selected_prefix
from scripts.qualify_raw_v2_selected_prefix import main
from strategies.nxt_breakout.direction_window import (
    POLICY as QUARANTINE_UNKNOWN_DIRECTION_POLICY,
)


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows sealed immutable reader")
UTC0 = "2026-09-21T00:30:00Z"
UTC1 = "2026-09-21T01:00:00Z"
SELECTED = {"005930": "unknown"}


def append(writer, event, utc, *, raw_fields):
    writer.append(
        event,
        received_at_utc=utc,
        raw_fields=raw_fields,
        exchange_ts_raw="090000",
        source_time_precision="second",
    )


def clean_trade(seq, ns, *, code="005930"):
    return OrderedTick(
        "fixture", "s", seq, ns, code, "unknown", "trade",
        price=10000, volume=10, is_buy=True, market_second=34200,
    )


def unknown_trade(seq, ns, *, code):
    return OrderedTick(
        "fixture", "s", seq, ns, code, "unknown", "trade",
        price=10000, volume=10, is_buy=None, market_second=34200,
    )


def zero_quote(seq, ns, *, code="005930", side="ask"):
    kwargs = dict(
        source="fixture", session_id="s", seq=seq, received_ns=ns,
        code=code, venue="unknown", kind="quote", market_second=31803,
    )
    if side == "ask":
        kwargs.update(
            bid=8440, ask=None, bid_size=2, ask_size=0,
            bid_sizes=(2, 2, 2), ask_sizes=(0, 0, 0),
        )
    else:
        kwargs.update(
            bid=None, ask=11030, bid_size=0, ask_size=15,
            bid_sizes=(0, 0, 0), ask_sizes=(15, 15, 15),
        )
    return OrderedTick(**kwargs)


def raw_clean_trade():
    return {
        "normalization": "kiwoom_fids_prototype_1",
        "real_type": "주식체결",
        "price_policy": "signed_magnitude",
        "direction_policy": "signed_volume",
        "fids": {"20": "093000", "10": "-10000", "15": "+10"},
        "issues": [],
    }


def raw_unknown_trade():
    value = raw_clean_trade()
    value["fids"] = {"20": "093000", "10": "-10000", "15": " 10"}
    value["issues"] = ["trade_direction_unverified"]
    return value


def raw_zero_quote(side):
    if side == "ask":
        fids = {
            "21": "085000",
            "41": "-0", "42": "-0", "43": "-0",
            "51": "-8440", "52": "-8430", "53": "-8420",
            "61": "0", "62": "0", "63": "0",
            "71": "2", "72": "2", "73": "2",
        }
        issue = "out_of_range_fid_41"
    else:
        fids = {
            "21": "085000",
            "41": "-11030", "42": "-11040", "43": "-11050",
            "51": "-0", "52": "-0", "53": "-0",
            "61": "15", "62": "15", "63": "15",
            "71": "0", "72": "0", "73": "0",
        }
        issue = "out_of_range_fid_51"
    return {
        "normalization": "kiwoom_fids_prototype_1",
        "real_type": "주식호가잔량",
        "price_policy": "signed_magnitude",
        "direction_policy": "signed_volume",
        "fids": fids,
        "issues": [issue],
    }


def issue_quote(seq, ns, *, code="111111"):
    event = OrderedTick(
        "fixture", "s", seq, ns, code, "unknown", "quote",
        bid=100, ask=101, bid_size=1, ask_size=1, market_second=34200,
        bid_sizes=(1, 1, 1), ask_sizes=(1, 1, 1),
    )
    raw = {
        "normalization": "kiwoom_fids_prototype_1",
        "real_type": "주식호가잔량",
        "price_policy": "signed_magnitude",
        "direction_policy": "signed_volume",
        "fids": {"21": "", "41": "-101", "51": "-100"},
        "issues": ["invalid_or_missing_fid_21"],
    }
    return event, raw


def parse_error(seq, ns, *, code, issues):
    return CaptureControl(
        "fixture", "s", seq, ns, "parse_error",
        {"code": code, "issues": list(issues)},
    )


def build(path, *, scenario):
    with RawV2Writer(
        path, source="fixture", session_id="s",
        market_date="2026-09-21", feed_scope="fixture",
    ) as writer:
        append(
            writer,
            CaptureControl("fixture", "s", 1, 1, "session_start", {}),
            "2026-09-21T00:00:00Z",
            raw_fields={},
        )
        seq = 2
        ns = 2

        if scenario == "clean":
            append(writer, clean_trade(seq, ns), UTC0, raw_fields=raw_clean_trade())
            seq += 1
            ns += 1
        elif scenario == "unselected_direction":
            tick = unknown_trade(seq, ns, code="111111")
            raw = raw_unknown_trade()
            append(writer, tick, UTC0, raw_fields=raw)
            append(writer, parse_error(seq + 1, ns, code="111111", issues=raw["issues"]), UTC0, raw_fields={})
            seq += 2
            ns += 1
            append(writer, clean_trade(seq, ns), UTC0, raw_fields=raw_clean_trade())
            seq += 1
            ns += 1
        elif scenario == "selected_direction":
            tick = unknown_trade(seq, ns, code="005930")
            raw = raw_unknown_trade()
            append(writer, tick, UTC0, raw_fields=raw)
            append(writer, parse_error(seq + 1, ns, code="005930", issues=raw["issues"]), UTC0, raw_fields={})
            seq += 2
            ns += 1
        elif scenario in ("selected_zero_ask", "selected_zero_bid"):
            side = "ask" if scenario.endswith("ask") else "bid"
            tick = zero_quote(seq, ns, side=side)
            raw = raw_zero_quote(side)
            append(writer, tick, UTC0, raw_fields=raw)
            append(writer, parse_error(seq + 1, ns, code="005930", issues=raw["issues"]), UTC0, raw_fields={})
            seq += 2
            ns += 1
        elif scenario == "unselected_unknown":
            tick, raw = issue_quote(seq, ns)
            append(writer, tick, UTC0, raw_fields=raw)
            append(writer, parse_error(seq + 1, ns, code="111111", issues=raw["issues"]), UTC0, raw_fields={})
            seq += 2
            ns += 1
            append(writer, clean_trade(seq, ns), UTC0, raw_fields=raw_clean_trade())
            seq += 1
            ns += 1
        else:
            raise ValueError(scenario)

        append(writer, clean_trade(seq, ns, code="999999"), UTC1, raw_fields=raw_clean_trade())
        writer.finish(close_ns=1000)


def strict_report(path, root):
    result = qualify_raw_v2_prefix(
        path,
        output_root=root / "strict",
        expected_session_id="s",
        closure_evidence="synthetic closed fixture",
        end_market_second=36000,
    )
    return result, json.loads(result.read_text(encoding="utf-8"))


def overlay(path, strict_path, root, *, unknown_direction_policy="strict"):
    result = qualify_selected_prefix(
        path,
        strict_path,
        output_root=root / "selected",
        instruments=SELECTED,
        unknown_direction_policy=unknown_direction_policy,
    )
    return result, json.loads(result.read_text(encoding="utf-8"))


def test_clean_strict_prefix_stays_selected_eligible(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path, scenario="clean")
    strict_path, strict = strict_report(path, tmp_path)
    _, selected = overlay(path, strict_path, tmp_path)

    assert strict["smoke_backtest_eligible"] is True
    assert selected["schema"] == "raw_v2_selected_prefix_qualification_v2"
    assert selected["input"]["unknown_direction_policy"] == "strict"
    assert selected["selected_prefix_structure_verified"] is True
    assert selected["selected_smoke_quality_eligible"] is True
    assert all(selected["strict_revalidation"].values())
    assert "strategies/nxt_breakout/direction_window.py" in selected["code_provenance"]
    assert "strategies/nxt_breakout/tick_research.py" in selected["code_provenance"]


def test_unselected_direction_can_fail_strict_but_pass_selected_overlay(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path, scenario="unselected_direction")
    strict_path, strict = strict_report(path, tmp_path)
    _, selected = overlay(path, strict_path, tmp_path)

    assert strict["smoke_backtest_eligible"] is False
    assert strict["quality_diagnostics"]["logical_issue_counts"] == {"trade_direction_unverified": 1}
    assert selected["selected_smoke_quality_eligible"] is True
    policy = selected["selected_policy_result"]
    assert policy["quarantine"]["unselected_issue_pairs_ignored"] == 1
    assert policy["counts"]["selected_clean_ticks"] == 1
    assert selected["contracts"]["whole_prefix_research_quality_upgraded"] is False


def test_selected_direction_remains_selected_disqualifying(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path, scenario="selected_direction")
    strict_path, strict = strict_report(path, tmp_path)
    _, selected = overlay(path, strict_path, tmp_path)

    assert strict["smoke_backtest_eligible"] is False
    assert selected["selected_smoke_quality_eligible"] is False
    assert selected["selected_policy_result"]["disqualifying"]["selected_issue_counts"] == {
        "trade_direction_unverified": 1
    }


def test_selected_direction_can_pass_only_with_explicit_quarantine_policy(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path, scenario="selected_direction")
    strict_path, strict = strict_report(path, tmp_path)
    _, selected = overlay(
        path,
        strict_path,
        tmp_path,
        unknown_direction_policy=QUARANTINE_UNKNOWN_DIRECTION_POLICY,
    )

    assert strict["smoke_backtest_eligible"] is False
    assert selected["schema"] == "raw_v2_selected_prefix_qualification_v2"
    assert selected["input"]["unknown_direction_policy"] == QUARANTINE_UNKNOWN_DIRECTION_POLICY
    assert selected["selected_smoke_quality_eligible"] is True
    policy = selected["selected_policy_result"]
    assert policy["quarantine"]["selected_unknown_direction_pairs"] == 1
    assert policy["quarantine"]["unknown_direction_immediate_entry_permission_granted"] is False
    assert policy["disqualifying"]["selected_issue_pairs"] == 0
    assert policy["contracts"]["selected_unknown_direction_requires_strategy_window_quarantine"] is True
    assert selected["strict_prefix"]["smoke_backtest_eligible"] is False
    assert selected["strict_prefix"]["whole_prefix_quality_upgraded"] is False
    assert selected["contracts"]["selected_unknown_direction_policy_explicit"] is True


@pytest.mark.parametrize(("scenario", "side"), [
    ("selected_zero_ask", "ask"),
    ("selected_zero_bid", "bid"),
])
def test_selected_zero_quote_pair_can_be_quarantined_without_changing_strict_result(tmp_path, scenario, side):
    path = tmp_path / "source" / "raw.db"
    build(path, scenario=scenario)
    strict_path, strict = strict_report(path, tmp_path)
    _, selected = overlay(path, strict_path, tmp_path)

    assert strict["smoke_backtest_eligible"] is False
    assert selected["selected_smoke_quality_eligible"] is True
    assert selected["selected_policy_result"]["quarantine"]["selected_zero_quote_by_side"] == {side: 1}
    assert selected["strict_prefix"]["smoke_backtest_eligible"] is False
    assert selected["strict_prefix"]["whole_prefix_quality_upgraded"] is False


def test_unselected_unknown_issue_remains_global_failure(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path, scenario="unselected_unknown")
    strict_path, _ = strict_report(path, tmp_path)
    _, selected = overlay(path, strict_path, tmp_path)

    assert selected["selected_smoke_quality_eligible"] is False
    assert selected["selected_policy_result"]["disqualifying"]["unselected_unapproved_issue_pairs"] == 1


def test_tampered_strict_digest_is_detected_by_overlay(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path, scenario="clean")
    strict_path, strict = strict_report(path, tmp_path)
    strict["prefix_event_sha256"] = "0" * 64
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(strict), encoding="utf-8")

    _, selected = overlay(path, tampered, tmp_path)
    assert selected["status"] == "failed"
    assert selected["selected_prefix_structure_verified"] is False
    assert "digest" in selected["stream_error"]



@pytest.mark.parametrize("mutation, expected", [
    ("status", "completed structure"),
    ("performance", "performance research"),
])
def test_strict_report_contract_tamper_is_rejected(tmp_path, mutation, expected):
    path = tmp_path / "source" / "raw.db"
    build(path, scenario="clean")
    strict_path, strict = strict_report(path, tmp_path)
    if mutation == "status":
        strict["status"] = "failed"
    else:
        strict["performance_research_eligibility"]["assessed"] = True
        strict["performance_research_eligibility"]["eligible"] = False
    tampered = tmp_path / ("tampered-" + mutation + ".json")
    tampered.write_text(json.dumps(strict), encoding="utf-8")

    _, selected = overlay(path, tampered, tmp_path)
    assert selected["selected_prefix_structure_verified"] is False
    assert expected in selected["stream_error"]

def test_raw_path_must_match_strict_report(tmp_path):
    first = tmp_path / "a" / "raw.db"
    second = tmp_path / "b" / "raw.db"
    build(first, scenario="clean")
    build(second, scenario="clean")
    strict_path, _ = strict_report(first, tmp_path)

    _, selected = overlay(second, strict_path, tmp_path)
    assert selected["selected_prefix_structure_verified"] is False
    assert "exactly match" in selected["stream_error"]


def test_invalid_unknown_direction_policy_rejected_before_output(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path, scenario="clean")
    strict_path, _ = strict_report(path, tmp_path)
    output = tmp_path / "invalid-selected"

    with pytest.raises(ValueError, match="unknown selected unknown-direction policy"):
        qualify_selected_prefix(
            path,
            strict_path,
            output_root=output,
            instruments=SELECTED,
            unknown_direction_policy="guess-direction",
        )
    assert not output.exists()


def test_cli_exit_codes_for_selected_eligible_and_disqualifying(tmp_path, capsys):
    good = tmp_path / "good" / "raw.db"
    bad = tmp_path / "bad" / "raw.db"
    build(good, scenario="unselected_direction")
    build(bad, scenario="selected_direction")
    good_strict, _ = strict_report(good, tmp_path / "g")
    bad_strict, _ = strict_report(bad, tmp_path / "b")
    common = ["--instrument", "005930=unknown"]

    assert main([
        "--db", str(good),
        "--strict-prefix-report", str(good_strict),
        "--output-root", str(tmp_path / "go"),
        *common,
    ]) == 0
    assert Path(capsys.readouterr().out.strip()).exists()

    assert main([
        "--db", str(bad),
        "--strict-prefix-report", str(bad_strict),
        "--output-root", str(tmp_path / "bo"),
        *common,
    ]) == 2
    assert Path(capsys.readouterr().out.strip()).exists()

    assert main([
        "--db", str(bad),
        "--strict-prefix-report", str(bad_strict),
        "--output-root", str(tmp_path / "bq"),
        *common,
        "--unknown-direction-policy", QUARANTINE_UNKNOWN_DIRECTION_POLICY,
    ]) == 0
    quarantine_path = Path(capsys.readouterr().out.strip())
    quarantine = json.loads(quarantine_path.read_text(encoding="utf-8"))
    assert quarantine["selected_smoke_quality_eligible"] is True
    assert (
        quarantine["input"]["unknown_direction_policy"]
        == QUARANTINE_UNKNOWN_DIRECTION_POLICY
    )
