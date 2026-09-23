"""Bounded post-run summary for one FID A-B-A diagnostic session.

Reads only small session evidence files. It never opens or scans the raw database,
does not certify research eligibility, and does not infer a native/root cause.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import median

from collector.kiwoom.capture_telemetry import CaptureTelemetry
from collector.kiwoom.fid_read_ab_diagnostic import (
    FEED_SCOPE_DIAGNOSTIC,
    PHASE_A1,
    PHASE_A2,
    PHASE_B,
    PHASE_POST,
    PHASE_PRE,
    SIDECAR_NAME,
    active_fids_for,
)

ANALYSIS_SCHEMA = "fid_read_ab_analysis_v1"
STATUS_NAME = "status.json"
TELEMETRY_NAME = "capture_telemetry.jsonl"
RESOURCE_NAME = "resource_history.jsonl"

MAX_STATUS_BYTES = 256 * 1024
MAX_SIDECAR_BYTES = 256 * 1024
MAX_TELEMETRY_BYTES = CaptureTelemetry.MAX_BYTES
MAX_RESOURCE_BYTES = 4 * 1024 * 1024
MAX_TELEMETRY_LINES = CaptureTelemetry.MAX_BATCHES + 1
MAX_RESOURCE_LINES = 5_000
MAX_JSONL_LINE_BYTES = 64 * 1024

PHASES = (PHASE_PRE, PHASE_A1, PHASE_B, PHASE_A2, PHASE_POST)
ANALYSIS_PHASES = (PHASE_A1, PHASE_B, PHASE_A2)

RESULT_READY = "CAPTURE_COMPLETE_ANALYSIS_READY"
RESULT_LIMITED = "CAPTURE_COMPLETE_EVIDENCE_LIMITED"
RESULT_DIAGNOSTIC_ERROR = "CAPTURE_COMPLETE_DIAGNOSTIC_ERROR"
RESULT_INCOMPLETE = "CAPTURE_INCOMPLETE"
RESULT_INVALID = "CAPTURE_EVIDENCE_INVALID"


class FidReadAnalysisError(ValueError):
    pass


def _bounded_text(path: Path, limit: int) -> str:
    path = Path(path)
    if not path.is_file():
        raise FidReadAnalysisError(f"required evidence file missing: {path.name}")
    size = path.stat().st_size
    if size > limit:
        raise FidReadAnalysisError(f"evidence file exceeds bounded size: {path.name} ({size} > {limit})")
    return path.read_text(encoding="utf-8")


def _read_json(path: Path, limit: int) -> dict:
    try:
        value = json.loads(_bounded_text(path, limit))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FidReadAnalysisError(f"invalid JSON evidence {path.name}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise FidReadAnalysisError(f"JSON evidence must be an object: {path.name}")
    return value


def _read_jsonl(path: Path, *, max_bytes: int, max_lines: int, required: bool) -> list[dict] | None:
    path = Path(path)
    if not path.is_file():
        if required:
            raise FidReadAnalysisError(f"required evidence file missing: {path.name}")
        return None
    size = path.stat().st_size
    if size > max_bytes:
        raise FidReadAnalysisError(f"evidence file exceeds bounded size: {path.name} ({size} > {max_bytes})")
    rows = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if line_no > max_lines:
                    raise FidReadAnalysisError(f"too many evidence lines: {path.name}")
                if len(line.encode("utf-8")) > MAX_JSONL_LINE_BYTES:
                    raise FidReadAnalysisError(f"evidence line too large: {path.name}:{line_no}")
                if not line.strip():
                    raise FidReadAnalysisError(f"blank evidence line: {path.name}:{line_no}")
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise FidReadAnalysisError(f"JSONL evidence must contain objects: {path.name}:{line_no}")
                rows.append(row)
    except FidReadAnalysisError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FidReadAnalysisError(f"invalid JSONL evidence {path.name}: {type(exc).__name__}") from exc
    return rows


def _metric(values) -> dict:
    clean = []
    for value in values:
        if type(value) is bool or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        clean.append(value)
    if not clean:
        return {"count": 0, "min": None, "median": None, "max": None}
    return {
        "count": len(clean),
        "min": min(clean),
        "median": median(clean),
        "max": max(clean),
    }


def _phase_dict(value, name: str, issues: list[dict]) -> dict | None:
    if not isinstance(value, dict):
        issues.append({"severity": "invalid", "id": name, "reason": "missing_or_not_object"})
        return None
    result = {}
    for phase in PHASES:
        item = value.get(phase)
        if type(item) is not int or item < 0:
            issues.append({"severity": "invalid", "id": name, "reason": f"invalid_{phase}"})
            return None
        result[phase] = item
    return result


def _counter_summary(sidecar: dict, status: dict, issues: list[dict]) -> dict | None:
    counters = sidecar.get("phase_counters")
    if not isinstance(counters, dict):
        issues.append({"severity": "limited", "id": "phase_counters", "reason": "final_counters_missing"})
        return None
    trade = _phase_dict(counters.get("trade_callbacks_by_phase"), "trade_callbacks_by_phase", issues)
    quote = _phase_dict(counters.get("quote_callbacks_by_phase"), "quote_callbacks_by_phase", issues)
    calls = _phase_dict(counters.get("fid_calls_by_phase"), "fid_calls_by_phase", issues)
    attempts = _phase_dict(counters.get("fid_attempts_by_phase"), "fid_attempts_by_phase", issues)
    failures = _phase_dict(counters.get("fid_read_failures_by_phase"), "fid_read_failures_by_phase", issues)
    if None in (trade, quote, calls, attempts, failures):
        return None

    per_phase = {}
    for phase in PHASES:
        expected_no_failure = (
            trade[phase] * len(active_fids_for("주식체결", phase))
            + quote[phase] * len(active_fids_for("주식호가잔량", phase))
        )
        per_phase[phase] = {
            "trade_callbacks": trade[phase],
            "quote_callbacks": quote[phase],
            "callback_attempts": trade[phase] + quote[phase],
            "fid_attempts": attempts[phase],
            "fid_calls_completed": calls[phase],
            "fid_read_failures": failures[phase],
            "expected_completed_if_no_read_failure": expected_no_failure,
        }
        if attempts[phase] != calls[phase] + failures[phase]:
            issues.append({
                "severity": "invalid",
                "id": "attempt_completion_accounting",
                "reason": phase,
            })
        if failures[phase] == 0 and calls[phase] != expected_no_failure:
            issues.append({
                "severity": "invalid",
                "id": "fid_call_policy_accounting",
                "reason": phase,
            })

    if counters.get("subscribed_at_set") is not True:
        issues.append({"severity": "invalid", "id": "subscribed_at_set", "reason": str(counters.get("subscribed_at_set"))})
    if counters.get("a2_includes_shutdown_tail") is not False:
        issues.append({"severity": "invalid", "id": "a2_tail_contract", "reason": str(counters.get("a2_includes_shutdown_tail"))})
    if counters.get("post_90s_phase") != PHASE_POST:
        issues.append({"severity": "invalid", "id": "post_phase_contract", "reason": str(counters.get("post_90s_phase"))})
    if counters.get("counter_scope") != "callback_read_attempts_not_accepted_or_committed":
        issues.append({"severity": "invalid", "id": "counter_scope", "reason": str(counters.get("counter_scope"))})

    if counters.get("diagnostic_error") not in (None, ""):
        issues.append({
            "severity": "diagnostic",
            "id": "diagnostic_error",
            "reason": str(counters.get("diagnostic_error"))[:256],
        })
    if sum(failures.values()):
        issues.append({
            "severity": "diagnostic",
            "id": "fid_read_failures",
            "reason": str(sum(failures.values())),
        })

    for phase in ANALYSIS_PHASES:
        if trade[phase] + quote[phase] == 0:
            issues.append({
                "severity": "limited",
                "id": "analysis_phase_callbacks",
                "reason": f"{phase}_has_no_callbacks",
            })

    accepted = status.get("snapshot", {}).get("accepted_callbacks")
    sidecar_callbacks = sum(trade.values()) + sum(quote.values())
    clean_callback_accounting = (
        type(accepted) is int
        and status.get("snapshot", {}).get("dropped_callbacks") == 0
        and not sum(failures.values())
    )
    if clean_callback_accounting and accepted != sidecar_callbacks:
        issues.append({
            "severity": "invalid",
            "id": "callback_accounting",
            "reason": f"status={accepted},sidecar={sidecar_callbacks}",
        })
    if clean_callback_accounting:
        received_trade = status.get("received_trade_callbacks")
        received_quote = status.get("received_quote_callbacks")
        if received_trade != sum(trade.values()):
            issues.append({
                "severity": "invalid",
                "id": "trade_callback_accounting",
                "reason": f"status={received_trade},sidecar={sum(trade.values())}",
            })
        if received_quote != sum(quote.values()):
            issues.append({
                "severity": "invalid",
                "id": "quote_callback_accounting",
                "reason": f"status={received_quote},sidecar={sum(quote.values())}",
            })

    return {
        "per_phase": per_phase,
        "subscribed_at_set": counters.get("subscribed_at_set"),
        "counter_scope": counters.get("counter_scope"),
        "a2_includes_shutdown_tail": counters.get("a2_includes_shutdown_tail"),
        "post_90s_phase": counters.get("post_90s_phase"),
    }


def _telemetry_summary(rows: list[dict] | None, *, session_id: str | None,
                       code_revision: str | None, issues: list[dict]) -> dict:
    if not rows:
        issues.append({"severity": "limited", "id": "telemetry", "reason": "missing_or_empty"})
        return {"present": False, "sample_count": 0, "by_phase": {}}

    header = rows[0]
    if header.get("schema") != "capture_telemetry_v1" or header.get("kind") != "header":
        issues.append({"severity": "invalid", "id": "telemetry_header", "reason": "schema_or_kind"})
    if session_id and header.get("session_id") != session_id:
        issues.append({"severity": "invalid", "id": "telemetry_session", "reason": "session_id_mismatch"})
    if code_revision and header.get("code_revision") != code_revision:
        issues.append({"severity": "invalid", "id": "telemetry_revision", "reason": "code_revision_mismatch"})

    samples = []
    dropped_max = 0
    for row in rows[1:]:
        if row.get("schema") != "capture_telemetry_v1" or row.get("kind") != "sample_batch":
            issues.append({"severity": "invalid", "id": "telemetry_batch", "reason": "schema_or_kind"})
            continue
        if session_id and row.get("session_id") != session_id:
            issues.append({"severity": "invalid", "id": "telemetry_batch_session", "reason": "session_id_mismatch"})
        dropped = row.get("diagnostic_samples_dropped")
        if type(dropped) is int and dropped >= 0:
            dropped_max = max(dropped_max, dropped)
        batch_samples = row.get("samples")
        if not isinstance(batch_samples, list):
            issues.append({"severity": "invalid", "id": "telemetry_samples", "reason": "not_list"})
            continue
        for sample in batch_samples:
            if isinstance(sample, dict):
                samples.append(sample)
            else:
                issues.append({"severity": "invalid", "id": "telemetry_sample", "reason": "not_object"})

    by_phase = {}
    unlabeled = 0
    for phase in PHASES:
        phase_samples = [s for s in samples if s.get("diagnostic_phase") == phase]
        codes = {s.get("code") for s in phase_samples if isinstance(s.get("code"), str)}
        clock_values = []
        for sample in phase_samples:
            comparison = sample.get("clock_comparison")
            if isinstance(comparison, dict) and comparison.get("status") == "unverified_clock_difference":
                clock_values.append(comparison.get("difference_seconds"))
        by_phase[phase] = {
            "samples": len(phase_samples),
            "trade_samples": sum(s.get("real_type") == "주식체결" for s in phase_samples),
            "quote_samples": sum(s.get("real_type") == "주식호가잔량" for s in phase_samples),
            "sampled_code_count": len(codes),
            "processing_ns": _metric(s.get("processing_ns") for s in phase_samples),
            "fid_read_ns": _metric(s.get("fid_read_ns") for s in phase_samples),
            "queue_submit_ns": _metric(s.get("queue_submit_ns") for s in phase_samples),
            "fid_call_count": _metric(s.get("fid_call_count") for s in phase_samples),
            "clock_difference_seconds": _metric(clock_values),
        }
    unlabeled = sum(s.get("diagnostic_phase") not in PHASES for s in samples)
    if unlabeled:
        issues.append({"severity": "limited", "id": "telemetry_phase", "reason": f"{unlabeled}_unlabeled_samples"})
    for phase in ANALYSIS_PHASES:
        if by_phase[phase]["samples"] == 0:
            issues.append({"severity": "limited", "id": "telemetry_coverage", "reason": f"{phase}_has_no_samples"})
    if dropped_max:
        issues.append({"severity": "limited", "id": "telemetry_dropped", "reason": str(dropped_max)})

    return {
        "present": True,
        "records": len(rows),
        "sample_count": len(samples),
        "diagnostic_samples_dropped_max": dropped_max,
        "unlabeled_samples": unlabeled,
        "by_phase": by_phase,
        "clock_difference_note": (
            "sample-only signed clock difference under same-KST-day assumption; "
            "not network latency and not a feed-wide distribution"
        ),
    }


def _resource_summary(rows: list[dict] | None, *, pid: int | None, issues: list[dict]) -> dict:
    if not rows:
        issues.append({"severity": "limited", "id": "resource_history", "reason": "missing_or_empty"})
        return {"present": False, "samples": 0}
    mismatched = 0
    malformed = 0
    for row in rows:
        if pid is not None and row.get("pid") != pid:
            mismatched += 1
        metrics = ("working_set", "peak_working_set", "commit", "peak_commit")
        if (
            type(row.get("pid")) is not int
            or not isinstance(row.get("at_utc"), str)
            or not row.get("at_utc")
            or any(type(row.get(name)) is not int or row.get(name) < 0 for name in metrics)
        ):
            malformed += 1
    if mismatched:
        issues.append({"severity": "invalid", "id": "resource_pid", "reason": f"{mismatched}_mismatched_rows"})
    if malformed:
        issues.append({"severity": "invalid", "id": "resource_shape", "reason": f"{malformed}_malformed_rows"})
    return {
        "present": True,
        "samples": len(rows),
        "first_at_utc": rows[0].get("at_utc"),
        "last_at_utc": rows[-1].get("at_utc"),
        "max_working_set": max((r.get("working_set") for r in rows if type(r.get("working_set")) is int), default=None),
        "max_peak_working_set": max((r.get("peak_working_set") for r in rows if type(r.get("peak_working_set")) is int), default=None),
        "max_commit": max((r.get("commit") for r in rows if type(r.get("commit")) is int), default=None),
        "max_peak_commit": max((r.get("peak_commit") for r in rows if type(r.get("peak_commit")) is int), default=None),
    }


def _result(issues: list[dict]) -> str:
    severities = {item["severity"] for item in issues}
    if "invalid" in severities:
        return RESULT_INVALID
    if "incomplete" in severities:
        return RESULT_INCOMPLETE
    if "diagnostic" in severities:
        return RESULT_DIAGNOSTIC_ERROR
    if "limited" in severities:
        return RESULT_LIMITED
    return RESULT_READY


def analyze_fid_read_ab_session(session_dir: Path, *, expected_revision: str | None = None) -> dict:
    directory = Path(session_dir).resolve()
    if not directory.is_dir():
        raise FidReadAnalysisError(f"session directory not found: {directory}")

    status = _read_json(directory / STATUS_NAME, MAX_STATUS_BYTES)
    sidecar = _read_json(directory / SIDECAR_NAME, MAX_SIDECAR_BYTES)
    telemetry_rows = _read_jsonl(
        directory / TELEMETRY_NAME,
        max_bytes=MAX_TELEMETRY_BYTES,
        max_lines=MAX_TELEMETRY_LINES,
        required=False,
    )
    resource_rows = _read_jsonl(
        directory / RESOURCE_NAME,
        max_bytes=MAX_RESOURCE_BYTES,
        max_lines=MAX_RESOURCE_LINES,
        required=False,
    )

    issues: list[dict] = []
    identity = status.get("identity")
    snapshot = status.get("snapshot")
    if not isinstance(identity, dict):
        issues.append({"severity": "invalid", "id": "status_identity", "reason": "missing_or_not_object"})
        identity = {}
    if not isinstance(snapshot, dict):
        issues.append({"severity": "invalid", "id": "status_snapshot", "reason": "missing_or_not_object"})
        snapshot = {}

    session_id = identity.get("session_id")
    revision = identity.get("code_revision")
    pid = identity.get("pid") if type(identity.get("pid")) is int else None

    if status.get("status_schema") != "raw_capture_status_v1":
        issues.append({"severity": "invalid", "id": "status_schema", "reason": str(status.get("status_schema"))})
    if identity.get("feed_scope") != FEED_SCOPE_DIAGNOSTIC or snapshot.get("feed_scope") != FEED_SCOPE_DIAGNOSTIC:
        issues.append({"severity": "invalid", "id": "feed_scope", "reason": "diagnostic_scope_required"})
    if identity.get("server") != "mock":
        issues.append({"severity": "invalid", "id": "server", "reason": str(identity.get("server"))})
    if identity.get("python_bits") != 32:
        issues.append({"severity": "invalid", "id": "python_bits", "reason": str(identity.get("python_bits"))})
    if snapshot.get("session_id") != session_id:
        issues.append({"severity": "invalid", "id": "snapshot_session", "reason": "session_id_mismatch"})
    if snapshot.get("dataset_path") != identity.get("dataset_path"):
        issues.append({"severity": "invalid", "id": "dataset_identity", "reason": "path_mismatch"})
    if expected_revision is not None and revision != expected_revision:
        issues.append({"severity": "invalid", "id": "expected_revision", "reason": f"observed={revision}"})

    if sidecar.get("schema") != "fid_read_ab_test_v2":
        issues.append({"severity": "invalid", "id": "sidecar_schema", "reason": str(sidecar.get("schema"))})
    if sidecar.get("diagnostic_only") is not True or sidecar.get("research_eligible") is not False:
        issues.append({"severity": "invalid", "id": "sidecar_research_flags", "reason": "diagnostic_only_required"})
    if sidecar.get("feed_scope") != FEED_SCOPE_DIAGNOSTIC:
        issues.append({"severity": "invalid", "id": "sidecar_scope", "reason": str(sidecar.get("feed_scope"))})
    if revision and sidecar.get("code_revision") != revision:
        issues.append({"severity": "invalid", "id": "sidecar_revision", "reason": "code_revision_mismatch"})
    if sidecar.get("intended_server") != "mock":
        issues.append({"severity": "invalid", "id": "sidecar_server", "reason": str(sidecar.get("intended_server"))})
    if sidecar.get("strict_phase_windows") is not True or sidecar.get("post_90s_callbacks_separated") is not True:
        issues.append({"severity": "invalid", "id": "phase_window_contract", "reason": "v2_phase_contract_required"})
    phase_meta = sidecar.get("phases")
    if not isinstance(phase_meta, dict):
        issues.append({"severity": "invalid", "id": "phase_metadata", "reason": "missing_or_not_object"})
    else:
        expected_windows = {
            PHASE_PRE: False,
            PHASE_A1: True,
            PHASE_B: True,
            PHASE_A2: True,
            PHASE_POST: False,
        }
        for phase, analysis_window in expected_windows.items():
            meta = phase_meta.get(phase)
            if not isinstance(meta, dict) or meta.get("analysis_window") is not analysis_window:
                issues.append({"severity": "invalid", "id": "phase_metadata", "reason": phase})
        a2_meta = phase_meta.get(PHASE_A2, {})
        post_meta = phase_meta.get(PHASE_POST, {})
        if a2_meta.get("includes_shutdown_tail_after_nominal_end") is not False:
            issues.append({"severity": "invalid", "id": "a2_phase_metadata", "reason": "tail_flag"})
        if post_meta.get("fid_set") != "FULL":
            issues.append({"severity": "invalid", "id": "post_phase_metadata", "reason": "FULL_required"})

    clean_capture_checks = [
        ("state", snapshot.get("state") == "closed", snapshot.get("state")),
        ("accepting", snapshot.get("accepting") is False, snapshot.get("accepting")),
        ("writer_closed", snapshot.get("writer_closed") is True, snapshot.get("writer_closed")),
        ("pending_callbacks", snapshot.get("pending_callbacks") == 0, snapshot.get("pending_callbacks")),
        ("queued", snapshot.get("queued") == 0, snapshot.get("queued")),
        ("in_flight", snapshot.get("in_flight") == 0, snapshot.get("in_flight")),
        ("dropped_callbacks", snapshot.get("dropped_callbacks") == 0, snapshot.get("dropped_callbacks")),
        (
            "callback_commit_accounting",
            type(snapshot.get("accepted_callbacks")) is int
            and snapshot.get("committed_callbacks") == snapshot.get("accepted_callbacks"),
            f"accepted={snapshot.get('accepted_callbacks')},committed={snapshot.get('committed_callbacks')}",
        ),
        ("capture_error", status.get("error") in (None, ""), status.get("error")),
        ("finalization", isinstance(snapshot.get("finalization"), dict), snapshot.get("finalization")),
    ]
    for name, ok, observed in clean_capture_checks:
        if not ok:
            issues.append({"severity": "incomplete", "id": name, "reason": str(observed)[:256]})

    finalization = snapshot.get("finalization")
    if isinstance(finalization, dict) and type(snapshot.get("committed_seq")) is int:
        if finalization.get("final_seq") != snapshot.get("committed_seq"):
            issues.append({
                "severity": "invalid",
                "id": "finalization_accounting",
                "reason": f"final={finalization.get('final_seq')},committed={snapshot.get('committed_seq')}",
            })

    counters = _counter_summary(sidecar, status, issues)
    telemetry = _telemetry_summary(
        telemetry_rows, session_id=session_id, code_revision=revision, issues=issues
    )
    if counters is not None and telemetry.get("present"):
        for phase in PHASES:
            counter_phase = counters["per_phase"][phase]
            telemetry_phase = telemetry["by_phase"][phase]
            if telemetry_phase["trade_samples"] > counter_phase["trade_callbacks"]:
                issues.append({"severity": "invalid", "id": "telemetry_trade_accounting", "reason": phase})
            if telemetry_phase["quote_samples"] > counter_phase["quote_callbacks"]:
                issues.append({"severity": "invalid", "id": "telemetry_quote_accounting", "reason": phase})
    resources = _resource_summary(resource_rows, pid=pid, issues=issues)

    return {
        "schema": ANALYSIS_SCHEMA,
        "result": _result(issues),
        "session_directory": str(directory),
        "identity": {
            "session_id": session_id,
            "code_revision": revision,
            "pid": pid,
            "python_bits": identity.get("python_bits"),
            "server": identity.get("server"),
            "feed_scope": identity.get("feed_scope"),
            "dataset_path_claim": identity.get("dataset_path"),
        },
        "capture": {
            "state": snapshot.get("state"),
            "writer_closed": snapshot.get("writer_closed"),
            "pending_callbacks": snapshot.get("pending_callbacks"),
            "dropped_callbacks": snapshot.get("dropped_callbacks"),
            "accepted_callbacks": snapshot.get("accepted_callbacks"),
            "committed_callbacks": snapshot.get("committed_callbacks"),
            "committed_seq": snapshot.get("committed_seq"),
            "error": status.get("error"),
            "finalization_present": isinstance(snapshot.get("finalization"), dict),
        },
        "diagnostic": {
            "sidecar_schema": sidecar.get("schema"),
            "diagnostic_only": sidecar.get("diagnostic_only"),
            "research_eligible": sidecar.get("research_eligible"),
            "shutdown_reason": sidecar.get("shutdown_reason"),
            "counters": counters,
        },
        "telemetry": telemetry,
        "resources": resources,
        "issues": issues,
        "notes": [
            "raw database was not opened or scanned",
            "result is diagnostic evidence readiness, not research eligibility",
            "phase callback counts divided by 30 are not certified service rates",
            "ESSENTIAL changes COM reads plus string/dict/JSON/storage/worker costs",
            "clock difference is not network latency",
            "no Qt/COM/GIL/native root cause is inferred",
        ],
    }
