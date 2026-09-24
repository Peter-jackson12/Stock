"""Bounded prefix qualification for raw-v2 morning research.

This does NOT qualify the whole raw session. It verifies only the contiguous
prefix from seq=1 up to an exclusive KST market-second boundary and requires a
structurally valid record at/after that boundary as a capture-reached sentinel.

The reader is Windows/NTFS-only, rejects every SQLite sidecar, holds the existing
write/delete exclusion handle, and opens the main DB immutable. A later tail
failure may remain unscanned and must never be hidden by a prefix pass.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from collector.raw_archive import reject_sqlite_sidecars, sealed_source
from collector.raw_v2 import (
    CaptureControl,
    MAX_MANIFEST_BYTES,
    SCHEMA as RAW_SCHEMA,
    SUPPORTED_SCHEMAS,
    _common,
    _envelope_fields,
    _identity,
    _load_json,
)
from collector.raw_v2_qualification import _Diagnostics, _file_identity, _require_local_ntfs
from collector.research_input_policy import research_exclusion_reason
from engine.tick_ordering import OrderedTick, ReceiveOrderReplay


SCHEMA = "raw_v2_prefix_qualification_v1"
KST = timezone(timedelta(hours=9))
UTC = timezone.utc


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code_provenance():
    root = Path(__file__).resolve().parents[1]
    names = (
        "collector/raw_v2_prefix_qualification.py",
        "collector/raw_v2_qualification.py",
        "collector/raw_v2.py",
        "collector/raw_archive.py",
        "collector/research_input_policy.py",
    )
    return {name: _file_hash(root / name) for name in names}


def _cutoff_times(manifest, end_market_second):
    if type(end_market_second) is not int or not 0 < end_market_second < 86_400:
        raise ValueError("end_market_second must be an integer in 1..86399")
    market_day = date.fromisoformat(manifest["market_date"])
    midnight = datetime.combine(market_day, datetime_time(), tzinfo=KST)
    cutoff_kst = midnight + timedelta(seconds=end_market_second)
    return cutoff_kst, cutoff_kst.astimezone(UTC)


def _read_manifest(conn):
    rows = conn.execute(
        "SELECT substr(CAST(value AS BLOB),1,?) FROM metadata LIMIT 2",
        (MAX_MANIFEST_BYTES + 1,),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("exactly one manifest required")
    if rows[0][0] is None or len(rows[0][0]) > MAX_MANIFEST_BYTES:
        raise ValueError("raw manifest exceeds 64 KiB limit or is null")
    manifest = _load_json(rows[0][0])
    _identity(manifest)
    if manifest.get("schema") not in SUPPORTED_SCHEMAS:
        raise ValueError("unsupported raw-v2 schema")
    state = manifest.get("state")
    if state not in ("closed", "incomplete"):
        raise ValueError("raw manifest state must be closed or incomplete")
    if state == "closed":
        if type(manifest.get("close_ns")) is not int or manifest["close_ns"] <= 0:
            raise ValueError("invalid closed-session close boundary")
        if type(manifest.get("event_count")) is not int or manifest["event_count"] < 0:
            raise ValueError("invalid closed-session event count")
        digest = manifest.get("payload_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("invalid closed-session payload digest")
    return manifest


def _decode_record(seq, payload, manifest, expected_seq, last_ns):
    envelope = _load_json(payload)
    if not isinstance(envelope, dict):
        raise ValueError("record envelope object required")
    _envelope_fields(
        envelope.get("received_at_utc"),
        envelope.get("raw_fields"),
        envelope.get("exchange_ts_raw"),
        envelope.get("source_time_precision"),
    )
    fields = envelope.get("event")
    if not isinstance(fields, dict):
        raise ValueError("event object required")
    fields = dict(fields)
    for name in ("bid_sizes", "ask_sizes"):
        if fields.get(name) is not None:
            if not isinstance(fields[name], list):
                raise ValueError("depth quantities must be arrays or null")
            fields[name] = tuple(fields[name])
    if "control_type" in fields:
        if manifest["schema"] != RAW_SCHEMA:
            raise ValueError("control records require prototype 2")
        event = CaptureControl(**fields)
    else:
        event = OrderedTick(**fields)
    _common(event, manifest, expected_seq, last_ns)
    if event.seq != seq:
        raise ValueError("record/header sequence mismatch")
    if manifest["state"] == "closed" and event.received_ns >= manifest["close_ns"]:
        raise ValueError("record exceeds closed-session boundary")
    received_utc = datetime.fromisoformat(
        envelope["received_at_utc"].replace("Z", "+00:00")
    )
    if received_utc.utcoffset() != timedelta(0):
        raise ValueError("explicit UTC receipt timestamp required")
    envelope["event"] = event
    return envelope, event, received_utc


@contextmanager
def read_sealed_prefix(path, *, end_market_second):
    """Yield a bounded verified prefix description without scanning the tail.

    A valid sentinel at/after the exclusive cutoff is required. This proves the
    captured sequence reached the requested boundary, not that the later session
    or provider feed was complete.
    """
    path = Path(path).absolute()
    filesystem = _require_local_ntfs(path)
    if not path.is_file():
        raise ValueError("regular raw database file required")
    reject_sqlite_sidecars(path)
    proof = {
        "path": str(path),
        "filesystem": filesystem,
        "write_delete_handles_excluded": False,
        "sidecars_absent_before": True,
        "sidecars_absent_after": False,
        "source_stat_unchanged": False,
    }
    with sealed_source(path) as stream:
        proof["write_delete_handles_excluded"] = True
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
            cutoff_kst, cutoff_utc = _cutoff_times(manifest, end_market_second)
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
                    sentinel = {
                        "seq": event.seq,
                        "received_ns": event.received_ns,
                        "received_at_utc": envelope["received_at_utc"],
                        "kind": "control" if isinstance(event, CaptureControl) else event.kind,
                        "code": None if isinstance(event, CaptureControl) else event.code,
                        "control_type": event.control_type if isinstance(event, CaptureControl) else None,
                    }
                    break

                diagnostics.accept(envelope)
                digest.update(payload.encode("utf-8") + b"\n")

            if sentinel is None:
                raise ValueError("capture did not reach the requested prefix boundary")

            yield {
                "manifest": manifest,
                "proof": proof,
                "diagnostics": diagnostics,
                "prefix_event_sha256": digest.hexdigest(),
                "prefix_records": diagnostics.total,
                "records_consumed_through_sentinel": consumed,
                "boundary_sentinel": sentinel,
                "cutoff_kst": cutoff_kst.isoformat(),
                "cutoff_utc": cutoff_utc.isoformat(),
            }
        finally:
            if cursor is not None:
                cursor.close()
            conn.close()
            _require_local_ntfs(path)
            reject_sqlite_sidecars(path)
            proof["sidecars_absent_after"] = True
            after = os.fstat(stream.fileno())
            if (_file_identity(before) != _file_identity(after)
                    or _file_identity(after) != _file_identity(path.stat())):
                raise ValueError("raw source identity, size, or mtime changed during prefix qualification")
            proof["source_stat_unchanged"] = True
            proof["size_bytes"] = after.st_size
            proof["mtime_ns"] = after.st_mtime_ns


def qualify_raw_v2_prefix(path, *, output_root, expected_session_id,
                          closure_evidence, end_market_second):
    """Qualify only seq=1..exclusive cutoff; never promotes the whole session."""
    if (not isinstance(expected_session_id, str) or not expected_session_id.strip()
            or not isinstance(closure_evidence, str) or not closure_evidence.strip()
            or len(closure_evidence) > 8192):
        raise ValueError("expected session and bounded external closure evidence are required")
    if type(end_market_second) is not int or not 0 < end_market_second < 86_400:
        raise ValueError("end_market_second must be an integer in 1..86399")

    started = time.monotonic()
    inspected = datetime.now(UTC).isoformat()
    path = Path(path).absolute()
    output_root = Path(output_root).resolve()
    run_id = uuid.uuid4().hex
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    scan = None
    error = None
    try:
        with read_sealed_prefix(path, end_market_second=end_market_second) as value:
            scan = value
            if value["manifest"]["session_id"] != expected_session_id:
                raise ValueError("external closure evidence session does not match raw manifest")
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError) as exc:
        error = f"{type(exc).__name__}: {exc}"

    manifest = scan["manifest"] if scan else None
    proof = scan["proof"] if scan else {
        "path": str(path),
        "prefix_reader_not_established": True,
    }
    if scan:
        detail = scan["diagnostics"].result()
        counts = detail["counts"]
        quality = detail["quality_diagnostics"]
        quality_eligible = detail["research_eligible"]
        reasons = list(detail["research_eligibility"]["reasons"])
        if counts["tick_records"] == 0:
            quality_eligible = False
            reasons.append("no tick records before the requested cutoff")
        exclusion = research_exclusion_reason(manifest)
        if exclusion is not None:
            quality_eligible = False
            reasons.append(exclusion)
        structure_verified = error is None
        prefix_eligible = structure_verified and quality_eligible
    else:
        counts = {
            "raw_records": 0, "tick_records": 0, "trade_records": 0,
            "quote_records": 0, "control_records": 0, "control_by_type": {},
        }
        quality = {}
        reasons = ["prefix structure was not verified"]
        structure_verified = False
        prefix_eligible = False

    report = {
        "schema": SCHEMA,
        "run_id": run_id,
        "status": "completed" if structure_verified else "failed",
        "inspected_at_utc": inspected,
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "input": {
            "path": str(path),
            "manifest": manifest,
            "expected_session_id": expected_session_id,
            "closure_evidence": closure_evidence,
            "closure_evidence_is_operator_claim": True,
        },
        "scope": {
            "kind": "bounded_prefix_only",
            "starts_at_common_seq": 1,
            "end_market_second_exclusive": end_market_second,
            "end_kst": scan["cutoff_kst"] if scan else None,
            "end_utc": scan["cutoff_utc"] if scan else None,
            "boundary_sentinel": scan["boundary_sentinel"] if scan else None,
            "tail_scanned": False,
            "whole_stream_assessed": False,
            "whole_stream_research_eligible": None,
        },
        "prefix_structure_verified": structure_verified,
        "smoke_backtest_eligible": prefix_eligible,
        "smoke_backtest_eligibility": {
            "eligible": prefix_eligible,
            "reasons": [] if prefix_eligible else reasons,
        },
        "prefix_event_sha256": scan["prefix_event_sha256"] if scan else None,
        "records_consumed_through_sentinel": (
            scan["records_consumed_through_sentinel"] if scan else 0
        ),
        "counts": counts,
        "quality_diagnostics": quality,
        "stream_error": error,
        "original_preservation": proof,
        "code_provenance": _code_provenance(),
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "performance_research_eligibility": {
            "assessed": False,
            "eligible": None,
            "reason": "bounded prefix scan does not verify the producer whole-stream checksum or an external immutable file hash anchor",
        },
        "limitations": [
            "This result qualifies the bounded prefix only for smoke backtesting; it never qualifies the whole session or strategy performance research.",
            "The tail is intentionally not scanned, so later corruption, gaps, controls, or feed loss remain unassessed.",
            "No producer-stored prefix checksum exists; prefix_event_sha256 identifies the bytes read by this run.",
            "A boundary sentinel proves capture reached the cutoff, not provider completeness or exchange correctness.",
            "Closed-manifest event_count and whole-stream payload_sha256 are not verified by this prefix scan.",
            "An incomplete manifest may still have an eligible frozen prefix when external closure and the cutoff sentinel are present.",
            "Only local Windows NTFS with no SQLite sidecars is accepted.",
            "Source directory ancestors must remain quiescent; file sharing is not a namespace lock.",
        ],
    }
    result = run_dir / "result.json"
    with result.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    return result
