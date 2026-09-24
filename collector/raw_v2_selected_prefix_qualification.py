"""Selected-instrument quality overlay for an existing strict bounded prefix report.

This is an opt-in companion to raw_v2_prefix_qualification_v1. It never
changes or replaces the strict report. Instead it re-opens the exact same sealed
raw source, re-verifies the strict prefix digest/counts/quality/sentinel, and
evaluates SelectedInstrumentSmokePolicy over the same ordered prefix.

The resulting selected quality decision is for pipeline smoke only. It does not
upgrade whole-prefix research quality, strategy performance eligibility, or
execution permission for quarantined one-sided quotes.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from collector.raw_archive import reject_sqlite_sidecars, sealed_source
from collector.raw_v2 import CaptureControl
from collector.raw_v2_prefix_qualification import (
    SCHEMA as STRICT_PREFIX_SCHEMA,
    _cutoff_times,
    _decode_record,
    _read_manifest,
)
from collector.raw_v2_qualification import _Diagnostics, _file_identity, _require_local_ntfs
from collector.research_input_policy import research_exclusion_reason
from collector.selected_instrument_smoke_policy import (
    STRICT_UNKNOWN_DIRECTION_POLICY,
    SelectedInstrumentSmokePolicy,
    _validate_unknown_direction_policy,
)
from engine.tick_ordering import ReceiveOrderReplay


SCHEMA = "raw_v2_selected_prefix_qualification_v2"
MAX_REPORT_BYTES = 8 * 1024 * 1024


def _json_loads(data):
    def reject_constant(value):
        raise ValueError(f"nonfinite JSON value: {value}")
    return json.loads(data, parse_constant=reject_constant)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _code_provenance():
    root = Path(__file__).resolve().parents[1]
    names = (
        "collector/raw_v2_selected_prefix_qualification.py",
        "collector/raw_v2_prefix_qualification.py",
        "collector/raw_v2_qualification.py",
        "collector/selected_instrument_smoke_policy.py",
        "collector/zero_quote_policy_experiment.py",
        "strategies/nxt_breakout/direction_window.py",
        "strategies/nxt_breakout/tick_research.py",
        "collector/raw_v2.py",
    )
    return {name: _sha256_file(root / name) for name in names}


def _same_path(left, right):
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def _load_strict_report(path):
    path = Path(path).resolve(strict=True)
    with path.open("rb") as stream:
        data = stream.read(MAX_REPORT_BYTES + 1)
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("strict prefix report exceeds 8 MiB")
    report = _json_loads(data)
    if not isinstance(report, dict) or report.get("schema") != STRICT_PREFIX_SCHEMA:
        raise ValueError("raw_v2_prefix_qualification_v1 report required")
    if report.get("status") != "completed" or report.get("stream_error") is not None:
        raise ValueError("strict prefix report must be a completed structure scan")
    if report.get("prefix_structure_verified") is not True:
        raise ValueError("strict prefix structure must already be verified")
    performance = report.get("performance_research_eligibility")
    if (
        not isinstance(performance, dict)
        or performance.get("assessed") is not False
        or performance.get("eligible") is not None
    ):
        raise ValueError("strict prefix performance research must remain unassessed")
    scope = report.get("scope")
    if (
        not isinstance(scope, dict)
        or scope.get("kind") != "bounded_prefix_only"
        or scope.get("tail_scanned") is not False
        or scope.get("whole_stream_assessed") is not False
        or scope.get("whole_stream_research_eligible") is not None
    ):
        raise ValueError("invalid strict bounded-prefix scope")
    input_record = report.get("input")
    if not isinstance(input_record, dict) or not isinstance(input_record.get("manifest"), dict):
        raise ValueError("strict prefix input manifest required")
    digest = report.get("prefix_event_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest.lower())
    ):
        raise ValueError("valid strict prefix digest required")
    cutoff = scope.get("end_market_second_exclusive")
    if type(cutoff) is not int or not 0 < cutoff < 86_400:
        raise ValueError("valid strict prefix cutoff required")
    sentinel = scope.get("boundary_sentinel")
    if (
        not isinstance(sentinel, dict)
        or type(sentinel.get("seq")) is not int
        or sentinel["seq"] < 1
        or type(sentinel.get("received_ns")) is not int
        or sentinel["received_ns"] <= 0
    ):
        raise ValueError("valid strict prefix sentinel required")
    if type(report.get("records_consumed_through_sentinel")) is not int:
        raise ValueError("strict consumed-record count required")
    if not isinstance(report.get("counts"), dict) or not isinstance(report.get("quality_diagnostics"), dict):
        raise ValueError("strict counts/quality diagnostics required")
    return path, data, report


def _sentinel(event, envelope):
    return {
        "seq": event.seq,
        "received_ns": event.received_ns,
        "received_at_utc": envelope["received_at_utc"],
        "kind": "control" if isinstance(event, CaptureControl) else event.kind,
        "code": None if isinstance(event, CaptureControl) else event.code,
        "control_type": event.control_type if isinstance(event, CaptureControl) else None,
    }


def _scan_selected_overlay(
    raw_path,
    strict_report,
    instruments,
    unknown_direction_policy,
):
    raw_path = Path(raw_path).absolute()
    filesystem = _require_local_ntfs(raw_path)
    if not raw_path.is_file():
        raise ValueError("regular raw database file required")
    reject_sqlite_sidecars(raw_path)

    proof = {
        "path": str(raw_path),
        "filesystem": filesystem,
        "write_delete_handles_excluded": False,
        "sidecars_absent_before": True,
        "sidecars_absent_after": False,
        "source_stat_unchanged": False,
    }

    with sealed_source(raw_path) as stream:
        proof["write_delete_handles_excluded"] = True
        _require_local_ntfs(raw_path)
        reject_sqlite_sidecars(raw_path)
        before = os.fstat(stream.fileno())
        if _file_identity(before) != _file_identity(raw_path.stat()):
            raise ValueError("raw source path does not match held file handle")

        conn = sqlite3.connect(raw_path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)
        cursor = None
        try:
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-8192")
            manifest = _read_manifest(conn)
            if manifest != strict_report["input"]["manifest"]:
                raise ValueError("raw manifest no longer matches strict prefix report")

            _, cutoff_utc = _cutoff_times(
                manifest,
                strict_report["scope"]["end_market_second_exclusive"],
            )
            replay = ReceiveOrderReplay(
                source=manifest["source"],
                session_id=manifest["session_id"],
                max_quote_age_ns=0,
            )
            strict_diagnostics = _Diagnostics()
            selected_policy = SelectedInstrumentSmokePolicy(
                instruments,
                unknown_direction_policy=unknown_direction_policy,
            )
            digest = hashlib.sha256()
            expected_seq = 1
            last_ns = 0
            last_utc = None
            consumed = 0
            sentinel = None

            cursor = conn.execute("SELECT seq,payload FROM events ORDER BY seq")
            for seq, payload in cursor:
                envelope, event, received_utc = _decode_record(
                    seq, payload, manifest, expected_seq, last_ns
                )
                if last_utc is not None and received_utc < last_utc:
                    raise ValueError("UTC receipt clock moved backwards before selected prefix boundary")
                last_utc = received_utc
                consumed += 1
                expected_seq += 1
                last_ns = event.received_ns
                if not isinstance(event, CaptureControl):
                    replay.accept(event)

                if received_utc >= cutoff_utc:
                    sentinel = _sentinel(event, envelope)
                    break

                strict_diagnostics.accept(envelope)
                selected_policy.accept(envelope)
                digest.update(payload.encode("utf-8") + b"\n")

            if sentinel is None:
                raise ValueError("capture no longer reaches strict prefix boundary")

            strict_detail = strict_diagnostics.result()
            selected_result = selected_policy.result()
            if digest.hexdigest() != strict_report["prefix_event_sha256"]:
                raise ValueError("prefix digest no longer matches strict report")
            if consumed != strict_report["records_consumed_through_sentinel"]:
                raise ValueError("prefix consumed-record count changed")
            if sentinel != strict_report["scope"]["boundary_sentinel"]:
                raise ValueError("prefix boundary sentinel changed")
            if strict_detail["counts"] != strict_report["counts"]:
                raise ValueError("strict prefix counts changed")
            if strict_detail["quality_diagnostics"] != strict_report["quality_diagnostics"]:
                raise ValueError("strict prefix quality diagnostics changed")

            return {
                "manifest": manifest,
                "proof": proof,
                "selected_policy_result": selected_result,
                "strict_revalidation": {
                    "prefix_digest_match": True,
                    "consumed_record_count_match": True,
                    "boundary_sentinel_match": True,
                    "counts_match": True,
                    "quality_diagnostics_match": True,
                },
            }
        finally:
            if cursor is not None:
                cursor.close()
            conn.close()
            _require_local_ntfs(raw_path)
            reject_sqlite_sidecars(raw_path)
            proof["sidecars_absent_after"] = True
            after = os.fstat(stream.fileno())
            if (
                _file_identity(before) != _file_identity(after)
                or _file_identity(after) != _file_identity(raw_path.stat())
            ):
                raise ValueError("raw source changed during selected prefix overlay")
            proof["source_stat_unchanged"] = True
            proof["size_bytes"] = after.st_size
            proof["mtime_ns"] = after.st_mtime_ns


def qualify_selected_prefix(
    raw_path,
    strict_prefix_report,
    *,
    output_root,
    instruments,
    unknown_direction_policy=STRICT_UNKNOWN_DIRECTION_POLICY,
):
    if (
        not isinstance(instruments, dict)
        or not instruments
        or any(
            not isinstance(code, str)
            or not code.strip()
            or not isinstance(venue, str)
            or not venue.strip()
            for code, venue in instruments.items()
        )
    ):
        raise ValueError("nonempty CODE=VENUE mapping required")
    unknown_direction_policy = _validate_unknown_direction_policy(
        unknown_direction_policy
    )

    started = time.monotonic()
    output_root = Path(output_root).resolve()
    run_id = uuid.uuid4().hex
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    overlay = None
    strict_path = None
    strict_bytes = None
    strict_report = None
    error = None
    raw_path = Path(raw_path).absolute()

    try:
        strict_path, strict_bytes, strict_report = _load_strict_report(strict_prefix_report)
        reported_raw = strict_report["input"].get("path")
        if not isinstance(reported_raw, str) or not _same_path(raw_path, reported_raw):
            raise ValueError("raw path must exactly match strict prefix report source")
        overlay = _scan_selected_overlay(
            raw_path,
            strict_report,
            instruments,
            unknown_direction_policy,
        )
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError) as exc:
        error = f"{type(exc).__name__}: {exc}"

    structure_verified = overlay is not None and error is None
    selected_result = overlay["selected_policy_result"] if overlay else None
    selected_eligible = bool(
        structure_verified
        and selected_result
        and selected_result["selected_smoke_quality_eligible"]
    )
    reasons = []
    if not structure_verified:
        reasons.append("selected prefix overlay structure/revalidation failed")
    elif not selected_result["selected_smoke_quality_eligible"]:
        reasons.append("selected-instrument smoke-quality policy is disqualifying")

    manifest = overlay["manifest"] if overlay else (
        strict_report.get("input", {}).get("manifest")
        if isinstance(strict_report, dict) else None
    )
    exclusion = research_exclusion_reason(manifest)
    if exclusion is not None:
        selected_eligible = False
        reasons.append(exclusion)

    report = {
        "schema": SCHEMA,
        "run_id": run_id,
        "status": "completed" if structure_verified else "failed",
        "input": {
            "raw_path": str(raw_path),
            "strict_prefix_report_path": str(strict_path) if strict_path else str(strict_prefix_report),
            "strict_prefix_report_sha256": hashlib.sha256(strict_bytes).hexdigest() if strict_bytes else None,
            "strict_prefix_report_run_id": strict_report.get("run_id") if isinstance(strict_report, dict) else None,
            "manifest": manifest,
            "selected_instruments": dict(sorted(instruments.items())),
            "unknown_direction_policy": unknown_direction_policy,
        },
        "strict_prefix": {
            "schema": strict_report.get("schema") if isinstance(strict_report, dict) else None,
            "smoke_backtest_eligible": strict_report.get("smoke_backtest_eligible") if isinstance(strict_report, dict) else None,
            "prefix_event_sha256": strict_report.get("prefix_event_sha256") if isinstance(strict_report, dict) else None,
            "scope": strict_report.get("scope") if isinstance(strict_report, dict) else None,
            "whole_prefix_quality_upgraded": False,
        },
        "selected_prefix_structure_verified": structure_verified,
        "selected_smoke_quality_eligible": selected_eligible,
        "selected_smoke_quality_eligibility": {
            "eligible": selected_eligible,
            "reasons": [] if selected_eligible else reasons,
        },
        "selected_policy_result": selected_result,
        "strict_revalidation": overlay["strict_revalidation"] if overlay else None,
        "original_preservation": overlay["proof"] if overlay else {
            "path": str(raw_path),
            "selected_overlay_reader_not_established": True,
        },
        "stream_error": error,
        "code_provenance": _code_provenance(),
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "performance_research_eligibility": {
            "assessed": False,
            "eligible": None,
            "reason": "selected-instrument pipeline-smoke quality does not certify whole-prefix or strategy performance research",
        },
        "contracts": {
            "strict_prefix_report_sha256_backreference_recorded": True,
            "strict_prefix_qualification_unchanged": True,
            "whole_prefix_research_quality_upgraded": False,
            "nxt_smoke_gate_unchanged": True,
            "selected_overlay_is_opt_in": True,
            "selected_unknown_direction_policy_explicit": True,
        },
        "limitations": [
            "This overlay does not alter the strict raw_v2_prefix_qualification_v1 result.",
            "Selected quality is only a candidate gate for a future selected-instrument pipeline smoke.",
            "Unselected issue pairs may be ignored only under the explicit selected policy whitelist and exact pairing contract.",
            "Selected unknown-direction quarantine requires the explicit strategy-aligned policy and the bounded raw-shape contract.",
            "Unsafe controls and unpaired/mismatched issues remain global failures.",
            "Selected one-sided zero quotes remain non-executable.",
            "The raw tail after the strict prefix sentinel remains unassessed.",
        ],
    }

    result_path = run_dir / "result.json"
    with result_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    return result_path
