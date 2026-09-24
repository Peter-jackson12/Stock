"""Replay an already-qualified raw-v2 prefix through the NXT portfolio path.

This is a smoke-backtest adapter, not strategy-performance certification. It
requires a successful raw_v2_prefix_qualification_v1 report, re-opens the exact
same sidecar-free sealed source, replays only declared instruments before the
exclusive cutoff, and re-verifies the qualified prefix digest/counts/sentinel.

The whole raw tail remains unassessed and raw_identity_verified remains false.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from collector.raw_archive import reject_sqlite_sidecars, sealed_source
from collector.raw_v2 import CaptureControl
from collector.raw_v2_prefix_qualification import (
    SCHEMA as PREFIX_SCHEMA,
    _cutoff_times,
    _decode_record,
    _read_manifest,
)
from collector.raw_v2_qualification import _Diagnostics, _file_identity, _require_local_ntfs
from engine.nxt_portfolio_research import run_nxt_portfolio
from engine.tick_ordering import ReceiveOrderReplay


MAX_REPORT_BYTES = 8 * 1024 * 1024


def _json_loads(data):
    def reject_constant(value):
        raise ValueError(f"nonfinite JSON value: {value}")
    return json.loads(data, parse_constant=reject_constant)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code_identity():
    root = Path(__file__).resolve().parents[1]
    names = (
        "engine/nxt_prefix_smoke.py",
        "engine/nxt_portfolio_research.py",
        "collector/raw_v2_prefix_qualification.py",
    )
    return {name: _sha256_file(root / name) for name in names}


def _load_prefix_report(path):
    path = Path(path).resolve(strict=True)
    with path.open("rb") as stream:
        data = stream.read(MAX_REPORT_BYTES + 1)
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("prefix report exceeds 8 MiB")
    report = _json_loads(data)
    if not isinstance(report, dict) or report.get("schema") != PREFIX_SCHEMA:
        raise ValueError("raw_v2_prefix_qualification_v1 report required")
    if report.get("prefix_structure_verified") is not True:
        raise ValueError("prefix structure is not verified")
    if report.get("smoke_backtest_eligible") is not True:
        raise ValueError("prefix is not eligible for smoke backtesting")
    scope = report.get("scope")
    if (not isinstance(scope, dict)
            or scope.get("kind") != "bounded_prefix_only"
            or scope.get("tail_scanned") is not False
            or scope.get("whole_stream_assessed") is not False
            or scope.get("whole_stream_research_eligible") is not None):
        raise ValueError("invalid bounded-prefix scope")
    performance = report.get("performance_research_eligibility")
    if (not isinstance(performance, dict)
            or performance.get("assessed") is not False
            or performance.get("eligible") is not None):
        raise ValueError("smoke report must not claim performance research eligibility")
    input_record = report.get("input")
    if not isinstance(input_record, dict):
        raise ValueError("prefix report input object required")
    manifest = input_record.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("prefix report manifest required")
    digest = report.get("prefix_event_sha256")
    if (not isinstance(digest, str) or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest.lower())):
        raise ValueError("valid prefix digest required")
    sentinel = scope.get("boundary_sentinel")
    if (not isinstance(sentinel, dict)
            or type(sentinel.get("seq")) is not int or sentinel["seq"] < 1
            or type(sentinel.get("received_ns")) is not int or sentinel["received_ns"] <= 0):
        raise ValueError("valid positive boundary sentinel required")
    cutoff = scope.get("end_market_second_exclusive")
    if type(cutoff) is not int or not 0 < cutoff < 86_400:
        raise ValueError("valid prefix cutoff required")
    if type(report.get("records_consumed_through_sentinel")) is not int:
        raise ValueError("prefix consumed-record count required")
    if not isinstance(report.get("counts"), dict) or not isinstance(report.get("quality_diagnostics"), dict):
        raise ValueError("prefix diagnostics required")
    return path, data, report


def _same_path(left, right):
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def _sentinel(event, envelope):
    return {
        "seq": event.seq,
        "received_ns": event.received_ns,
        "received_at_utc": envelope["received_at_utc"],
        "kind": "control" if isinstance(event, CaptureControl) else event.kind,
        "code": None if isinstance(event, CaptureControl) else event.code,
        "control_type": event.control_type if isinstance(event, CaptureControl) else None,
    }


def _selected_prefix_events(path, *, report, instruments):
    """Yield declared instrument ticks and verify the whole qualified prefix on EOF."""
    path = Path(path).absolute()
    filesystem = _require_local_ntfs(path)
    if not path.is_file():
        raise ValueError("regular raw database file required")
    reject_sqlite_sidecars(path)

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
            if manifest != report["input"]["manifest"]:
                raise ValueError("raw manifest no longer matches prefix report")
            _, cutoff_utc = _cutoff_times(
                manifest, report["scope"]["end_market_second_exclusive"]
            )
            replay = ReceiveOrderReplay(
                source=manifest["source"],
                session_id=manifest["session_id"],
                max_quote_age_ns=0,
            )
            diagnostics = _Diagnostics()
            digest = hashlib.sha256()
            expected_seq = 1
            last_ns = 0
            last_utc = None
            sentinel = None
            consumed = 0
            selected_counts = Counter()
            cursor = conn.execute("SELECT seq,payload FROM events ORDER BY seq")
            for seq, payload in cursor:
                envelope, event, received_utc = _decode_record(
                    seq, payload, manifest, expected_seq, last_ns
                )
                if last_utc is not None and received_utc < last_utc:
                    raise ValueError("UTC receipt clock moved backwards before prefix boundary")
                last_utc = received_utc
                consumed += 1
                expected_seq += 1
                last_ns = event.received_ns
                if not isinstance(event, CaptureControl):
                    replay.accept(event)

                if received_utc >= cutoff_utc:
                    sentinel = _sentinel(event, envelope)
                    break

                diagnostics.accept(envelope)
                digest.update(payload.encode("utf-8") + b"\n")
                if not isinstance(event, CaptureControl) and instruments.get(event.code) == event.venue:
                    selected_counts[event.code] += 1
                    yield event

            if sentinel is None:
                raise ValueError("capture no longer reaches prefix boundary")
            if digest.hexdigest() != report["prefix_event_sha256"]:
                raise ValueError("prefix digest no longer matches qualification report")
            if consumed != report["records_consumed_through_sentinel"]:
                raise ValueError("prefix consumed-record count changed")
            if sentinel != report["scope"]["boundary_sentinel"]:
                raise ValueError("prefix boundary sentinel changed")
            detail = diagnostics.result()
            if detail["counts"] != report["counts"]:
                raise ValueError("prefix record counts changed")
            if detail["quality_diagnostics"] != report["quality_diagnostics"]:
                raise ValueError("prefix quality diagnostics changed")
            if not selected_counts:
                # Empty selection is a valid smoke outcome and will be recorded by
                # run_nxt_portfolio as completed_empty_input. Do not invent ticks.
                return
        finally:
            if cursor is not None:
                cursor.close()
            conn.close()
            _require_local_ntfs(path)
            reject_sqlite_sidecars(path)
            after = os.fstat(stream.fileno())
            if (_file_identity(before) != _file_identity(after)
                    or _file_identity(after) != _file_identity(path.stat())):
                raise ValueError("raw source changed during prefix smoke replay")


def run_nxt_prefix_smoke(raw_path, prefix_report, *, output_root, simulator_config,
                         quantity, exit_rule="fixed", cooldown_ns=10_000_000_000,
                         params=None, dataset_label=None):
    """Run one NXT strategy on a qualified bounded prefix only."""
    report_path, report_bytes, report = _load_prefix_report(prefix_report)
    raw_path = Path(raw_path).absolute()
    reported_raw = report["input"].get("path")
    if not isinstance(reported_raw, str) or not _same_path(raw_path, reported_raw):
        raise ValueError("raw path must exactly match the qualified prefix source")

    manifest = report["input"]["manifest"]
    config = dict(simulator_config)
    for name in ("source", "session_id"):
        supplied = config.get(name)
        expected = manifest[name]
        if supplied is not None and supplied != expected:
            raise ValueError(f"simulator {name} conflicts with prefix manifest")
        config[name] = expected
    instruments = config.get("instruments")
    if not isinstance(instruments, dict) or not instruments:
        raise ValueError("explicit smoke instruments mapping required")

    sentinel_ns = report["scope"]["boundary_sentinel"]["received_ns"]
    provenance = {
        "kind": "raw_v2_prefix_smoke_v1",
        "purpose": "smoke_backtest_only",
        "raw_path": str(raw_path),
        "raw_identity_verified": False,
        "prefix_report_path": str(report_path),
        "prefix_report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "prefix_report_run_id": report.get("run_id"),
        "prefix_event_sha256": report["prefix_event_sha256"],
        "end_market_second_exclusive": report["scope"]["end_market_second_exclusive"],
        "boundary_sentinel": report["scope"]["boundary_sentinel"],
        "whole_stream_assessed": False,
        "performance_research_assessed": False,
        "selected_instruments": dict(sorted(instruments.items())),
        "adapter_code_sha256": _code_identity(),
    }
    label = dataset_label or f"raw-v2-prefix-smoke:{report.get('run_id', 'unknown')}"
    events = _selected_prefix_events(raw_path, report=report, instruments=instruments)
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
        input_provenance=provenance,
    )
