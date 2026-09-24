"""Bounded post-run summary for one FID A-B-A diagnostic session.

Reads only small session evidence files. It never opens or scans the raw database,
does not certify research eligibility, and does not infer a native/root cause.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import re
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
from control_tower.lifecycle import Finalization, ProcessIdentity

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

DURATION_METRICS = ("processing_ns", "fid_read_ns", "queue_submit_ns")
SNAPSHOT_COUNTERS = (
    "accepted_callbacks", "committed_callbacks", "queued", "in_flight",
    "pending_callbacks", "dropped_callbacks", "committed_seq",
)
_SHA1 = re.compile(r"[0-9a-f]{40}")


class FidReadAnalysisError(ValueError):
    pass


def _bounded_text(path: Path, limit: int) -> str:
    """Read at most ``limit + 1`` bytes. The stat check is only a fast path.

    The bound limits bytes held in memory; it is not a guarantee on OS I/O time.
    Input files are opened read-only and never modified.
    """
    path = Path(path)
    if not path.is_file():
        raise FidReadAnalysisError(f"required evidence file missing: {path.name}")
    size = path.stat().st_size
    if size > limit:
        raise FidReadAnalysisError(f"evidence file exceeds bounded size: {path.name} ({size} > {limit})")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise FidReadAnalysisError(f"evidence file exceeds bounded size: {path.name} (>{limit} bytes read)")
    return data.decode("utf-8")


def _reject_constant(token):
    raise ValueError(f"non-finite JSON constant {token}")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def _strict_json(text: str):
    return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float,
                      object_pairs_hook=_unique_object)


def _read_json(path: Path, limit: int) -> dict:
    try:
        value = _strict_json(_bounded_text(path, limit))
    except FidReadAnalysisError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
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
    total = 0
    try:
        with path.open("rb") as stream:
            line_no = 0
            while True:
                # Per-line and cumulative byte bounds apply to bytes actually read.
                line = stream.readline(MAX_JSONL_LINE_BYTES + 1)
                if not line:
                    break
                line_no += 1
                total += len(line)
                if line_no > max_lines:
                    raise FidReadAnalysisError(f"too many evidence lines: {path.name}")
                if len(line) > MAX_JSONL_LINE_BYTES:
                    raise FidReadAnalysisError(f"evidence line too large: {path.name}:{line_no}")
                if total > max_bytes:
                    raise FidReadAnalysisError(
                        f"evidence file exceeds bounded size: {path.name} (>{max_bytes} bytes read)")
                if not line.endswith(b"\n"):
                    raise FidReadAnalysisError(f"truncated evidence line (no newline): {path.name}:{line_no}")
                text = line.decode("utf-8")
                if not text.strip():
                    raise FidReadAnalysisError(f"blank evidence line: {path.name}:{line_no}")
                row = _strict_json(text)
                if not isinstance(row, dict):
                    raise FidReadAnalysisError(f"JSONL evidence must contain objects: {path.name}:{line_no}")
                rows.append(row)
    except FidReadAnalysisError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise FidReadAnalysisError(f"invalid JSONL evidence {path.name}: {type(exc).__name__}") from exc
    return rows


def _is_utc(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() == timedelta(0)
    except ValueError:
        return False


def _metric(values, *, nonnegative: bool = True, invalid: list | None = None) -> dict:
    """Summarize present values. Absent (None) values are skipped; malformed ones are counted."""
    clean = []
    for value in values:
        if value is None:
            continue
        if type(value) is bool or not isinstance(value, (int, float)) \
                or (isinstance(value, float) and not math.isfinite(value)) \
                or (nonnegative and value < 0):
            if invalid is not None:
                invalid.append(value)
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

    snapshot = status.get("snapshot") if isinstance(status.get("snapshot"), dict) else {}
    accepted = snapshot.get("accepted_callbacks")
    sidecar_callbacks = sum(trade.values()) + sum(quote.values())
    clean_callback_accounting = (
        type(accepted) is int
        and snapshot.get("dropped_callbacks") == 0
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


def _sample_metrics(samples: list[dict], invalid: list | None = None) -> dict:
    codes = {s.get("code") for s in samples if isinstance(s.get("code"), str)}
    clock_values = []
    for sample in samples:
        comparison = sample.get("clock_comparison")
        if isinstance(comparison, dict) and comparison.get("status") == "unverified_clock_difference":
            clock_values.append(comparison.get("difference_seconds"))
    summary = {"samples": len(samples), "sampled_code_count": len(codes)}
    for name in DURATION_METRICS:
        summary[name] = _metric((s.get(name) for s in samples), invalid=invalid)
    summary["fid_call_count"] = _metric((s.get("fid_call_count") for s in samples), invalid=invalid)
    summary["clock_difference_seconds"] = _metric(clock_values, nonnegative=False)
    return summary


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

    # fid_call_count must be an integer; _sample_metrics flags bool/negative/non-finite values.
    invalid_values = [s["fid_call_count"] for s in samples if isinstance(s.get("fid_call_count"), float)]
    _sample_metrics(samples, invalid_values)
    if invalid_values:
        issues.append({
            "severity": "invalid",
            "id": "telemetry_metric_value",
            "reason": f"{len(invalid_values)}_negative_nonfinite_or_nonnumeric_values",
        })

    by_phase = {}
    by_phase_real_type = {}
    unlabeled = 0
    for phase in PHASES:
        phase_samples = [s for s in samples if s.get("diagnostic_phase") == phase]
        phase_summary = _sample_metrics(phase_samples)
        phase_summary["trade_samples"] = sum(s.get("real_type") == "주식체결" for s in phase_samples)
        phase_summary["quote_samples"] = sum(s.get("real_type") == "주식호가잔량" for s in phase_samples)
        by_phase[phase] = phase_summary
        by_phase_real_type[phase] = {}
        for real_type in ("주식체결", "주식호가잔량"):
            typed = [s for s in phase_samples if s.get("real_type") == real_type]
            by_phase_real_type[phase][real_type] = _sample_metrics(typed)
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
        "by_phase_real_type": by_phase_real_type,
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
            or not _is_utc(row.get("at_utc"))
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
    try:
        ProcessIdentity(**identity)
    except (TypeError, ValueError) as exc:
        issues.append({"severity": "invalid", "id": "process_identity", "reason": str(exc)[:256]})
    if not isinstance(revision, str) or _SHA1.fullmatch(revision) is None:
        issues.append({"severity": "invalid", "id": "code_revision", "reason": "full_40_hex_revision_required"})
    if not _is_utc(status.get("observed_at_utc")):
        issues.append({"severity": "invalid", "id": "status_observed_at_utc", "reason": "explicit_utc_required"})
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
    if sidecar.get("duration_is_shutdown_request_not_hard_cutoff") is not True:
        issues.append({"severity": "invalid", "id": "duration_contract", "reason": "shutdown_request_required"})
    if sidecar.get("pure_com_cost_experiment") is not False:
        issues.append({"severity": "invalid", "id": "cost_coupling_contract", "reason": "pure_com_must_be_false"})
    if sidecar.get("backlog_reset_between_phases") is not False:
        issues.append({"severity": "invalid", "id": "backlog_contract", "reason": "backlog_reset_must_be_false"})
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
        a2_meta = phase_meta.get(PHASE_A2) if isinstance(phase_meta.get(PHASE_A2), dict) else {}
        post_meta = phase_meta.get(PHASE_POST) if isinstance(phase_meta.get(PHASE_POST), dict) else {}
        if a2_meta.get("includes_shutdown_tail_after_nominal_end") is not False:
            issues.append({"severity": "invalid", "id": "a2_phase_metadata", "reason": "tail_flag"})
        if post_meta.get("fid_set") != "FULL":
            issues.append({"severity": "invalid", "id": "post_phase_metadata", "reason": "FULL_required"})

    def is_zero(value) -> bool:
        return type(value) is int and value == 0

    counters_ok = all(type(snapshot.get(key)) is int and snapshot.get(key) >= 0 for key in SNAPSHOT_COUNTERS)
    if not counters_ok:
        issues.append({"severity": "invalid", "id": "snapshot_counters",
                       "reason": "non_negative_integer_counters_required_bool_is_not_a_count"})
    clean_capture_checks = [
        ("state", snapshot.get("state") == "closed", snapshot.get("state")),
        ("accepting", snapshot.get("accepting") is False, snapshot.get("accepting")),
        ("writer_closed", snapshot.get("writer_closed") is True, snapshot.get("writer_closed")),
        ("pending_callbacks", is_zero(snapshot.get("pending_callbacks")), snapshot.get("pending_callbacks")),
        ("queued", is_zero(snapshot.get("queued")), snapshot.get("queued")),
        ("in_flight", is_zero(snapshot.get("in_flight")), snapshot.get("in_flight")),
        ("dropped_callbacks", is_zero(snapshot.get("dropped_callbacks")), snapshot.get("dropped_callbacks")),
        (
            "callback_commit_accounting",
            type(snapshot.get("accepted_callbacks")) is int
            and type(snapshot.get("committed_callbacks")) is int
            and snapshot.get("committed_callbacks") == snapshot.get("accepted_callbacks"),
            f"accepted={snapshot.get('accepted_callbacks')},committed={snapshot.get('committed_callbacks')}",
        ),
        ("capture_error", status.get("error") in (None, ""), status.get("error")),
        ("snapshot_error", snapshot.get("error") in (None, ""), snapshot.get("error")),
        ("finalization", isinstance(snapshot.get("finalization"), dict), snapshot.get("finalization")),
    ]
    for name, ok, observed in clean_capture_checks:
        if not ok:
            issues.append({"severity": "incomplete", "id": name, "reason": str(observed)[:256]})
    for scope, value in (("status", status.get("error")), ("snapshot", snapshot.get("error"))):
        if value is not None and not isinstance(value, str):
            issues.append({"severity": "invalid", "id": f"{scope}_error_type", "reason": type(value).__name__})
    if counters_ok and (
        snapshot["accepted_callbacks"] - snapshot["committed_callbacks"] != snapshot["pending_callbacks"]
        or snapshot["queued"] + snapshot["in_flight"] != snapshot["pending_callbacks"]
    ):
        issues.append({"severity": "incomplete", "id": "snapshot_accounting",
                       "reason": "pending_queue_in_flight_mismatch"})
    committed = snapshot.get("committed_callbacks")
    if type(committed) is int and committed > 0 and not _is_utc(snapshot.get("last_commit_at_utc")):
        issues.append({"severity": "invalid", "id": "last_commit_at_utc", "reason": "explicit_utc_required"})

    finalization = snapshot.get("finalization")
    if isinstance(finalization, dict):
        try:
            final = Finalization(**finalization)
        except (TypeError, ValueError) as exc:
            issues.append({"severity": "invalid", "id": "finalization_evidence", "reason": str(exc)[:256]})
        else:
            if type(snapshot.get("committed_seq")) is int and final.final_seq != snapshot.get("committed_seq"):
                issues.append({
                    "severity": "invalid",
                    "id": "finalization_accounting",
                    "reason": f"final={final.final_seq},committed={snapshot.get('committed_seq')}",
                })
            last_event = snapshot.get("last_event_ns")
            if last_event is not None and (type(last_event) is not int or final.close_ns <= last_event):
                issues.append({"severity": "invalid", "id": "finalization_close_ns",
                               "reason": f"close_ns={final.close_ns},last_event_ns={last_event}"})

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
            "READY is not a raw quality pass and does not prove the collector process has exited",
            "phase callback counts divided by 30 are not certified service rates",
            "ESSENTIAL changes COM reads plus string/dict/JSON/storage/worker costs",
            "clock difference is not network latency",
            "no Qt/COM/GIL/native root cause is inferred",
        ],
    }
