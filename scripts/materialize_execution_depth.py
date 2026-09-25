from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import tracemalloc

from collector.raw_archive import reject_sqlite_sidecars, sealed_source
from collector.raw_v2 import CaptureControl
from collector.raw_v2_prefix_qualification import _cutoff_times, _decode_record, _read_manifest
from collector.raw_v2_qualification import _file_identity, _require_local_ntfs
from engine.tick_ordering import ReceiveOrderReplay
from research.fast_backtest.execution_depth import (
    ExecutionDepthSpec,
    ExecutionDepthWriter,
    MarketInventoryAccumulator,
    SCHEMA,
)


UTC = timezone.utc


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--raw", required=True)
    value.add_argument("--snapshot-result", required=True)
    value.add_argument("--prefix-report", required=True)
    value.add_argument("--output-dir", required=True)
    value.add_argument("--code-revision", required=True)
    return value


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def working_set_bytes() -> int | None:
    if os.name != "nt":
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

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    process = ctypes.windll.kernel32.GetCurrentProcess()
    if not ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
        return None
    return int(counters.PeakWorkingSetSize)


def safety(snapshot: dict, prefix: dict, raw: Path) -> None:
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


def aggregate_summary(inventory: dict) -> dict:
    gate = Counter()
    clock = Counter()
    depth = Counter()
    direction = Counter()
    eligible_rates = []
    clock_rates = []
    for cell in inventory["cells"]:
        event_gate = cell["event_weighted_gate"]
        for key in ("PASS", "FAIL", "UNKNOWN"):
            gate[key] += event_gate[key]
        clock_gate = cell["clock_time_gate"]
        for key in ("PASS", "FAIL", "UNKNOWN"):
            clock[key] += clock_gate[key]
        for key, value in cell["quality"]["depth"].items():
            depth[key] += value
        for key, value in cell["quality"]["direction"].items():
            direction[key] += value
        if event_gate["evaluated"]:
            eligible_rates.append(event_gate["pass_rate"])
        if clock_gate["total_duration_ns"]:
            clock_rates.append(clock_gate["pass_ratio"])
    evaluated = sum(gate.values())
    clock_total = sum(clock.values())
    return {
        "event_weighted_gate": {
            "evaluated": evaluated, **dict(gate),
            "pass_rate": gate["PASS"] / evaluated if evaluated else None,
            "cell_equal_weight_mean_pass_rate": sum(eligible_rates) / len(eligible_rates) if eligible_rates else None,
        },
        "clock_time_gate": {
            "total_duration_ns": clock_total, **dict(clock),
            "pass_ratio": clock["PASS"] / clock_total if clock_total else None,
            "cell_equal_weight_mean_pass_ratio": sum(clock_rates) / len(clock_rates) if clock_rates else None,
        },
        "depth_completeness": dict(sorted(depth.items())),
        "direction_quality": dict(sorted(direction.items())),
    }


