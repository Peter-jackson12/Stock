"""End-to-end smoke replay from an eligible selected-prefix v2 overlay."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from collector.raw_v2 import CaptureControl, RawV2Writer
from collector.raw_v2_prefix_qualification import qualify_raw_v2_prefix
from collector.raw_v2_selected_prefix_qualification import qualify_selected_prefix
from engine.nxt_portfolio_research import NxtPortfolioRunFailed
from engine.nxt_selected_prefix_smoke import run_nxt_selected_prefix_smoke
from engine.tick_ordering import OrderedTick
from scripts.run_nxt_selected_prefix_smoke import main
from strategies.nxt_breakout.direction_window import (
    POLICY as QUARANTINE_UNKNOWN_DIRECTION_POLICY,
)


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows sealed immutable reader")


def append(writer, event, utc, *, raw_fields, exchange_ts_raw="090000"):
    writer.append(
        event,
        received_at_utc=utc,
        raw_fields=raw_fields,
        exchange_ts_raw=exchange_ts_raw,
        source_time_precision="second",
    )


def quote(seq, ns, second, *, code="005930", **changes):
    event = OrderedTick(
        "fixture", "s", seq, ns, code, "unknown", "quote",
        bid=10000, ask=10001, bid_size=10, ask_size=3,
        market_second=second,
        bid_sizes=(10, 10, 10), ask_sizes=(3, 3, 3),
    )
    if changes:
        from dataclasses import replace
        event = replace(event, **changes)
    return event


def trade(seq, ns, second, *, code="005930", is_buy=True, volume=30, price=10001):
    return OrderedTick(
        "fixture", "s", seq, ns, code, "unknown", "trade",
        price=price, volume=volume, is_buy=is_buy, market_second=second,
    )


def raw_clean_quote():
    return {
        "normalization": "kiwoom_fids_prototype_1",
        "real_type": "주식호가잔량",
        "price_policy": "signed_magnitude",
        "direction_policy": "signed_volume",
        "fids": {
            "21": "090000",
            "41": "-10001",
            "51": "-10000",
            "61": "3",
            "71": "10",
        },
        "issues": [],
    }


def raw_clean_trade(*, volume=30, price=10001, exchange="090001"):
    return {
        "normalization": "kiwoom_fids_prototype_1",
        "real_type": "주식체결",
        "price_policy": "signed_magnitude",
        "direction_policy": "signed_volume",
        "fids": {
            "20": exchange,
            "10": f"+{price}",
            "15": f"+{volume}",
        },
        "issues": [],
    }


def raw_unknown_trade(*, volume=237016, price=264000):
    return {
        "normalization": "kiwoom_fids_prototype_1",
        "real_type": "주식체결",
        "price_policy": "signed_magnitude",
        "direction_policy": "signed_volume",
        "fids": {
            "20": "090025",
            "10": f"+{price}",
            "14": "62573",
            "15": f" {volume}",
            "27": f"+{price}",
            "28": "+263500",
        },
        "issues": ["trade_direction_unverified"],
    }


def zero_quote(seq, ns, second):
    return OrderedTick(
        "fixture", "s", seq, ns, "005930", "unknown", "quote",
        bid=8440, ask=None, bid_size=2, ask_size=0,
        market_second=second,
        bid_sizes=(2, 2, 2), ask_sizes=(0, 0, 0),
    )


def raw_zero_quote():
    return {
        "normalization": "kiwoom_fids_prototype_1",
        "real_type": "주식호가잔량",
        "price_policy": "signed_magnitude",
        "direction_policy": "signed_volume",
        "fids": {
            "21": "085000",
            "41": "-0", "42": "-0", "43": "-0",
            "51": "-8440", "52": "-8430", "53": "-8420",
            "61": "0", "62": "0", "63": "0",
            "71": "2", "72": "2", "73": "2",
        },
        "issues": ["out_of_range_fid_41"],
    }


def parse_error(seq, ns, *, code="005930", issues):
    return CaptureControl(
        "fixture", "s", seq, ns, "parse_error",
        {"code": code, "issues": list(issues)},
    )


def build_raw(path, *, scenario="direction"):
    with RawV2Writer(
        path,
        source="fixture",
        session_id="s",
        market_date="2026-09-21",
        feed_scope="fixture",
    ) as writer:
        append(
            writer,
            CaptureControl("fixture", "s", 1, 0, "session_start", {}),
            "2026-09-20T23:59:58Z",
            raw_fields={},
            exchange_ts_raw=None,
        )
        seq = 2
        ns = 1

        if scenario == "direction":
            append(
                writer,
                quote(seq, ns, 32399),
                "2026-09-20T23:59:59Z",
                raw_fields=raw_clean_quote(),
                exchange_ts_raw="085959",
            )
            seq += 1
            ns += 1
            raw = raw_unknown_trade()
            append(
                writer,
                trade(seq, ns, 32400, is_buy=None, volume=237016, price=264000),
                "2026-09-21T00:00:00Z",
                raw_fields=raw,
                exchange_ts_raw="090025",
            )
            append(
                writer,
                parse_error(seq + 1, ns, issues=raw["issues"]),
                "2026-09-21T00:00:00Z",
                raw_fields={},
                exchange_ts_raw=None,
            )
            seq += 2
            ns += 1
            for index in range(15):
                second = 32401 + index
                append(
                    writer,
                    trade(seq, ns, second, price=264000),
                    f"2026-09-21T00:00:{index + 1:02d}Z",
                    raw_fields=raw_clean_trade(
                        volume=30,
                        price=264000,
                        exchange=f"0900{index + 1:02d}",
                    ),
                    exchange_ts_raw=f"0900{index + 1:02d}",
                )
                seq += 1
                ns += 1
        elif scenario == "zero_quote":
            raw = raw_zero_quote()
            append(
                writer,
                zero_quote(seq, ns, 31800),
                "2026-09-20T23:50:00Z",
                raw_fields=raw,
                exchange_ts_raw="085000",
            )
            append(
                writer,
                parse_error(seq + 1, ns, issues=raw["issues"]),
                "2026-09-20T23:50:00Z",
                raw_fields={},
                exchange_ts_raw=None,
            )
            seq += 2
            ns += 1
            append(
                writer,
                quote(seq, ns, 32399),
                "2026-09-20T23:59:59Z",
                raw_fields=raw_clean_quote(),
                exchange_ts_raw="085959",
            )
            seq += 1
            ns += 1
            append(
                writer,
                trade(seq, ns, 32400),
                "2026-09-21T00:00:00Z",
                raw_fields=raw_clean_trade(),
                exchange_ts_raw="090000",
            )
            seq += 1
            ns += 1
        elif scenario == "clean":
            append(
                writer,
                quote(seq, ns, 32399),
                "2026-09-20T23:59:59Z",
                raw_fields=raw_clean_quote(),
                exchange_ts_raw="085959",
            )
            seq += 1
            ns += 1
            append(
                writer,
                trade(seq, ns, 32400),
                "2026-09-21T00:00:00Z",
                raw_fields=raw_clean_trade(),
                exchange_ts_raw="090000",
            )
            seq += 1
            ns += 1
        else:
            raise ValueError(scenario)

        append(
            writer,
            quote(seq, 100, 36000, code="999999"),
            "2026-09-21T01:00:00Z",
            raw_fields=raw_clean_quote(),
            exchange_ts_raw="100000",
        )
        writer.finish(close_ns=1000)


def strict_report(raw, root):
    return qualify_raw_v2_prefix(
        raw,
        output_root=root / "strict",
        expected_session_id="s",
        closure_evidence="synthetic fixture writer closed",
        end_market_second=36000,
    )


def selected_report(raw, strict, root, *, policy=QUARANTINE_UNKNOWN_DIRECTION_POLICY):
    return qualify_selected_prefix(
        raw,
        strict,
        output_root=root / "selected",
        instruments={"005930": "unknown"},
        unknown_direction_policy=policy,
    )


def config(instruments=None):
    return dict(
        instruments=instruments or {"005930": "unknown"},
        cash=10_000_000,
        fee_rate="0.001",
        max_quote_age_ns=100,
        buy_latency_ns=5,
        sell_latency_ns=5,
        cancel_latency_ns=2,
    )


def test_selected_v2_direction_quarantine_replays_into_strategy_window(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw, scenario="direction")
    strict = strict_report(raw, tmp_path)
    strict_data = json.loads(strict.read_text(encoding="utf-8"))
    assert strict_data["smoke_backtest_eligible"] is False

    selected = selected_report(raw, strict, tmp_path)
    selected_data = json.loads(selected.read_text(encoding="utf-8"))
    assert selected_data["selected_smoke_quality_eligible"] is True
    assert selected_data["selected_policy_result"]["quarantine"]["selected_unknown_direction_pairs"] == 1

    result = run_nxt_selected_prefix_smoke(
        raw,
        selected,
        output_root=tmp_path / "runs",
        simulator_config=config(),
        quantity=1,
    )
    saved = json.loads(result.read_text(encoding="utf-8"))

    assert saved["status"] == "completed_with_open_position"
    assert saved["event_count"] == 17
    assert len(saved["strategy_signals"]) == 1
    assert saved["strategy_signals"][0]["reason"] == "breakout"
    assert saved["settings"]["strategy"]["unknown_direction_policy"] == QUARANTINE_UNKNOWN_DIRECTION_POLICY
    assert saved["input_provenance"]["kind"] == "raw_v2_selected_prefix_smoke_v1"
    assert saved["input_provenance"]["strict_smoke_backtest_eligible"] is False
    assert saved["input_provenance"]["selected_policy_result"]["quarantine"]["selected_unknown_direction_pairs"] == 1
    assert saved["input_provenance"]["whole_stream_assessed"] is False
    assert saved["input_provenance"]["performance_research_assessed"] is False
    assert saved["raw_identity_verified"] is False
    assert saved["realized_pnl"] is None
    assert saved["equity"] is None


def test_selected_zero_quote_pair_is_withheld_from_strategy_input(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw, scenario="zero_quote")
    strict = strict_report(raw, tmp_path)
    selected = selected_report(raw, strict, tmp_path)
    selected_data = json.loads(selected.read_text(encoding="utf-8"))

    assert selected_data["selected_policy_result"]["quarantine"]["selected_zero_quote_pairs"] == 1
    result = run_nxt_selected_prefix_smoke(
        raw,
        selected,
        output_root=tmp_path / "runs",
        simulator_config=config(),
        quantity=1,
    )
    saved = json.loads(result.read_text(encoding="utf-8"))

    assert saved["event_count"] == 2
    assert saved["strategy_signals"][0]["reason"] == "breakout"
    assert all(item["event_seq"] != 2 for item in saved["order_intents"])


def test_strict_selected_report_is_rejected_before_run_output(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw, scenario="clean")
    strict = strict_report(raw, tmp_path)
    selected = selected_report(raw, strict, tmp_path, policy="strict")
    selected_data = json.loads(selected.read_text(encoding="utf-8"))
    assert selected_data["selected_smoke_quality_eligible"] is True

    output = tmp_path / "runs"
    with pytest.raises(ValueError, match="explicit quarantine"):
        run_nxt_selected_prefix_smoke(
            raw,
            selected,
            output_root=output,
            simulator_config=config(),
            quantity=1,
        )
    assert not output.exists()


def test_instrument_mismatch_is_rejected_before_run_output(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw, scenario="direction")
    strict = strict_report(raw, tmp_path)
    selected = selected_report(raw, strict, tmp_path)

    output = tmp_path / "runs"
    with pytest.raises(ValueError, match="exactly match selected overlay instruments"):
        run_nxt_selected_prefix_smoke(
            raw,
            selected,
            output_root=output,
            simulator_config=config({"000660": "unknown"}),
            quantity=1,
        )
    assert not output.exists()




def test_changed_raw_after_selected_overlay_fails_replay_and_keeps_diagnostics(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw, scenario="direction")
    strict = strict_report(raw, tmp_path)
    selected = selected_report(raw, strict, tmp_path)

    conn = sqlite3.connect(raw)
    try:
        mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        assert mode.lower() == "delete"
        payload = conn.execute("SELECT payload FROM events WHERE seq=2").fetchone()[0]
        envelope = json.loads(payload)
        envelope["raw_fields"]["test_note"] = "changed-after-selected-overlay"
        changed = json.dumps(
            envelope,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        conn.execute("UPDATE events SET payload=? WHERE seq=2", (changed,))
        conn.commit()
    finally:
        conn.close()
    assert not Path(str(raw) + "-wal").exists()
    assert not Path(str(raw) + "-shm").exists()

    with pytest.raises(NxtPortfolioRunFailed) as caught:
        run_nxt_selected_prefix_smoke(
            raw,
            selected,
            output_root=tmp_path / "runs",
            simulator_config=config(),
            quantity=1,
        )
    saved = json.loads(caught.value.path.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["diagnostics_only"] is True
    assert "prefix digest no longer matches" in saved["error"]


def test_changed_strict_report_after_overlay_is_rejected_before_output(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw, scenario="direction")
    strict = strict_report(raw, tmp_path)
    selected = selected_report(raw, strict, tmp_path)

    strict_data = json.loads(strict.read_text(encoding="utf-8"))
    strict_data["closure_evidence"] = "tampered-after-overlay"
    strict.write_text(json.dumps(strict_data), encoding="utf-8")

    output = tmp_path / "runs"
    with pytest.raises(ValueError, match="strict prefix report content"):
        run_nxt_selected_prefix_smoke(
            raw,
            selected,
            output_root=output,
            simulator_config=config(),
            quantity=1,
        )
    assert not output.exists()

def test_selected_report_policy_result_tamper_fails_during_replay(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw, scenario="direction")
    strict = strict_report(raw, tmp_path)
    selected = selected_report(raw, strict, tmp_path)
    report = json.loads(selected.read_text(encoding="utf-8"))
    report["selected_policy_result"]["quarantine"]["selected_unknown_direction_pairs"] = 2
    tampered = tmp_path / "tampered-selected.json"
    tampered.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(NxtPortfolioRunFailed) as caught:
        run_nxt_selected_prefix_smoke(
            raw,
            tampered,
            output_root=tmp_path / "runs",
            simulator_config=config(),
            quantity=1,
        )
    saved = json.loads(caught.value.path.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["diagnostics_only"] is True
    assert "selected policy result no longer matches" in saved["error"]


def test_selected_report_code_provenance_tamper_is_rejected_before_output(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw, scenario="direction")
    strict = strict_report(raw, tmp_path)
    selected = selected_report(raw, strict, tmp_path)
    report = json.loads(selected.read_text(encoding="utf-8"))
    report["code_provenance"]["strategies/nxt_breakout/tick_research.py"] = "0" * 64
    tampered = tmp_path / "tampered-code.json"
    tampered.write_text(json.dumps(report), encoding="utf-8")

    output = tmp_path / "runs"
    with pytest.raises(ValueError, match="code provenance"):
        run_nxt_selected_prefix_smoke(
            raw,
            tampered,
            output_root=output,
            simulator_config=config(),
            quantity=1,
        )
    assert not output.exists()


def test_cli_runs_selected_v2_smoke_without_exposing_policy_override(tmp_path, capsys):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw, scenario="direction")
    strict = strict_report(raw, tmp_path)
    selected = selected_report(raw, strict, tmp_path)

    code = main([
        "--db", str(raw),
        "--selected-prefix-report", str(selected),
        "--output-root", str(tmp_path / "runs"),
        "--instrument", "005930=unknown",
        "--quantity", "1",
        "--cash", "10000000",
        "--fee-rate", "0.001",
        "--buy-latency-sec", "0.000000005",
        "--sell-latency-sec", "0.000000005",
        "--cancel-latency-sec", "0.000000002",
        "--max-quote-age-sec", "0.000000100",
    ])
    assert code == 0
    path = Path(capsys.readouterr().out.strip())
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["settings"]["strategy"]["unknown_direction_policy"] == QUARANTINE_UNKNOWN_DIRECTION_POLICY
