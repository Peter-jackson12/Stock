"""MWFD-03 Phase 0과 45-cell event cache의 단일 bounded raw pass."""
from __future__ import annotations

import argparse
from datetime import timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from collector.raw_archive import reject_sqlite_sidecars, sealed_source
from collector.raw_v2 import CaptureControl
from collector.raw_v2_prefix_qualification import _cutoff_times, _decode_record, _read_manifest
from collector.raw_v2_qualification import _file_identity, _require_local_ntfs
from engine.tick_ordering import OrderedTick
from research.fast_backtest.execution_depth import load_execution_depth_cache
from research.fast_backtest.runtime_probe import (
    ProbeEventCacheWriter,
    directory_bytes,
    load_candidate_family,
    load_probe_event_cache,
    load_probe_sample,
    sha256_file,
    write_json_create,
)


UTC = timezone.utc


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="MWFD-03 preregistered event materialization")
    value.add_argument("--raw", required=True)
    value.add_argument("--snapshot-result", required=True)
    value.add_argument("--prefix-report", required=True)
    value.add_argument("--mwfd02-root", required=True)
    value.add_argument("--candidate-summary", required=True)
    value.add_argument("--output-dir", required=True)
    value.add_argument("--code-revision", required=True)
    value.add_argument("--minimum-free-bytes", type=int, default=5_000_000_000)
    return value


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def safety(snapshot: Mapping, prefix: Mapping, raw: Path) -> None:
    if snapshot.get("status") != "snapshot_ready" or not snapshot.get("snapshot_ready_for_prefix_qualification"):
        raise ValueError("snapshot is not ready")
    cleanup = snapshot.get("working_cleanup", {})
    if not cleanup.get("main_readback_verified"):
        raise ValueError("working snapshot readback is not verified")
    if any(item.get("exists") for item in cleanup.get("sidecars_after_close", {}).values()):
        raise ValueError("snapshot working sidecars were not absent")
    if Path(snapshot["paths"]["working_main"]).name != raw.name:
        raise ValueError("snapshot/raw identity mismatch")
    if prefix.get("status") != "completed" or not prefix.get("prefix_structure_verified"):
        raise ValueError("bounded prefix structure is not verified")
    if Path(prefix["input"]["path"]).name != raw.name:
        raise ValueError("prefix/raw identity mismatch")
    if prefix["scope"].get("tail_scanned") is not False:
        raise ValueError("bounded prefix contract required")
    reject_sqlite_sidecars(raw)


