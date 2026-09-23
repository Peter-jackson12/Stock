from __future__ import annotations

import json
from pathlib import Path

import pytest

from collector.kiwoom.fid_read_ab_analysis import (
    MAX_STATUS_BYTES,
    RESULT_DIAGNOSTIC_ERROR,
    RESULT_INCOMPLETE,
    RESULT_INVALID,
    RESULT_LIMITED,
    RESULT_READY,
    FidReadAnalysisError,
    analyze_fid_read_ab_session,
)
from collector.kiwoom.fid_read_ab_diagnostic import (
    FEED_SCOPE_DIAGNOSTIC,
    PHASE_A1,
    PHASE_A2,
    PHASE_B,
    PHASE_POST,
    PHASE_PRE,
    active_fids_for,
    sidecar_payload,
)

REV = "b40f8c31a51111bc1e612afde290181048e29f51"
PHASES = (PHASE_PRE, PHASE_A1, PHASE_B, PHASE_A2, PHASE_POST)


def _phase_counts():
    trade = {phase: 0 for phase in PHASES}
    quote = {phase: 0 for phase in PHASES}
    trade.update({PHASE_A1: 2, PHASE_B: 2, PHASE_A2: 2, PHASE_POST: 1})
    quote.update({PHASE_A1: 1, PHASE_B: 1, PHASE_A2: 1})
    calls = {}
    for phase in PHASES:
        calls[phase] = (
            trade[phase] * len(active_fids_for("주식체결", phase))
            + quote[phase] * len(active_fids_for("주식호가잔량", phase))
        )
    return trade, quote, calls


def _write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def make_session(tmp_path: Path, *, include_b_sample=True):
    session = tmp_path / "session"
    session.mkdir()
    raw = tmp_path / "missing_raw.db"  # analyzer must not require or open this path

    trade, quote, calls = _phase_counts()
    counters = {
        "trade_callbacks_by_phase": trade,
        "quote_callbacks_by_phase": quote,
        "fid_calls_by_phase": calls,
        "fid_attempts_by_phase": dict(calls),
        "fid_read_failures_by_phase": {phase: 0 for phase in PHASES},
        "subscribed_at_set": True,
        "diagnostic_error": None,
        "counter_scope": "callback_read_attempts_not_accepted_or_committed",
        "a2_includes_shutdown_tail": False,
        "post_90s_phase": PHASE_POST,
    }
    sidecar = sidecar_payload(code_revision=REV, intended_server="mock")
    sidecar["phase_counters"] = counters
    sidecar["shutdown_reason"] = "제한 수집 시간 종료"
    _write_json(session / "fid_read_ab_test.json", sidecar)

    accepted = sum(trade.values()) + sum(quote.values())
    status = {
        "status_schema": "raw_capture_status_v1",
        "observed_at_utc": "2026-09-28T01:00:00+00:00",
        "identity": {
            "session_id": "fixture-session",
            "pid": 123,
            "started_at_utc": "2026-09-28T00:59:00+00:00",
            "executable": "C:/Python310-32/python.exe",
            "code_revision": REV,
            "python_bits": 32,
            "server": "mock",
            "feed_scope": FEED_SCOPE_DIAGNOSTIC,
            "dataset_path": str(raw.resolve()),
        },
        "snapshot": {
            "state": "closed",
            "accepting": False,
            "error": None,
            "accepted_callbacks": accepted,
            "committed_callbacks": accepted,
            "queued": 0,
            "in_flight": 0,
            "dropped_callbacks": 0,
            "pending_callbacks": 0,
            "committed_seq": accepted + 1,
            "data_quality": "unverified",
            "dataset_path": str(raw.resolve()),
            "session_id": "fixture-session",
            "feed_scope": FEED_SCOPE_DIAGNOSTIC,
            "last_event_ns": 90_000_000_000,
            "last_commit_at_utc": "2026-09-28T01:00:00+00:00",
            "writer_closed": True,
            "finalization": {
                "final_seq": accepted + 1,
                "close_ns": 91_000_000_000,
                "payload_sha256": "0" * 64,
            },
        },
        "received_trade_callbacks": sum(trade.values()),
        "received_quote_callbacks": sum(quote.values()),
        "error": None,
        "reason": "제한 수집 시간 종료",
        "control_heartbeat": False,
    }
    _write_json(session / "status.json", status)

    samples = []
    phase_order = [PHASE_A1, PHASE_B, PHASE_A2, PHASE_POST]
    for idx, phase in enumerate(phase_order):
        if phase == PHASE_B and not include_b_sample:
            continue
        samples.append({
            "code": "005930",
            "real_type": "주식체결",
            "source_fid": "20",
            "source_clock_raw": f"0900{idx:02d}",
            "received_at_utc": "2026-09-28T00:00:03+00:00",
            "received_ns": (idx + 1) * 5_000_000_000,
            "processing_ns": 100 + idx,
            "accepted": True,
            "diagnostic_phase": phase,
            "fid_call_count": 3 if phase == PHASE_B else 6,
            "fid_read_ns": 60 + idx,
            "queue_submit_ns": 40,
            "clock_comparison": {
                "status": "unverified_clock_difference",
                "difference_seconds": 2.5 + idx,
                "same_day_kst_assumption": True,
                "not_network_latency": True,
            },
        })
    telemetry = [
        {
            "schema": "capture_telemetry_v1",
            "kind": "header",
            "session_id": "fixture-session",
            "code_revision": REV,
            "sample_interval_ns": 5_000_000_000,
        },
        {
            "schema": "capture_telemetry_v1",
            "kind": "sample_batch",
            "session_id": "fixture-session",
            "observed_at_utc": "2026-09-28T01:00:00+00:00",
            "observed_ns": 100,
            "poll": {},
            "samples": samples,
            "diagnostic_samples_dropped": 0,
            "force": True,
        },
    ]
    _write_jsonl(session / "capture_telemetry.jsonl", telemetry)

    resources = [
        {
            "pid": 123,
            "at_utc": "2026-09-28T00:59:01+00:00",
            "working_set": 100,
            "peak_working_set": 120,
            "commit": 200,
            "peak_commit": 220,
        },
        {
            "pid": 123,
            "at_utc": "2026-09-28T01:00:01+00:00",
            "working_set": 150,
            "peak_working_set": 180,
            "commit": 250,
            "peak_commit": 280,
        },
    ]
    _write_jsonl(session / "resource_history.jsonl", resources)
    return session, raw