def deterministic_probe_plan(inventory: dict, session_id: str) -> dict:
    candidates = [cell for cell in inventory["cells"] if cell["admission"] == "ELIGIBLE_FOR_FAST_PROBE"]
    if len(candidates) < 30:
        return {
            "status": "BLOCKED", "eligible_cells": len(candidates), "selected_cells": [],
            "reason": "fewer than 30 eligible cells",
        }
    ordered = sorted(candidates, key=lambda cell: (cell["activity"]["event_count"], cell["cell_id"]))
    n = len(ordered)
    tiers = {
        "low": ordered[: n // 3],
        "medium": ordered[n // 3: 2 * n // 3],
        "high": ordered[2 * n // 3:],
    }
    selected = []
    for tier in ("high", "medium", "low"):
        stable = sorted(
            tiers[tier],
            key=lambda cell: hashlib.sha256(f"{session_id}|{cell['cell_id']}".encode()).hexdigest(),
        )[:15]
        selected.extend({
            "tier": tier,
            "cell_id": cell["cell_id"],
            "event_count": cell["activity"]["event_count"],
            "selection_hash": hashlib.sha256(f"{session_id}|{cell['cell_id']}".encode()).hexdigest(),
        } for cell in stable)
    return {
        "status": "ADMITTED" if len(selected) >= 30 else "BLOCKED",
        "eligible_cells": len(candidates),
        "selection_rule": "event-count terciles; within tier SHA256(session_id|cell_id) ascending; 15 each",
        "selected_cells": selected,
        "probe_contract": {
            "cold": "verified execution-depth/input cache absent before cell run",
            "warm": "matching source/input/depth/feature digests required",
            "measure": ["gate_seconds", "feature_build_seconds", "663_candidate_sweep_seconds", "output_seconds", "peak_memory_bytes", "output_bytes"],
            "shared_materialization_reuse": True,
            "checkpoint": "create-only per-cell status and digest; incomplete attempts restart in new directory",
            "full_market_runtime_estimate": "NOT_COMPUTED_BEFORE_PROBE",
        },
    }


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    raw = Path(args.raw).absolute()
    snapshot_path = Path(args.snapshot_result).resolve(strict=True)
    prefix_path = Path(args.prefix_report).resolve(strict=True)
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    snapshot, prefix = read_json(snapshot_path), read_json(prefix_path)
    safety(snapshot, prefix, raw)
    source_before = raw.stat()
    source_identity = {"path": str(raw), "size": source_before.st_size, "mtime_ns": source_before.st_mtime_ns}
    code_root = Path(__file__).resolve().parents[1]
    provenance_paths = (
        "research/fast_backtest/execution_depth.py",
        "scripts/materialize_execution_depth.py",
        "collector/raw_v2_prefix_qualification.py",
        "collector/selected_instrument_smoke_policy.py",
    )
    code_provenance = {name: file_hash(code_root / name) for name in provenance_paths}
    manifest_claim = prefix["input"]["manifest"]
    expected_digest = prefix["prefix_event_sha256"]
    cutoff_second = prefix["scope"]["end_market_second_exclusive"]
    spec = ExecutionDepthSpec(
        source=manifest_claim["source"], session_id=manifest_claim["session_id"],
        market_date=manifest_claim["market_date"], cutoff_market_second_exclusive=cutoff_second,
        source_prefix_digest=expected_digest, source_path_identity=source_identity,
        code_provenance=code_provenance | {"git_revision": args.code_revision},
    )
    writer = ExecutionDepthWriter(output / "execution_depth_cache", spec)
    accumulator = MarketInventoryAccumulator()
    timings = Counter()
    prefix_digest = hashlib.sha256()
    total_start = time.perf_counter()
    tracemalloc.start()
    peak_working_set = working_set_bytes()
    proof = {
        "path": str(raw), "write_delete_handles_excluded": False,
        "sidecars_absent_before": True, "sidecars_absent_after": False,
        "source_stat_unchanged": False,
    }
    prefix_records = 0
    sentinel = None
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
                if manifest["session_id"] != spec.session_id:
                    raise ValueError("session mismatch")
                _, cutoff_utc = _cutoff_times(manifest, cutoff_second)
                replay = ReceiveOrderReplay(source=manifest["source"], session_id=manifest["session_id"], max_quote_age_ns=0)
                expected_seq, last_ns, last_utc = 1, 0, None
                cursor = conn.execute("SELECT seq,payload FROM events ORDER BY seq")
                for seq, payload in cursor:
                    t0 = time.perf_counter()
                    envelope, event, received_utc = _decode_record(seq, payload, manifest, expected_seq, last_ns)
                    timings["decode_seconds"] += time.perf_counter() - t0
                    if last_utc is not None and received_utc < last_utc:
                        raise ValueError("UTC receipt clock moved backwards")
                    last_utc = received_utc
                    expected_seq += 1
                    last_ns = event.received_ns
                    if not isinstance(event, CaptureControl):
                        replay.accept(event)
                    if received_utc >= cutoff_utc:
                        sentinel = {"seq": seq, "received_ns": event.received_ns, "received_at_utc": envelope["received_at_utc"]}
                        break
                    prefix_records += 1
                    prefix_digest.update(payload.encode("utf-8") + b"\n")
                    t0 = time.perf_counter()
                    depth = accumulator.accept(envelope)
                    timings["inventory_gate_seconds"] += time.perf_counter() - t0
                    if depth is not None:
                        t0 = time.perf_counter()
                        writer.append(depth)
                        timings["depth_cache_write_seconds"] += time.perf_counter() - t0
                    if prefix_records % 500_000 == 0:
                        current_peak = working_set_bytes()
                        peak_working_set = max(filter(None, (peak_working_set, current_peak)), default=None)
                        print(json.dumps({"progress_records": prefix_records, "quotes": writer.count, "cells": len(accumulator.cells), "elapsed_seconds": round(time.perf_counter() - total_start, 3)}), flush=True)
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
        if observed_digest != expected_digest:
            raise ValueError("prefix digest mismatch")
        t0 = time.perf_counter()
        cache_manifest = writer.finish(observed_prefix_digest=observed_digest)
        timings["depth_cache_finalize_seconds"] += time.perf_counter() - t0
    except BaseException as exc:
        try:
            writer.abort()
        except BaseException:
            pass
        write_json(output / "failure.json", {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}", "prefix_records": prefix_records})
        raise

    inventory = accumulator.result()
    inventory.update({
        "market_date": spec.market_date, "source": spec.source, "session_id": spec.session_id,
        "source_prefix_digest": expected_digest, "cutoff_market_second_exclusive": cutoff_second,
        "boundary_sentinel": sentinel, "source_strict_prefix_research_eligible": prefix["smoke_backtest_eligible"],
        "selected_policy": "multi-code equivalent of selected_instrument_smoke_quality_v2 with unknown-direction quarantine",
    })
    aggregate = aggregate_summary(inventory)
    plan = deterministic_probe_plan(inventory, spec.session_id)
    write_json(output / "market_inventory.json", inventory)
    write_json(output / "probe_admission.json", plan)
    current, peak_traced = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    final_peak_ws = working_set_bytes()
    peak_working_set = max(filter(None, (peak_working_set, final_peak_ws)), default=None)
    output_bytes = sum(item.stat().st_size for item in output.rglob("*") if item.is_file())
    summary = {
        "schema": "mwfd_02_shared_market_materialization_v1",
        "status": "COMPLETED",
        "code_revision": args.code_revision,
        "source": {
            **source_identity, "prefix_digest": expected_digest,
            "prefix_records": prefix_records, "sentinel": sentinel,
            "multi_code_evidence": {
                "cell_count": inventory["cell_count"],
                "selected_005930_events": next((cell["activity"]["event_count"] for cell in inventory["cells"] if cell["code"] == "005930"), None),
                "source_is_multi_code": inventory["cell_count"] > 1,
            },
        },
        "execution_depth_cache": {
            "schema": SCHEMA, "cache_id": cache_manifest.cache_id,
            "path": str(cache_manifest.cache_dir), "quote_count": cache_manifest.quote_count,
            "logical_digest": cache_manifest.logical_digest,
        },
        "inventory": {
            "cell_count": inventory["cell_count"],
            "admission_counts": inventory["admission_counts"],
            "reconciliation": inventory["source_relevant_counts"],
            **aggregate,
        },
        "probe_admission": plan,
        "timing_seconds": dict(timings) | {"total": time.perf_counter() - total_start},
        "memory": {"tracemalloc_current_bytes": current, "tracemalloc_peak_bytes": peak_traced, "peak_working_set_bytes": peak_working_set},
        "output_bytes": output_bytes,
        "source_preservation": proof,
        "code_provenance": code_provenance,
        "limitations": [
            "bounded prefix only; tail and whole stream remain unassessed",
            "venue literals are preserved and unknown is not inferred",
            "cell eligibility is selected-strategy research eligibility, not whole-prefix or performance-research eligibility",
            "no PnL, parameter tuning, threshold tuning, candidate sweep, or production exact replay was run",
        ],
    }
    write_json(output / "summary.json", summary)
    print(json.dumps({"status": "COMPLETED", "cells": inventory["cell_count"], "admission": inventory["admission_counts"], "quotes": cache_manifest.quote_count, "total_seconds": summary["timing_seconds"]["total"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
