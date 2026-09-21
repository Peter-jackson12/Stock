"""Strategy-free, filesystem-nonmutating qualification for closed raw-v2.

This path is deliberately Windows/NTFS-only.  It reuses the archive sharing
lock to exclude existing/future write handles, rejects every SQLite sidecar,
and only then opens the main database with ``immutable=1``.  Ambiguous storage
or closure state is rejected instead of risking a stale WAL-blind read.
"""
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from collector.raw_archive import reject_sqlite_sidecars, sealed_source
from collector.raw_v2 import CaptureControl, _read_raw_v2_connection


SCHEMA = "raw_v2_qualification_v1"
MAX_CATEGORY_KEYS = 10_000
MAX_EXAMPLE_ISSUES = 256
MAX_EXAMPLES_PER_ISSUE = 10
TOP_CODES = 20
SAFE_CONTROL_TYPES = {"session_start", "session_note"}


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _code_provenance():
    root = Path(__file__).resolve().parents[1]
    names = ("collector/raw_v2_qualification.py", "collector/raw_v2.py",
             "collector/raw_archive.py")
    return {name: _file_hash(root / name) for name in names}


def _require_local_ntfs(path):
    """Reject network, reparse, removable, and untested filesystem inputs."""
    if os.name != "nt":
        raise OSError("qualification requires Windows sharing-lock semantics")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_attributes = kernel.GetFileAttributesW
    get_attributes.argtypes = [wintypes.LPCWSTR]
    get_attributes.restype = wintypes.DWORD
    attrs = get_attributes(str(path))
    if attrs == 0xFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())
    if attrs & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        raise ValueError("reparse-point raw input is not supported")

    volume = ctypes.create_unicode_buffer(32768)
    get_volume_path = kernel.GetVolumePathNameW
    get_volume_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    if not get_volume_path(str(path), volume, len(volume)):
        raise ctypes.WinError(ctypes.get_last_error())
    if kernel.GetDriveTypeW(volume.value) != 3:  # DRIVE_FIXED
        raise ValueError("qualification requires a fixed local drive")

    filesystem = ctypes.create_unicode_buffer(256)
    get_volume_info = kernel.GetVolumeInformationW
    get_volume_info.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    if not get_volume_info(volume.value, None, 0, None, None, None,
                           filesystem, len(filesystem)):
        raise ctypes.WinError(ctypes.get_last_error())
    if filesystem.value.upper() != "NTFS":
        raise ValueError("qualification is verified only for local NTFS")
    return filesystem.value


@contextmanager
def read_sealed_raw_v2(path):
    """Read a closed main-file-only raw-v2 without creating SQLite sidecars.

    The immutable assumption is established by a local NTFS requirement, no
    pre-existing sidecars, and a Windows handle whose share mode denies any
    existing or future write/delete handle for the duration of the scan.
    """
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("regular raw database file required")
    filesystem = _require_local_ntfs(path)
    reject_sqlite_sidecars(path)
    proof = {"path": str(path), "filesystem": filesystem,
             "write_delete_handles_excluded": False,
             "sidecars_absent_before": True, "sidecars_absent_after": False,
             "source_stat_unchanged": False}
    with sealed_source(path) as stream:
        proof["write_delete_handles_excluded"] = True
        reject_sqlite_sidecars(path)
        before = os.fstat(stream.fileno())
        conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)
        try:
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-8192")
            with _read_raw_v2_connection(conn) as (manifest, rows):
                yield manifest, rows, proof
        finally:
            conn.close()
            reject_sqlite_sidecars(path)
            proof["sidecars_absent_after"] = True
            after = os.fstat(stream.fileno())
            before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if before_identity != after_identity:
                raise ValueError("raw source identity, size, or mtime changed during qualification")
            proof["source_stat_unchanged"] = True
            proof["size_bytes"] = after.st_size
            proof["mtime_ns"] = after.st_mtime_ns