def test_clean_complete_session_is_analysis_ready_without_raw_access(tmp_path):
    session, raw = make_session(tmp_path)
    assert not raw.exists()
    report = analyze_fid_read_ab_session(session, expected_revision=REV)
    assert report["result"] == RESULT_READY
    assert report["issues"] == []
    assert report["identity"]["server"] == "mock"
    assert report["capture"]["writer_closed"] is True
    assert report["diagnostic"]["research_eligible"] is False
    assert report["telemetry"]["by_phase"][PHASE_B]["fid_call_count"]["median"] == 3
    assert report["resources"]["max_peak_commit"] == 280
    assert "raw database was not opened or scanned" in report["notes"]
    assert not raw.exists()


def test_missing_analysis_phase_telemetry_is_limited_not_failed(tmp_path):
    session, _ = make_session(tmp_path, include_b_sample=False)
    report = analyze_fid_read_ab_session(session)
    assert report["result"] == RESULT_LIMITED
    assert any(i["id"] == "telemetry_coverage" and PHASE_B in i["reason"] for i in report["issues"])
    assert report["capture"]["state"] == "closed"


def test_diagnostic_error_is_separate_from_capture_completion(tmp_path):
    session, _ = make_session(tmp_path)
    sidecar = json.loads((session / "fid_read_ab_test.json").read_text(encoding="utf-8"))
    sidecar["phase_counters"]["diagnostic_error"] = "clock_ns:RuntimeError"
    _write_json(session / "fid_read_ab_test.json", sidecar)
    report = analyze_fid_read_ab_session(session)
    assert report["result"] == RESULT_DIAGNOSTIC_ERROR
    assert report["capture"]["state"] == "closed"
    assert any(i["id"] == "diagnostic_error" for i in report["issues"])


def test_unclosed_storage_is_capture_incomplete(tmp_path):
    session, _ = make_session(tmp_path)
    status = json.loads((session / "status.json").read_text(encoding="utf-8"))
    status["snapshot"]["state"] = "interrupted"
    status["snapshot"]["writer_closed"] = False
    status["snapshot"]["finalization"] = None
    status["error"] = "shutdown incomplete"
    _write_json(session / "status.json", status)
    report = analyze_fid_read_ab_session(session)
    assert report["result"] == RESULT_INCOMPLETE
    ids = {i["id"] for i in report["issues"]}
    assert {"state", "writer_closed", "capture_error", "finalization"} <= ids


