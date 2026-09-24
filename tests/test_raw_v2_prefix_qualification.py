"""Bounded raw-v2 prefix qualification regressions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from collector.raw_v2 import CaptureControl, OrderedTick, RawV2Writer
from collector.raw_v2_prefix_qualification import qualify_raw_v2_prefix
from scripts.qualify_raw_v2_prefix import main


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows sealed immutable reader")
MARKET_DATE = "2026-09-21"


def control(seq, ns, kind="session_start", details=None):
    return CaptureControl("fixture", "s", seq, ns, kind, {} if details is None else details)


def tick(seq, ns, *, code="005930", kind="quote", second=34200):
    kwargs = dict(
        source="fixture", session_id="s", seq=seq, received_ns=ns,
        code=code, venue="unknown", kind=kind, market_second=second,
    )
    if kind == "quote":
        kwargs.update(bid=10000, ask=10001, bid_size=10, ask_size=10,
                      bid_sizes=(10, 10, 10), ask_sizes=(10, 10, 10))
    else:
        kwargs.update(price=10001, volume=30, is_buy=True)
    return OrderedTick(**kwargs)


def append(writer, event, utc, raw_fields=None):
    writer.append(
        event,
        received_at_utc=utc,
        raw_fields={} if raw_fields is None else raw_fields,
        exchange_ts_raw=None,
        source_time_precision="fixture",
    )


def build(path, *, tail_issue=False, prefix_issue=False, incomplete=False,
          reach_cutoff=True, feed_scope="fixture"):
    with RawV2Writer(
        path,
        source="fixture",
        session_id="s",
        market_date=MARKET_DATE,
        feed_scope=feed_scope,
    ) as writer:
        append(writer, control(1, 1), "2026-09-21T00:00:00Z")
        append(writer, tick(2, 2, second=34200), "2026-09-21T00:30:00Z",
               {"issues": ["bad_prefix"]} if prefix_issue else {})
        seq = 3
        if prefix_issue:
            append(
                writer,
                control(seq, 2, "parse_error", {"code": "005930", "issues": ["bad_prefix"]}),
                "2026-09-21T00:30:00Z",
            )
            seq += 1
        if reach_cutoff:
            append(writer, tick(seq, 3, second=36000), "2026-09-21T01:00:00Z")
            seq += 1
        if tail_issue:
            append(
                writer,
                control(seq, 4, "callback_error", {"code": "005930"}),
                "2026-09-21T01:01:00Z",
            )
            seq += 1
        if incomplete:
            writer.commit()
        else:
            writer.finish(close_ns=100)


def run(path, tmp_path, **kwargs):
    result = qualify_raw_v2_prefix(
        path,
        output_root=tmp_path / "reports",
        expected_session_id="s",
        closure_evidence="synthetic fixture frozen after writer close",
        end_market_second=36000,
        **kwargs,
    )
    return result, json.loads(result.read_text(encoding="utf-8"))


def snapshot(directory):
    return {
        item.name: (
            item.stat().st_size,
            item.stat().st_mtime_ns,
            hashlib.sha256(item.read_bytes()).hexdigest(),
        )
        for item in directory.iterdir()
        if item.is_file()
    }


def test_clean_prefix_is_eligible_but_whole_stream_is_unassessed(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path)
    before = snapshot(path.parent)
    _, data = run(path, tmp_path)

    assert data["prefix_structure_verified"] is True
    assert data["smoke_backtest_eligible"] is True
    assert data["scope"]["tail_scanned"] is False
    assert data["scope"]["whole_stream_assessed"] is False
    assert data["scope"]["whole_stream_research_eligible"] is None
    assert data["performance_research_eligibility"]["eligible"] is None
    assert data["scope"]["boundary_sentinel"]["seq"] == 3
    assert data["counts"]["raw_records"] == 2
    assert data["counts"]["tick_records"] == 1
    assert len(data["prefix_event_sha256"]) == 64
    assert snapshot(path.parent) == before


def test_tail_failure_after_cutoff_does_not_retroactively_fail_clean_prefix(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path, tail_issue=True)
    _, data = run(path, tmp_path)

    assert data["smoke_backtest_eligible"] is True
    assert data["scope"]["boundary_sentinel"]["seq"] == 3
    assert data["records_consumed_through_sentinel"] == 3
    assert data["counts"]["control_by_type"] == {"session_start": 1}
    assert "callback_error" not in data["counts"]["control_by_type"]


def test_quality_issue_before_cutoff_rejects_prefix(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path, prefix_issue=True)
    _, data = run(path, tmp_path)

    assert data["prefix_structure_verified"] is True
    assert data["smoke_backtest_eligible"] is False
    assert data["quality_diagnostics"]["logical_issue_counts"] == {"bad_prefix": 1}


def test_frozen_incomplete_session_can_have_eligible_prefix_if_boundary_was_reached(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path, incomplete=True)
    _, data = run(path, tmp_path)

    assert data["input"]["manifest"]["state"] == "incomplete"
    assert data["prefix_structure_verified"] is True
    assert data["smoke_backtest_eligible"] is True
    assert data["scope"]["boundary_sentinel"]["seq"] == 3


def test_capture_that_never_reached_cutoff_is_not_a_verified_prefix(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path, reach_cutoff=False)
    _, data = run(path, tmp_path)

    assert data["status"] == "failed"
    assert data["prefix_structure_verified"] is False
    assert data["smoke_backtest_eligible"] is False
    assert "did not reach" in data["stream_error"]


def test_gap_before_cutoff_fails_structure(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM events WHERE seq=2")
    _, data = run(path, tmp_path)

    assert data["prefix_structure_verified"] is False
    assert data["smoke_backtest_eligible"] is False
    assert "sequence" in data["stream_error"]


def test_known_diagnostic_feed_scope_is_excluded_even_when_prefix_is_clean(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path, feed_scope="kiwoom_universe_fid_read_diagnostic")
    _, data = run(path, tmp_path)

    assert data["prefix_structure_verified"] is True
    assert data["smoke_backtest_eligible"] is False
    assert "diagnostic feed_scope" in data["prefix_research_eligibility"]["reasons"][0]


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sidecar_is_preserved_and_rejected(tmp_path, suffix):
    path = tmp_path / "source" / "raw.db"
    build(path)
    sidecar = Path(str(path) + suffix)
    sidecar.write_bytes(b"preserve")
    before = snapshot(path.parent)
    _, data = run(path, tmp_path)

    assert data["prefix_structure_verified"] is False
    assert "sidecar" in data["stream_error"]
    assert snapshot(path.parent) == before


def test_prefix_digest_is_reproducible(tmp_path):
    path = tmp_path / "source" / "raw.db"
    build(path)
    _, a = run(path, tmp_path)
    _, b = run(path, tmp_path)

    assert a["prefix_event_sha256"] == b["prefix_event_sha256"]
    assert a["counts"] == b["counts"]
    assert a["scope"]["boundary_sentinel"] == b["scope"]["boundary_sentinel"]


def test_cli_exit_codes_distinguish_eligible_quality_and_boundary_failure(tmp_path, capsys):
    clean = tmp_path / "clean" / "raw.db"
    quality = tmp_path / "quality" / "raw.db"
    short = tmp_path / "short" / "raw.db"
    build(clean)
    build(quality, prefix_issue=True)
    build(short, reach_cutoff=False)
    common = [
        "--expected-session-id", "s",
        "--closure-evidence", "synthetic frozen fixture",
        "--end-market-second", "36000",
    ]

    assert main(["--db", str(clean), "--output-root", str(tmp_path / "out1"), *common]) == 0
    assert Path(capsys.readouterr().out.strip()).exists()
    assert main(["--db", str(quality), "--output-root", str(tmp_path / "out2"), *common]) == 2
    assert Path(capsys.readouterr().out.strip()).exists()
    assert main(["--db", str(short), "--output-root", str(tmp_path / "out3"), *common]) == 3
    assert Path(capsys.readouterr().out.strip()).exists()


@pytest.mark.parametrize("value", [0, -1, 86400, True, 1.5])
def test_invalid_cutoff_rejected_before_output(tmp_path, value):
    path = tmp_path / "source" / "raw.db"
    build(path)
    with pytest.raises(ValueError, match="end_market_second"):
        qualify_raw_v2_prefix(
            path,
            output_root=tmp_path / "reports",
            expected_session_id="s",
            closure_evidence="synthetic",
            end_market_second=value,
        )
    assert not (tmp_path / "reports").exists()