class _Diagnostics:
    def __init__(self):
        self.total = self.ticks = self.trades = self.quotes = self.controls = 0
        self.control_types = Counter()
        self.normalized_issues = Counter()
        self.parse_error_reasons = Counter()
        self.logical_issues = Counter()
        self.problem_codes = Counter()
        self.affected_codes = set()
        self.time_buckets = Counter()
        self.examples = defaultdict(list)
        self.affected_tick_records = 0
        self.disqualifying_control_records = 0
        self.paired_parse_error_records = 0
        self.unpaired_parse_error_records = 0
        self.category_overflow = False
        self.code_overflow = False
        self.last_tick = None

    def _add(self, counter, key, count=1):
        key = str(key)
        if key in counter or len(counter) < MAX_CATEGORY_KEYS:
            counter[key] += count
        else:
            counter["__other_categories__"] += count
            self.category_overflow = True

    def _example(self, issue, envelope, event):
        issue = str(issue)
        if issue not in self.examples and len(self.examples) >= MAX_EXAMPLE_ISSUES:
            issue = "__other_categories__"
        bucket = self.examples[issue]
        if len(bucket) >= MAX_EXAMPLES_PER_ISSUE:
            return
        bucket.append({"seq": event.seq,
                       "code": getattr(event, "code", None) or event.details.get("code"),
                       "received_at_utc": envelope["received_at_utc"],
                       "exchange_ts_raw": envelope.get("exchange_ts_raw"),
                       "control_type": getattr(event, "control_type", None)})

    @staticmethod
    def _issues(value, malformed):
        if value is None:
            return []
        if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
            return value
        return [malformed]

    def _problem_location(self, envelope, code):
        if code:
            if code in self.affected_codes or len(self.affected_codes) < MAX_CATEGORY_KEYS:
                self.affected_codes.add(code)
                self._add(self.problem_codes, code)
            else:
                self.code_overflow = True
                self._add(self.problem_codes, "__other_codes__")
        clock = datetime.fromisoformat(envelope["received_at_utc"].replace("Z", "+00:00"))
        minute = clock.minute - clock.minute % 5
        key = clock.replace(minute=minute, second=0, microsecond=0).isoformat()
        self._add(self.time_buckets, key)

    def accept(self, envelope):
        event = envelope["event"]
        self.total += 1
        if isinstance(event, CaptureControl):
            self._control(envelope, event)
            return
        self.ticks += 1
        if event.kind == "trade":
            self.trades += 1
        else:
            self.quotes += 1
        issues = self._issues(envelope["raw_fields"].get("issues"),
                              "malformed_normalized_issues")
        self.last_tick = {"seq": event.seq, "received_ns": event.received_ns,
                          "code": event.code, "issues": tuple(issues)}
        if not issues:
            return
        self.affected_tick_records += 1
        self._problem_location(envelope, event.code)
        for issue in issues:
            self._add(self.normalized_issues, issue)
            self._add(self.logical_issues, issue)
            self._example(issue, envelope, event)

    def _control(self, envelope, event):
        self.controls += 1
        self._add(self.control_types, event.control_type)
        if event.control_type in SAFE_CONTROL_TYPES:
            self.last_tick = None
            return
        code = event.details.get("code") if isinstance(event.details, dict) else None
        if event.control_type == "parse_error":
            reasons = self._issues(event.details.get("issues") if isinstance(event.details, dict) else None,
                                   "unspecified_parse_error")
            for reason in reasons:
                self._add(self.parse_error_reasons, reason)
                self._example(f"control:parse_error:{reason}", envelope, event)
            paired = (self.last_tick is not None and self.last_tick["seq"] + 1 == event.seq
                      and self.last_tick["received_ns"] == event.received_ns
                      and self.last_tick["code"] == code
                      and self.last_tick["issues"] == tuple(reasons))
            if paired:
                self.paired_parse_error_records += 1
            else:
                self.unpaired_parse_error_records += 1
                self.disqualifying_control_records += 1
                self._problem_location(envelope, code)
                for reason in reasons:
                    self._add(self.logical_issues, reason)
        else:
            self.disqualifying_control_records += 1
            issue = f"control:{event.control_type}"
            self._add(self.logical_issues, issue)
            self._problem_location(envelope, code)
            self._example(issue, envelope, event)
        self.last_tick = None

    def result(self):
        def ordered(counter):
            return dict(sorted(counter.items()))
        research_eligible = not self.logical_issues
        return {
            "counts": {"raw_records": self.total, "tick_records": self.ticks,
                       "trade_records": self.trades, "quote_records": self.quotes,
                       "control_records": self.controls,
                       "control_by_type": ordered(self.control_types)},
            "quality_diagnostics": {
                "affected_tick_records": self.affected_tick_records,
                "disqualifying_control_records": self.disqualifying_control_records,
                "parse_error_records": self.control_types.get("parse_error", 0),
                "paired_parse_error_records": self.paired_parse_error_records,
                "unpaired_parse_error_records": self.unpaired_parse_error_records,
                "normalized_issue_counts": ordered(self.normalized_issues),
                "parse_error_reason_counts": ordered(self.parse_error_reasons),
                "logical_issue_counts": ordered(self.logical_issues),
                "affected_code_count": len(self.affected_codes),
                "affected_code_count_exact": not self.code_overflow,
                "top_problem_codes": [{"code": code, "count": count} for code, count in
                                      self.problem_codes.most_common(TOP_CODES)],
                "received_utc_5minute_buckets": ordered(self.time_buckets),
                "bounded_examples": dict(sorted(self.examples.items())),
                "category_overflow": self.category_overflow,
                "example_limits": {"issue_categories": MAX_EXAMPLE_ISSUES,
                                   "per_issue": MAX_EXAMPLES_PER_ISSUE}},
            "research_eligible": research_eligible,
            "research_eligibility": {
                "eligible": research_eligible,
                "reasons": [] if research_eligible else ["quality issues violate the current raw-v2 research contract"],
                "paired_tick_and_parse_error_are_one_logical_issue": True}}