def test_identity_mismatch_is_invalid_even_if_files_are_small(tmp_path):
    session, _ = make_session(tmp_path)
    telemetry = [
        json.loads(line)
        for line in (session / "capture_telemetry.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    telemetry[0]["session_id"] = "other-session"
    _write_jsonl(session / "capture_telemetry.jsonl", telemetry)
    report = analyze_fid_read_ab_session(session)
    assert report["result"] == RESULT_INVALID
    assert any(i["id"] == "telemetry_session" for i in report["issues"])


def test_expected_revision_mismatch_is_invalid(tmp_path):
    session, _ = make_session(tmp_path)
    report = analyze_fid_read_ab_session(session, expected_revision="different")
    assert report["result"] == RESULT_INVALID
    assert any(i["id"] == "expected_revision" for i in report["issues"])


def test_bounded_reader_rejects_oversized_status_without_touching_raw(tmp_path):
    session, raw = make_session(tmp_path)
    (session / "status.json").write_text("x" * (MAX_STATUS_BYTES + 1), encoding="utf-8")
    with pytest.raises(FidReadAnalysisError, match="exceeds bounded size"):
        analyze_fid_read_ab_session(session)
    assert not raw.exists()

def test_python_bits_and_v2_phase_contract_are_identity_evidence(tmp_path):
    session, _ = make_session(tmp_path)
    status = json.loads((session / "status.json").read_text(encoding="utf-8"))
    status["identity"]["python_bits"] = 64
    _write_json(session / "status.json", status)
    sidecar = json.loads((session / "fid_read_ab_test.json").read_text(encoding="utf-8"))
    sidecar["strict_phase_windows"] = False
    sidecar["pure_com_cost_experiment"] = True
    sidecar["backlog_reset_between_phases"] = True
    sidecar["duration_is_shutdown_request_not_hard_cutoff"] = False
    sidecar["phase_counters"]["a2_includes_shutdown_tail"] = True
    _write_json(session / "fid_read_ab_test.json", sidecar)
    report = analyze_fid_read_ab_session(session)
    assert report["result"] == RESULT_INVALID
    ids = {i["id"] for i in report["issues"]}
    assert {
        "python_bits", "phase_window_contract", "duration_contract",
        "cost_coupling_contract", "backlog_contract", "a2_tail_contract",
    } <= ids


def test_closed_status_with_queue_accounting_conflict_is_incomplete(tmp_path):
    session, _ = make_session(tmp_path)
    status = json.loads((session / "status.json").read_text(encoding="utf-8"))
    status["snapshot"]["queued"] = 1
    status["snapshot"]["committed_callbacks"] -= 1
    _write_json(session / "status.json", status)
    report = analyze_fid_read_ab_session(session)
    assert report["result"] == RESULT_INCOMPLETE
    ids = {i["id"] for i in report["issues"]}
    assert {"queued", "callback_commit_accounting"} <= ids


def test_telemetry_samples_cannot_exceed_sidecar_callbacks(tmp_path):
    session, _ = make_session(tmp_path)
    rows = [
        json.loads(line)
        for line in (session / "capture_telemetry.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    a1 = next(s for s in rows[1]["samples"] if s["diagnostic_phase"] == PHASE_A1)
    rows[1]["samples"].extend([dict(a1), dict(a1)])
    _write_jsonl(session / "capture_telemetry.jsonl", rows)
    report = analyze_fid_read_ab_session(session)
    assert report["result"] == RESULT_INVALID
    assert any(i["id"] == "telemetry_trade_accounting" for i in report["issues"])


def test_resource_pid_mismatch_is_invalid(tmp_path):
    session, _ = make_session(tmp_path)
    rows = [
        json.loads(line)
        for line in (session / "resource_history.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rows[-1]["pid"] = 999
    _write_jsonl(session / "resource_history.jsonl", rows)
    report = analyze_fid_read_ab_session(session)
    assert report["result"] == RESULT_INVALID
    assert any(i["id"] == "resource_pid" for i in report["issues"])

def test_cli_prints_bounded_ready_report(tmp_path, capsys):
    from scripts.analyze_fid_read_ab import main
    session, raw = make_session(tmp_path)
    code = main(["--session-dir", str(session), "--expected-revision", REV])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == RESULT_READY
    assert payload["identity"]["code_revision"] == REV
    assert not raw.exists()


def test_cli_missing_session_returns_unavailable_without_creating_files(tmp_path, capsys):
    from scripts.analyze_fid_read_ab import main
    missing = tmp_path / "missing"
    code = main(["--session-dir", str(missing)])
    assert code == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["status"] == "unavailable"
    assert "raw database was not opened or scanned" in payload["note"]
    assert not missing.exists()

