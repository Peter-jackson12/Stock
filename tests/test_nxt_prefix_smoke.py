"""End-to-end smoke replay from a qualified bounded raw-v2 prefix."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from collector.raw_v2 import CaptureControl, RawV2Writer
from collector.raw_v2_prefix_qualification import qualify_raw_v2_prefix
from engine.nxt_portfolio_research import NxtPortfolioRunFailed
from engine.nxt_prefix_smoke import run_nxt_prefix_smoke
from engine.tick_ordering import OrderedTick
from scripts.run_nxt_prefix_smoke import main


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows sealed immutable reader")


def quote(seq, ns, second):
    return OrderedTick(
        "fixture", "s", seq, ns, "005930", "unknown", "quote",
        bid=10000, ask=10001, bid_size=10, ask_size=3,
        market_second=second,
        bid_sizes=(10, 10, 10), ask_sizes=(3, 3, 3),
    )


def trade(seq, ns, second):
    return OrderedTick(
        "fixture", "s", seq, ns, "005930", "unknown", "trade",
        price=10001, volume=30, is_buy=True, market_second=second,
    )


def append(writer, event, utc, raw_fields=None):
    writer.append(
        event,
        received_at_utc=utc,
        raw_fields={} if raw_fields is None else raw_fields,
        exchange_ts_raw=None,
        source_time_precision="fixture",
    )


def build_raw(path, *, sentinel_ns=20):
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
        )
        append(writer, quote(2, 1, 32399), "2026-09-20T23:59:59Z")
        append(writer, trade(3, 3, 32400), "2026-09-21T00:00:00Z")
        append(writer, quote(4, sentinel_ns, 36000), "2026-09-21T01:00:00Z")
        writer.finish(close_ns=max(30, sentinel_ns + 1))


def qualify(path, tmp_path):
    return qualify_raw_v2_prefix(
        path,
        output_root=tmp_path / "qualification",
        expected_session_id="s",
        closure_evidence="synthetic fixture writer closed",
        end_market_second=36000,
    )


def config(instruments=None):
    return dict(
        instruments=instruments or {"005930": "unknown"},
        cash=1_000_000,
        fee_rate="0.001",
        max_quote_age_ns=100,
        buy_latency_ns=5,
        sell_latency_ns=5,
        cancel_latency_ns=2,
    )


def test_qualified_prefix_replays_real_raw_contract_into_nxt_portfolio(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw)
    prefix = qualify(raw, tmp_path)
    result = run_nxt_prefix_smoke(
        raw,
        prefix,
        output_root=tmp_path / "runs",
        simulator_config=config(),
        quantity=2,
    )
    saved = json.loads(result.read_text(encoding="utf-8"))

    assert saved["status"] == "completed_with_open_position"
    assert saved["event_count"] == 2
    assert len(saved["account"]["fills"]) == 1
    assert saved["account"]["fills"][0]["code"] == "005930"
    assert saved["strategy_signals"][0]["reason"] == "breakout"
    assert saved["input_provenance"]["kind"] == "raw_v2_prefix_smoke_v1"
    assert saved["input_provenance"]["purpose"] == "smoke_backtest_only"
    assert saved["input_provenance"]["whole_stream_assessed"] is False
    assert saved["input_provenance"]["performance_research_assessed"] is False
    assert saved["raw_identity_verified"] is False
    assert saved["realized_pnl"] is None
    assert saved["equity"] is None


def test_same_prefix_smoke_rerun_has_same_reproducibility_key(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw)
    prefix = qualify(raw, tmp_path)
    kwargs = dict(
        raw_path=raw,
        prefix_report=prefix,
        output_root=tmp_path / "runs",
        simulator_config=config(),
        quantity=1,
    )
    a = json.loads(run_nxt_prefix_smoke(**kwargs).read_text(encoding="utf-8"))
    b = json.loads(run_nxt_prefix_smoke(**kwargs).read_text(encoding="utf-8"))
    assert a["reproducibility_key"] == b["reproducibility_key"]
    assert a["account"]["fills"] == b["account"]["fills"]


def test_changed_prefix_bytes_after_qualification_fail_replay_and_keep_diagnostics(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw)
    prefix = qualify(raw, tmp_path)

    conn = sqlite3.connect(raw)
    try:
        mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        assert mode.lower() == "delete"
        payload = conn.execute("SELECT payload FROM events WHERE seq=2").fetchone()[0]
        envelope = json.loads(payload)
        envelope["raw_fields"]["test_note"] = "changed-after-qualification"
        changed = json.dumps(envelope, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        conn.execute("UPDATE events SET payload=? WHERE seq=2", (changed,))
        conn.commit()
    finally:
        conn.close()
    assert not Path(str(raw) + "-wal").exists()
    assert not Path(str(raw) + "-shm").exists()

    with pytest.raises(NxtPortfolioRunFailed) as caught:
        run_nxt_prefix_smoke(
            raw,
            prefix,
            output_root=tmp_path / "runs",
            simulator_config=config(),
            quantity=1,
        )
    saved = json.loads(caught.value.path.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["diagnostics_only"] is True
    assert "prefix digest no longer matches" in saved["error"]


def test_noneligible_prefix_report_is_rejected_before_run_output(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw)
    prefix = qualify(raw, tmp_path)
    report = json.loads(prefix.read_text(encoding="utf-8"))
    report["smoke_backtest_eligible"] = False
    report["smoke_backtest_eligibility"] = {"eligible": False, "reasons": ["fixture"]}
    bad = tmp_path / "bad-prefix.json"
    bad.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="not eligible"):
        run_nxt_prefix_smoke(
            raw,
            bad,
            output_root=tmp_path / "runs",
            simulator_config=config(),
            quantity=1,
        )
    assert not (tmp_path / "runs").exists()


def test_prefix_report_cannot_be_reused_for_a_different_raw_path(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw)
    prefix = qualify(raw, tmp_path)
    other = tmp_path / "copy" / "raw.db"
    other.parent.mkdir()
    shutil.copy2(raw, other)

    with pytest.raises(ValueError, match="exactly match"):
        run_nxt_prefix_smoke(
            other,
            prefix,
            output_root=tmp_path / "runs",
            simulator_config=config(),
            quantity=1,
        )


def test_equal_monotonic_time_at_cutoff_fails_closed(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw, sentinel_ns=3)
    prefix = qualify(raw, tmp_path)

    with pytest.raises(NxtPortfolioRunFailed) as caught:
        run_nxt_prefix_smoke(
            raw,
            prefix,
            output_root=tmp_path / "runs",
            simulator_config=config(),
            quantity=1,
        )
    saved = json.loads(caught.value.path.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert "event at or after exclusive close" in saved["error"]


def test_empty_selected_instrument_is_explicit_completed_empty_input(tmp_path):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw)
    prefix = qualify(raw, tmp_path)
    result = run_nxt_prefix_smoke(
        raw,
        prefix,
        output_root=tmp_path / "runs",
        simulator_config=config({"000660": "unknown"}),
        quantity=1,
    )
    saved = json.loads(result.read_text(encoding="utf-8"))
    assert saved["status"] == "completed_empty_input"
    assert saved["event_count"] == 0
    assert saved["account"]["fills"] == []


def test_cli_runs_only_from_qualified_prefix(tmp_path, capsys):
    raw = tmp_path / "raw" / "raw.db"
    build_raw(raw)
    prefix = qualify(raw, tmp_path)
    code = main([
        "--db", str(raw),
        "--prefix-report", str(prefix),
        "--output-root", str(tmp_path / "runs"),
        "--instrument", "005930=unknown",
        "--quantity", "1",
        "--cash", "1000000",
        "--fee-rate", "0.001",
        "--buy-latency-sec", "0.000000005",
        "--sell-latency-sec", "0.000000005",
        "--cancel-latency-sec", "0.000000002",
        "--max-quote-age-sec", "0.000000100",
    ])
    assert code == 0
    result = Path(capsys.readouterr().out.strip())
    saved = json.loads(result.read_text(encoding="utf-8"))
    assert saved["input_provenance"]["prefix_report_path"] == str(prefix.resolve())