def qualify_raw_v2(path, *, output_root, expected_session_id, closure_evidence):
    """Scan one raw-v2 into a new result directory and return result.json."""
    if (not isinstance(expected_session_id, str) or not expected_session_id.strip()
            or not isinstance(closure_evidence, str) or not closure_evidence.strip()
            or len(closure_evidence) > 8192):
        raise ValueError("expected session and bounded external closure evidence are required")
    started = time.monotonic()
    inspected = datetime.now(timezone.utc).isoformat()
    path = Path(path).resolve()
    output_root = Path(output_root).resolve()
    run_id = uuid.uuid4().hex
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    diagnostics = _Diagnostics()
    manifest = None
    proof = None
    error = None
    scan_complete = False
    try:
        with read_sealed_raw_v2(path) as (manifest, rows, proof):
            if manifest["session_id"] != expected_session_id:
                raise ValueError("external closure evidence session does not match raw manifest")
            for envelope in rows:
                diagnostics.accept(envelope)
        scan_complete = True
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError) as exc:
        error = f"{type(exc).__name__}: {exc}"

    detail = diagnostics.result()
    integrity = scan_complete and error is None
    if not integrity:
        detail["research_eligible"] = False
        detail["research_eligibility"] = {
            "eligible": False,
            "reasons": ["stream integrity was not fully verified"],
            "paired_tick_and_parse_error_are_one_logical_issue": True}
    report = {
        "schema": SCHEMA, "run_id": run_id,
        "status": "completed" if integrity else "failed",
        "inspected_at_utc": inspected,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "input": {"path": str(path), "manifest": manifest,
                  "expected_session_id": expected_session_id,
                  "closure_evidence": closure_evidence,
                  "closure_evidence_is_operator_claim": True},
        "code_provenance": _code_provenance(),
        "scan_complete": scan_complete,
        "stream_integrity_verified": integrity,
        "stream_integrity": {
            "verified": integrity, "error": error,
            "records_consumed": diagnostics.total,
            "reader_checks": ["manifest_identity", "contiguous_sequence",
                              "monotonic_receive_clock", "exclusive_close_boundary",
                              "event_count", "payload_sha256", "iterator_exhaustion"]},
        **detail,
        "original_preservation": proof or {
            "path": str(path), "qualification_reader_not_established": True},
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "limitations": [
            "The manifest closed state remains a producer claim; external closure evidence is required.",
            "Payload integrity does not prove provider completeness, venue identity, or feed accuracy.",
            "The main database byte hash is not recalculated by this streaming qualification.",
            "Only local Windows NTFS with no SQLite sidecars is accepted."],
    }
    result = run_dir / "result.json"
    with result.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    return result
