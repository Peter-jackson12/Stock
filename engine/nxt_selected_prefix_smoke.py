"""Replay an eligible selected-prefix v2 overlay through the NXT portfolio path.

This is a pipeline-smoke adapter, not strategy-performance certification.
It accepts only raw_v2_selected_prefix_qualification_v2 reports using the
explicit unknown-direction recent-window quarantine policy. The underlying
strict raw_v2_prefix_qualification_v1 report may remain smoke-ineligible.

The runner re-opens the exact same sidecar-free sealed raw source and, in one
bounded prefix scan, re-verifies the strict prefix evidence and recomputes the
selected policy result. Selected clean ticks are forwarded, selected
unknown-direction quarantine pairs forward their trade after exact pair
confirmation, and selected one-sided zero-quote pairs are withheld.

The whole raw tail remains unassessed and raw_identity_verified remains false.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from collector.raw_archive import reject_sqlite_sidecars, sealed_source
from collector.raw_v2 import CaptureControl
from collector.raw_v2_prefix_qualification import (
    SCHEMA as STRICT_PREFIX_SCHEMA,
    _cutoff_times,
    _decode_record,
    _read_manifest,
)
from collector.raw_v2_qualification import _Diagnostics, _file_identity, _require_local_ntfs
from collector.raw_v2_selected_prefix_qualification import (
    MAX_REPORT_BYTES,
    SCHEMA as SELECTED_PREFIX_SCHEMA,
    _code_provenance as _selected_overlay_code_provenance,
    _json_loads,
    _load_strict_report,
    _same_path,
)
from collector.selected_instrument_smoke_policy import (
    SelectedInstrumentSmokePolicy,
    _tick_issues,
)
from engine.nxt_portfolio_research import run_nxt_portfolio
from engine.tick_ordering import ReceiveOrderReplay
from strategies.nxt_breakout.direction_window import (
    POLICY as QUARANTINE_UNKNOWN_DIRECTION_POLICY,
)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code_identity():
    root = Path(__file__).resolve().parents[1]
    names = (
        "engine/nxt_selected_prefix_smoke.py",
        "engine/nxt_portfolio_research.py",
        "collector/raw_v2_selected_prefix_qualification.py",
        "collector/selected_instrument_smoke_policy.py",
        "strategies/nxt_breakout/direction_window.py",
        "strategies/nxt_breakout/tick_research.py",
    )
    return {name: _sha256_file(root / name) for name in names}


def _load_selected_report(path):
    path = Path(path).resolve(strict=True)
    with path.open("rb") as stream:
        data = stream.read(MAX_REPORT_BYTES + 1)
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("selected prefix report exceeds 8 MiB")
    report = _json_loads(data)
    if not isinstance(report, dict) or report.get("schema") != SELECTED_PREFIX_SCHEMA:
        raise ValueError("raw_v2_selected_prefix_qualification_v2 report required")
    if report.get("status") != "completed" or report.get("stream_error") is not None:
        raise ValueError("selected prefix report must be completed without stream error")
    if report.get("selected_prefix_structure_verified") is not True:
        raise ValueError("selected prefix structure is not verified")
    if report.get("selected_smoke_quality_eligible") is not True:
        raise ValueError("selected prefix is not eligible for smoke replay")

    performance = report.get("performance_research_eligibility")
    if (
        not isinstance(performance, dict)
        or performance.get("assessed") is not False
        or performance.get("eligible") is not None
    ):
        raise ValueError("selected smoke report must not claim performance research eligibility")

    input_record = report.get("input")
    if not isinstance(input_record, dict):
        raise ValueError("selected prefix input object required")
    instruments = input_record.get("selected_instruments")
    if (
        not isinstance(instruments, dict)
        or not instruments
        or any(
            not isinstance(code, str)
            or not code
            or not isinstance(venue, str)
            or not venue
            for code, venue in instruments.items()
        )
    ):
        raise ValueError("selected instruments mapping required")
    if input_record.get("unknown_direction_policy") != QUARANTINE_UNKNOWN_DIRECTION_POLICY:
        raise ValueError("selected smoke requires explicit quarantine unknown-direction policy")
    if not isinstance(input_record.get("raw_path"), str):
        raise ValueError("selected raw path required")
    if not isinstance(input_record.get("strict_prefix_report_path"), str):
        raise ValueError("strict prefix report path required")
    strict_sha = input_record.get("strict_prefix_report_sha256")
    if (
        not isinstance(strict_sha, str)
        or len(strict_sha) != 64
        or any(ch not in "0123456789abcdef" for ch in strict_sha.lower())
    ):
        raise ValueError("valid strict prefix report SHA-256 required")

    strict = report.get("strict_prefix")
    if (
        not isinstance(strict, dict)
        or strict.get("schema") != STRICT_PREFIX_SCHEMA
        or strict.get("whole_prefix_quality_upgraded") is not False
    ):
        raise ValueError("valid preserved strict-prefix state required")
    scope = strict.get("scope")
    if (
        not isinstance(scope, dict)
        or scope.get("kind") != "bounded_prefix_only"
        or scope.get("tail_scanned") is not False
        or scope.get("whole_stream_assessed") is not False
        or scope.get("whole_stream_research_eligible") is not None
    ):
        raise ValueError("valid selected bounded-prefix scope required")

    revalidation = report.get("strict_revalidation")
    required_revalidation = (
        "prefix_digest_match",
        "consumed_record_count_match",
        "boundary_sentinel_match",
        "counts_match",
        "quality_diagnostics_match",
    )
    if (
        not isinstance(revalidation, dict)
        or any(revalidation.get(name) is not True for name in required_revalidation)
    ):
        raise ValueError("selected report strict revalidation must be fully true")

    policy_result = report.get("selected_policy_result")
    if not isinstance(policy_result, dict):
        raise ValueError("selected policy result required")
    if policy_result.get("selected_smoke_quality_eligible") is not True:
        raise ValueError("selected policy result is not eligible")
    if policy_result.get("unknown_direction_policy") != QUARANTINE_UNKNOWN_DIRECTION_POLICY:
        raise ValueError("selected policy result does not match quarantine policy")
    if policy_result.get("selected_instruments") != dict(sorted(instruments.items())):
        raise ValueError("selected policy instruments mismatch")
    quarantine = policy_result.get("quarantine")
    contracts = policy_result.get("contracts")
    if (
        not isinstance(quarantine, dict)
        or quarantine.get("unknown_direction_immediate_entry_permission_granted") is not False
        or not isinstance(contracts, dict)
        or contracts.get("selected_unknown_direction_requires_strategy_window_quarantine") is not True
        or contracts.get("selected_unknown_direction_requires_explicit_policy") is not True
        or contracts.get("selected_trade_direction_quarantine_exception_is_bounded") is not True
    ):
        raise ValueError("selected quarantine/strategy alignment contract required")

    report_contracts = report.get("contracts")
    if (
        not isinstance(report_contracts, dict)
        or report_contracts.get("strict_prefix_qualification_unchanged") is not True
        or report_contracts.get("whole_prefix_research_quality_upgraded") is not False
        or report_contracts.get("selected_overlay_is_opt_in") is not True
        or report_contracts.get("selected_unknown_direction_policy_explicit") is not True
    ):
        raise ValueError("selected overlay contracts required")

    current_code = _selected_overlay_code_provenance()
    if report.get("code_provenance") != current_code:
        raise ValueError("selected overlay code provenance no longer matches current policy code")

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


def _selected_v2_events(path, *, selected_report, strict_report, instruments):
    """Yield selected strategy inputs while revalidating strict + selected evidence."""
    path = Path(path).absolute()
    _require_local_ntfs(path)
    if not path.is_file():
        raise ValueError("regular raw database file required")
    reject_sqlite_sidecars(path)

    expected_policy_result = selected_report["selected_policy_result"]
    expected_forwarded = (
        expected_policy_result["counts"]["selected_clean_ticks"]
        + expected_policy_result["quarantine"]["selected_unknown_direction_pairs"]
    )

    with sealed_source(path) as stream:
        _require_local_ntfs(path)
        reject_sqlite_sidecars(path)
        before = os.fstat(stream.fileno())
        if _file_identity(before) != _file_identity(path.stat()):
            raise ValueError("raw source path does not match the held file handle")

        conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)
        cursor = None
        try:
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-8192")
            manifest = _read_manifest(conn)
            if manifest != strict_report["input"]["manifest"]:
                raise ValueError("raw manifest no longer matches strict prefix report")
            if manifest != selected_report["input"]["manifest"]:
                raise ValueError("raw manifest no longer matches selected prefix report")

            _, cutoff_utc = _cutoff_times(
                manifest,
                strict_report["scope"]["end_market_second_exclusive"],
            )
            replay = ReceiveOrderReplay(
                source=manifest["source"],
                session_id=manifest["session_id"],
                max_quote_age_ns=0,
            )
            diagnostics = _Diagnostics()
            selected_policy = SelectedInstrumentSmokePolicy(
                instruments,
                unknown_direction_policy=QUARANTINE_UNKNOWN_DIRECTION_POLICY,
            )
            digest = hashlib.sha256()
            expected_seq = 1
            last_ns = 0
            last_utc = None
            sentinel = None
            consumed = 0
            forwarded = 0
            selected_forwarded = Counter()
            pending_selected = None

            cursor = conn.execute("SELECT seq,payload FROM events ORDER BY seq")
            for seq, payload in cursor:
                envelope, event, received_utc = _decode_record(
                    seq, payload, manifest, expected_seq, last_ns
                )
                if last_utc is not None and received_utc < last_utc:
                    raise ValueError("UTC receipt clock moved backwards before selected smoke boundary")
                last_utc = received_utc
                consumed += 1
                expected_seq += 1
                last_ns = event.received_ns

                if received_utc >= cutoff_utc:
                    sentinel = _sentinel(event, envelope)
                    break

                if not isinstance(event, CaptureControl):
                    replay.accept(event)
                diagnostics.accept(envelope)
                digest.update(payload.encode("utf-8") + b"\n")

                before_unknown = selected_policy.selected_unknown_direction_pairs
                before_zero = selected_policy.selected_zero_quote_pairs
                before_disqualifying = selected_policy.selected_disqualifying_pairs
                before_unpaired = selected_policy.unpaired_issue_ticks
                before_unsafe = selected_policy.unsafe_controls
                had_pending_selected = pending_selected is not None

                selected_policy.accept(envelope)

                if had_pending_selected:
                    if (
                        isinstance(event, CaptureControl)
                        and selected_policy.selected_unknown_direction_pairs
                        == before_unknown + 1
                    ):
                        forwarded += 1
                        selected_forwarded[pending_selected.code] += 1
                        yield pending_selected
                    elif (
                        isinstance(event, CaptureControl)
                        and selected_policy.selected_zero_quote_pairs
                        == before_zero + 1
                    ):
                        pass
                    else:
                        raise ValueError(
                            "selected issue no longer matches eligible quarantine pair contract"
                        )
                    pending_selected = None
                    continue

                if (
                    selected_policy.selected_disqualifying_pairs > before_disqualifying
                    or selected_policy.unpaired_issue_ticks > before_unpaired
                    or selected_policy.unsafe_controls > before_unsafe
                ):
                    raise ValueError("selected policy became disqualifying during smoke replay")

                if isinstance(event, CaptureControl):
                    continue

                if instruments.get(event.code) != event.venue:
                    continue
                issues = _tick_issues(envelope)
                if issues is None:
                    raise ValueError("selected tick has malformed normalized issue metadata")
                if issues:
                    pending_selected = event
                    continue

                forwarded += 1
                selected_forwarded[event.code] += 1
                yield event

            if sentinel is None:
                raise ValueError("capture no longer reaches selected smoke boundary")

            policy_result = selected_policy.result()
            if digest.hexdigest() != strict_report["prefix_event_sha256"]:
                raise ValueError("prefix digest no longer matches strict qualification report")
            if consumed != strict_report["records_consumed_through_sentinel"]:
                raise ValueError("prefix consumed-record count changed")
            if sentinel != strict_report["scope"]["boundary_sentinel"]:
                raise ValueError("prefix boundary sentinel changed")
            detail = diagnostics.result()
            if detail["counts"] != strict_report["counts"]:
                raise ValueError("strict prefix record counts changed")
            if detail["quality_diagnostics"] != strict_report["quality_diagnostics"]:
                raise ValueError("strict prefix quality diagnostics changed")
            if policy_result != expected_policy_result:
                raise ValueError("selected policy result no longer matches v2 overlay report")
            if forwarded != expected_forwarded:
                raise ValueError("selected strategy input count no longer matches v2 overlay")
            expected_by_code = Counter()
            for code in instruments:
                # The current report is aggregate-only by code. A single declared
                # instrument is required below, so this is still exact.
                expected_by_code[code] = expected_forwarded
            if selected_forwarded != expected_by_code:
                raise ValueError("selected forwarded instrument counts changed")
        finally:
            if cursor is not None:
                cursor.close()
            conn.close()
            _require_local_ntfs(path)
            reject_sqlite_sidecars(path)
            after = os.fstat(stream.fileno())
            if (
                _file_identity(before) != _file_identity(after)
                or _file_identity(after) != _file_identity(path.stat())
            ):
                raise ValueError("raw source changed during selected prefix smoke replay")


def run_nxt_selected_prefix_smoke(
    raw_path,
    selected_prefix_report,
    *,
    output_root,
    simulator_config,
    quantity,
    exit_rule="fixed",
    cooldown_ns=10_000_000_000,
    params=None,
    dataset_label=None,
):
    """Run one NXT strategy from an eligible selected-prefix v2 overlay."""
    selected_path, selected_bytes, selected = _load_selected_report(selected_prefix_report)
    strict_path, strict_bytes, strict = _load_strict_report(
        selected["input"]["strict_prefix_report_path"]
    )
    if hashlib.sha256(strict_bytes).hexdigest() != selected["input"]["strict_prefix_report_sha256"]:
        raise ValueError("strict prefix report content no longer matches selected overlay")
    if selected["input"].get("strict_prefix_report_run_id") != strict.get("run_id"):
        raise ValueError("strict prefix report run id changed")
    if selected["strict_prefix"].get("prefix_event_sha256") != strict.get("prefix_event_sha256"):
        raise ValueError("selected overlay strict prefix digest backreference mismatch")
    if selected["strict_prefix"].get("scope") != strict.get("scope"):
        raise ValueError("selected overlay strict scope backreference mismatch")
    if selected["strict_prefix"].get("smoke_backtest_eligible") != strict.get("smoke_backtest_eligible"):
        raise ValueError("selected overlay strict eligibility backreference mismatch")

    raw_path = Path(raw_path).absolute()
    if not _same_path(raw_path, selected["input"]["raw_path"]):
        raise ValueError("raw path must exactly match selected prefix source")
    if not _same_path(raw_path, strict["input"].get("path")):
        raise ValueError("raw path must exactly match strict prefix source")

    config = dict(simulator_config)
    manifest = strict["input"]["manifest"]
    for name in ("source", "session_id"):
        supplied = config.get(name)
        expected = manifest[name]
        if supplied is not None and supplied != expected:
            raise ValueError(f"simulator {name} conflicts with selected prefix manifest")
        config[name] = expected

    instruments = config.get("instruments")
    selected_instruments = selected["input"]["selected_instruments"]
    if not isinstance(instruments, dict) or not instruments:
        raise ValueError("explicit selected smoke instruments mapping required")
    if dict(sorted(instruments.items())) != dict(sorted(selected_instruments.items())):
        raise ValueError("simulator instruments must exactly match selected overlay instruments")
    if len(instruments) != 1:
        raise ValueError("selected-prefix v2 smoke currently requires exactly one instrument")

    sentinel_ns = strict["scope"]["boundary_sentinel"]["received_ns"]
    provenance = {
        "kind": "raw_v2_selected_prefix_smoke_v1",
        "purpose": "selected_prefix_smoke_backtest_only",
        "raw_path": str(raw_path),
        "raw_identity_verified": False,
        "selected_prefix_report_path": str(selected_path),
        "selected_prefix_report_sha256": hashlib.sha256(selected_bytes).hexdigest(),
        "selected_prefix_report_run_id": selected.get("run_id"),
        "selected_prefix_report_schema": selected.get("schema"),
        "strict_prefix_report_path": str(strict_path),
        "strict_prefix_report_sha256": hashlib.sha256(strict_bytes).hexdigest(),
        "strict_prefix_report_run_id": strict.get("run_id"),
        "strict_smoke_backtest_eligible": strict.get("smoke_backtest_eligible"),
        "prefix_event_sha256": strict["prefix_event_sha256"],
        "end_market_second_exclusive": strict["scope"]["end_market_second_exclusive"],
        "boundary_sentinel": strict["scope"]["boundary_sentinel"],
        "whole_stream_assessed": False,
        "performance_research_assessed": False,
        "selected_instruments": dict(sorted(instruments.items())),
        "unknown_direction_policy": QUARANTINE_UNKNOWN_DIRECTION_POLICY,
        "selected_policy_result": selected["selected_policy_result"],
        "adapter_code_sha256": _code_identity(),
    }
    label = dataset_label or f"raw-v2-selected-prefix-smoke:{selected.get('run_id', 'unknown')}"
    events = _selected_v2_events(
        raw_path,
        selected_report=selected,
        strict_report=strict,
        instruments=instruments,
    )
    return run_nxt_portfolio(
        events,
        output_root=output_root,
        dataset_label=label,
        simulator_config=config,
        close_ns=sentinel_ns,
        quantity=quantity,
        exit_rule=exit_rule,
        cooldown_ns=cooldown_ns,
        params=params,
        unknown_direction_policy=QUARANTINE_UNKNOWN_DIRECTION_POLICY,
        input_provenance=provenance,
    )