def peak_working_set_bytes() -> int | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    ok = psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    return int(counters.PeakWorkingSetSize) if ok else None


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    raw = Path(args.raw).absolute()
    snapshot_path = Path(args.snapshot_result).resolve(strict=True)
    prefix_path = Path(args.prefix_report).resolve(strict=True)
    mwfd02 = Path(args.mwfd02_root).resolve(strict=True)
    output = Path(args.output_dir).resolve()
    snapshot, prefix = read_json(snapshot_path), read_json(prefix_path)
    safety(snapshot, prefix, raw)

    sample = load_probe_sample(mwfd02 / "probe_admission.json", mwfd02 / "market_inventory.json")
    family = load_candidate_family(args.candidate_summary)
    mwfd02_summary = read_json(mwfd02 / "summary.json")
    source_prefix_digest = mwfd02_summary["source"]["prefix_digest"]
    if source_prefix_digest != prefix["prefix_event_sha256"]:
        raise ValueError("MWFD-02/prefix source digest mismatch")
    depth_path = Path(mwfd02_summary["execution_depth_cache"]["path"])
    depth = load_execution_depth_cache(
        depth_path, expected_source_prefix_digest=source_prefix_digest, verify_payload=False
    )
    if depth.cache_id != mwfd02_summary["execution_depth_cache"]["cache_id"]:
        raise ValueError("MWFD-02 depth cache identity mismatch")
    free_before = shutil.disk_usage(output.parent).free
    if free_before < args.minimum_free_bytes:
        raise ValueError("insufficient output capacity")

    output.mkdir(parents=True, exist_ok=False)
    expected_work_units = len(sample.cells) * len(family.candidates)
    smoke_cells = [
        next(cell.cell_id for cell in sample.cells if cell.tier == tier)
        for tier in ("high", "medium", "low")
    ]
    admission = {
        "schema": "mwfd_03_phase0_admission_v1",
        "status": "ADMITTED",
        "sample": sample.manifest(),
        "candidate_family": family.manifest(),
        "cache": {
            "execution_depth_path": str(depth.cache_dir),
            "execution_depth_cache_id": depth.cache_id,
            "execution_depth_logical_digest": depth.logical_digest,
            "source_prefix_digest": source_prefix_digest,
        },
        "expected": {
            "cells": 45,
            "candidates": 663,
            "candidate_cell_work_units": expected_work_units,
            "smoke_cells": smoke_cells,
            "remaining_cells": 42,
        },
        "capacity": {"free_bytes_before": free_before, "minimum_free_bytes": args.minimum_free_bytes},
        "selection_used_pnl": False,
    }
    write_json_create(output / "phase0_admission.json", admission)
    run_manifest = {
        "schema": "mwfd_03_runtime_probe_v1",
        "status": "PHASE0_ADMITTED",
        "code_revision": args.code_revision,
        "trade_date": "2026-09-21",
        "venue_interpretation": "literal unknown; not KRX/NXT-specific",
        "source": {
            "raw_path": str(raw),
            "raw_size": raw.stat().st_size,
            "raw_mtime_ns": raw.stat().st_mtime_ns,
            "prefix_report": str(prefix_path),
            "prefix_digest": source_prefix_digest,
            "prefix_records": prefix["counts"]["raw_records"],
        },
        "sample": sample.manifest(),
        "candidate_family": family.manifest(),
        "shared_cache": admission["cache"],
        "account": {
            "cash": 1_000_000, "fee_rate": 0.001, "quantity": 1,
            "buy_latency_ns": 1_000_000_000, "sell_latency_ns": 1_000_000_000,
            "max_quote_age_ns": 2_000_000_000, "cooldown_ns": 10_000_000_000,
            "close_ns": prefix["scope"]["boundary_sentinel"]["received_ns"],
        },
        "prohibited_actions": {
            "production_exact": False, "parameter_or_threshold_tuning": False,
            "holdout_exploration": False, "ocx_login_live_order": False,
        },
    }
    write_json_create(output / "run_manifest.json", run_manifest)

    writer = ProbeEventCacheWriter(
        output / "event_cache", source_prefix_digest=source_prefix_digest, sample=sample
    )
    started_wall, started_cpu = time.perf_counter(), time.process_time()
    source_before = raw.stat()
    prefix_digest = hashlib.sha256()
    prefix_records = 0
    selected_records = 0
    sentinel = None
    proof = {
        "path": str(raw), "write_delete_handles_excluded": False,
        "sidecars_absent_before": True, "sidecars_absent_after": False,
        "source_stat_unchanged": False,
    }
    try:
        _require_local_ntfs(raw)
        with sealed_source(raw) as stream:
            proof["write_delete_handles_excluded"] = True
            before = os.fstat(stream.fileno())
            if _file_identity(before) != _file_identity(raw.stat()):
                raise ValueError("raw source path does not match held handle")
            conn = sqlite3.connect(raw.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)
            cursor = None
            try:
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.execute("PRAGMA cache_size=-8192")
                manifest = _read_manifest(conn)
                _, cutoff_utc = _cutoff_times(
                    manifest, prefix["scope"]["end_market_second_exclusive"]
                )
                expected_seq, last_ns, last_utc = 1, 0, None
                cursor = conn.execute("SELECT seq,payload FROM events ORDER BY seq")
                for seq, payload in cursor:
                    envelope, event, received_utc = _decode_record(
                        seq, payload, manifest, expected_seq, last_ns
                    )
                    if last_utc is not None and received_utc < last_utc:
                        raise ValueError("UTC receipt clock moved backwards")
                    last_utc = received_utc
                    expected_seq += 1
                    last_ns = event.received_ns
                    if received_utc >= cutoff_utc:
                        sentinel = {
                            "seq": seq, "received_ns": event.received_ns,
                            "received_at_utc": envelope["received_at_utc"],
                        }
                        break
                    prefix_records += 1
                    prefix_digest.update(payload.encode("utf-8") + b"\n")
                    if isinstance(event, OrderedTick) and writer.append(event):
                        selected_records += 1
                    if prefix_records % 1_000_000 == 0:
                        print(json.dumps({
                            "progress_records": prefix_records,
                            "selected_records": selected_records,
                            "elapsed_seconds": round(time.perf_counter() - started_wall, 3),
                        }), flush=True)
                if sentinel is None:
                    raise ValueError("prefix cutoff sentinel not reached")
            finally:
                if cursor is not None:
                    cursor.close()
                conn.close()
            reject_sqlite_sidecars(raw)
            after = os.fstat(stream.fileno())
            if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(raw.stat()):
                raise ValueError("raw source identity changed")
            proof["source_stat_unchanged"] = True
            proof["sidecars_absent_after"] = True
        observed_digest = prefix_digest.hexdigest()
        if prefix_records != prefix["counts"]["raw_records"]:
            raise ValueError("prefix record count mismatch")
        if observed_digest != source_prefix_digest:
            raise ValueError("prefix digest mismatch")
        cache = writer.finish(observed_prefix_digest=observed_digest)
    except BaseException as exc:
        try:
            writer.abort()
        except BaseException:
            pass
        write_json_create(output / "materialization_failure.json", {
            "status": "FAILED", "error": f"{type(exc).__name__}: {exc}",
            "prefix_records": prefix_records, "selected_records": selected_records,
        })
        raise

    verified = load_probe_event_cache(
        cache.cache_dir,
        expected_source_prefix_digest=source_prefix_digest,
        expected_sample_digest=sample.ordered_cell_digest,
        verify_payload=True,
    )
    result = {
        "schema": "mwfd_03_event_materialization_v1",
        "status": "COMPLETED",
        "cache": {
            "path": str(verified.cache_dir),
            "cache_id": verified.manifest["cache_id"],
            "event_count": verified.manifest["event_count"],
            "event_digest": verified.manifest["event_digest"],
            "cell_count": len(verified.manifest["cells"]),
            "bytes": directory_bytes(verified.cache_dir),
            "roundtrip_verified": True,
        },
        "source": {
            "prefix_records": prefix_records, "prefix_digest": source_prefix_digest,
            "boundary_sentinel": sentinel,
        },
        "timing": {
            "wall_seconds": time.perf_counter() - started_wall,
            "cpu_seconds": time.process_time() - started_cpu,
        },
        "memory": {"peak_working_set_bytes": peak_working_set_bytes()},
        "source_preservation": proof,
        "capacity": {"free_bytes_after": shutil.disk_usage(output.parent).free},
    }
    write_json_create(output / "event_materialization.json", result)
    print(json.dumps({
        "status": "COMPLETED", "selected_records": selected_records,
        "cache_id": verified.manifest["cache_id"],
        "wall_seconds": result["timing"]["wall_seconds"],
    }), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
