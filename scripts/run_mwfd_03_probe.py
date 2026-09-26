"""MWFD-03 3-cell smoke → remaining 42 → warm smoke runtime probe."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import sys
import time
import tracemalloc
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research.fast_backtest.execution_depth import (
    load_cell_execution_depth_from_manifest,
    load_execution_depth_cache,
)
from research.fast_backtest.feasibility import feasibility_summary, join_entry_feasibility
from research.fast_backtest.features import (
    build_causal_features,
    load_feature_cache,
    write_feature_cache,
)
from research.fast_backtest.runtime_probe import (
    assert_finite,
    directory_bytes,
    json_digest,
    load_candidate_family,
    load_gate_cache,
    load_probe_cell_events,
    load_probe_event_cache,
    load_probe_sample,
    write_gate_cache,
    write_json_create,
)
from research.fast_backtest.plan import canonical_json
from research.fast_backtest.sweep import (
    FastAccountAssumptions,
    feature_config_for_candidates,
    result_record,
    run_fast_sweep,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="MWFD-03 45-cell Fast runtime probe")
    value.add_argument("--run-root", required=True)
    value.add_argument("--mwfd02-root", required=True)
    value.add_argument("--candidate-summary", required=True)
    value.add_argument("--code-revision", required=True)
    value.add_argument("--resume", action="store_true")
    value.add_argument("--resume-check", action="store_true")
    return value


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    index = (len(ordered) - 1) * fraction
    lower, upper = math.floor(index), math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


def working_set() -> tuple[int | None, int | None]:
    if sys.platform != "win32":
        return None, None
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
    return (
        (int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize))
        if ok else (None, None)
    )


def timed(call):
    wall, cpu = time.perf_counter(), time.process_time()
    value = call()
    return value, time.perf_counter() - wall, time.process_time() - cpu


def no_trade_status(result) -> str:
    if result.trades:
        return "TRADED"
    if result.terminal_position:
        return "OPEN_POSITION"
    if result.terminal_open_order:
        return "OPEN_ORDER"
    if result.entry_signals and not result.fills:
        return "ENTRY_INTENT_NO_FILL"
    if result.fills:
        return "INCOMPLETE_TRADE"
    return "NO_ENTRY_SIGNAL"


def economic_record(result) -> dict:
    return {
        "candidate_id": result.candidate_id,
        "parameter_identity": result.parameter_identity,
        "signals": result.signals,
        "entry_signals": result.entry_signals,
        "exit_signals": result.exit_signals,
        "fills": result.fills,
        "trades": result.trades,
        "gross_pnl": result.gross_pnl,
        "fees": result.fees,
        "net_pnl": result.net_pnl,
        "screening_status": result.screening_status,
        "terminal_position": result.terminal_position,
        "terminal_open_order": result.terminal_open_order,
        "entry_decision_ns": list(result.entry_decision_ns),
        "exit_decision_ns": list(result.exit_decision_ns),
        "fill_records": [asdict(fill) for fill in result.fill_records],
    }


def results_digest(results) -> str:
    return json_digest([economic_record(result) for result in sorted(results, key=lambda item: item.parameter_identity)])


def candidate_cell_record(result, *, cell, gate, inventory_cell, sweep_seconds, candidate_count):
    value = {
        "date": "2026-09-21",
        "source": "kiwoom_openapi_raw_v2",
        "session_id": None,
        "code": cell.code,
        "venue": cell.venue,
        "cell_id": cell.cell_id,
        "activity_stratum": cell.tier,
        "event_count": cell.event_count,
        "candidate_id": result.candidate_id,
        "parameter_identity": result.parameter_identity,
        "candidate_aliases": list(result.aliases),
        "evaluated": 1,
        "entry_signals": result.entry_signals,
        "submitted_intents": result.signals,
        "fills": result.fills,
        "completed_trades": result.trades,
        "no_trade_status": no_trade_status(result),
        "fast_gross_result": result.gross_pnl,
        "fast_net_result": result.net_pnl,
        "fee": result.fees,
        "ending_position": result.terminal_position,
        "ending_open_order": result.terminal_open_order,
        "screening_status": result.screening_status,
        "screening_only": True,
        "feasibility": {
            "evaluated": gate["evaluated"], "PASS": gate["PASS"],
            "FAIL": gate["FAIL"], "UNKNOWN": gate["UNKNOWN"],
            "event_weighted_pass_rate": gate["pass_rate"],
            "clock_time": inventory_cell["clock_time_gate"],
        },
        "runtime_contribution_seconds": sweep_seconds / candidate_count,
    }
    assert_finite(value)
    return value


def verify_cell_checkpoint(path: Path, *, candidate_family, event_digest: str) -> dict:
    completion = read_json(path / "completion.json")
    if completion.get("status") != "COMPLETED":
        raise ValueError("incomplete cell checkpoint")
    if completion["candidate_family_digest"] != candidate_family.canonical_family_digest:
        raise ValueError("cell checkpoint candidate identity mismatch")
    if completion["event_digest"] != event_digest:
        raise ValueError("cell checkpoint event digest mismatch")
    results_path = path / "candidate_results.jsonl"
    digest = hashlib.sha256()
    identities = []
    count = 0
    with results_path.open("rb") as stream:
        for line in stream:
            digest.update(line)
            row = json.loads(line)
            assert_finite(row)
            identities.append(row["parameter_identity"])
            count += 1
    expected_candidates = len(candidate_family.candidates)
    if count != expected_candidates or len(set(identities)) != expected_candidates:
        raise ValueError("candidate-cell checkpoint reconciliation failed")
    if identities != [candidate.parameter_identity for candidate in candidate_family.candidates]:
        raise ValueError("candidate ordering changed")
    if digest.hexdigest() != completion["candidate_results_file_digest"]:
        raise ValueError("candidate-cell serialization digest mismatch")
    return completion


def execute_cell(
    *,
    run_root: Path,
    cell,
    event_cache,
    depth_manifest,
    source_prefix_digest: str,
    candidate_family,
    feature_config,
    account,
    inventory_cell,
) -> dict:
    checkpoints = run_root / "checkpoints"
    checkpoints.mkdir(exist_ok=True)
    name = f"{cell.index:02d}-{cell.code}-{cell.venue}"
    target = checkpoints / name
    event_meta = event_cache.manifest["cells"][cell.cell_id]
    if target.exists():
        return verify_cell_checkpoint(
            target, candidate_family=candidate_family, event_digest=event_meta["event_digest"]
        ) | {"resumed": True}
    building = checkpoints / f".building-{name}-{uuid4().hex}"
    building.mkdir(exist_ok=False)
    total_wall, total_cpu = time.perf_counter(), time.process_time()
    tracemalloc.reset_peak()
    try:
        events, access_wall, access_cpu = timed(
            lambda: load_probe_cell_events(event_cache, cell.cell_id)
        )
        if len(events) != cell.event_count:
            raise ValueError("cell event count differs from preregistration")
        depths, depth_wall, depth_cpu = timed(
            lambda: load_cell_execution_depth_from_manifest(
                depth_manifest, code=cell.code, venue=cell.venue
            )
        )
        gate_states, join_wall, join_cpu = timed(
            lambda: join_entry_feasibility(
                events, depths, max_quote_age_ns=account.max_quote_age_ns
            )
        )
        gate = feasibility_summary(gate_states)
        expected_gate = inventory_cell["event_weighted_gate"]
        for key in ("evaluated", "PASS", "FAIL", "UNKNOWN"):
            if gate[key] != expected_gate[key]:
                raise ValueError("MWFD-02 gate reconciliation failed")
        features, feature_wall, feature_cpu = timed(
            lambda: build_causal_features(
                events,
                config=feature_config,
                input_event_digest=event_meta["event_digest"],
            )
        )
        results, sweep_wall, sweep_cpu = timed(
            lambda: run_fast_sweep(
                features,
                candidate_family.candidates,
                account=account,
                entry_feasibility=gate_states,
            )
        )
        ordered_results = tuple(sorted(results, key=lambda item: item.parameter_identity))
        if [item.parameter_identity for item in ordered_results] != [
            candidate.parameter_identity for candidate in candidate_family.candidates
        ]:
            raise ValueError("candidate identities changed during sweep")
        economic_digest = results_digest(ordered_results)

        serialize_wall, serialize_cpu = time.perf_counter(), time.process_time()
        gate_manifest = write_gate_cache(
            building / "gate_cache",
            states=gate_states,
            event_digest=event_meta["event_digest"],
            depth_cache_id=depth_manifest.cache_id,
        )
        write_feature_cache(features, building / "feature_cache")
        result_path = building / "candidate_results.jsonl"
        result_file_digest = hashlib.sha256()
        no_trade = Counter()
        trade_candidates = 0
        pnl = []
        with result_path.open("x", encoding="utf-8", newline="\n") as stream:
            for result in ordered_results:
                row = candidate_cell_record(
                    result, cell=cell, gate=gate,
                    inventory_cell=inventory_cell, sweep_seconds=sweep_wall,
                    candidate_count=len(ordered_results),
                )
                row["source"] = events[0].source
                row["session_id"] = events[0].session_id
                status = row["no_trade_status"]
                no_trade[status] += 1
                trade_candidates += int(result.trades > 0)
                if result.net_pnl is not None:
                    pnl.append(result.net_pnl)
                line = canonical_json(row) + "\n"
                stream.write(line)
                result_file_digest.update(line.encode("utf-8"))
        serialize_wall = time.perf_counter() - serialize_wall
        serialize_cpu = time.process_time() - serialize_cpu
        current_trace, peak_trace = tracemalloc.get_traced_memory()
        rss, peak_rss = working_set()
        timings = {
            "event_access": {"wall_seconds": access_wall, "cpu_seconds": access_cpu},
            "execution_feasibility": {
                "wall_seconds": depth_wall + join_wall,
                "cpu_seconds": depth_cpu + join_cpu,
            },
            "causal_feature": {"wall_seconds": feature_wall, "cpu_seconds": feature_cpu},
            "candidate_sweep": {"wall_seconds": sweep_wall, "cpu_seconds": sweep_cpu},
            "serialization": {"wall_seconds": serialize_wall, "cpu_seconds": serialize_cpu},
        }
        cell_result = {
            "schema": "mwfd_03_cell_result_v1",
            "status": "COMPLETED",
            "cell": cell.to_dict(),
            "event": {
                "count": len(events), "trade_count": gate["evaluated"],
                "quote_count": len(depths), "digest": event_meta["event_digest"],
                "bytes": event_meta["event_bytes"],
            },
            "gate": gate,
            "clock_time_gate": inventory_cell["clock_time_gate"],
            "candidate_count": len(ordered_results),
            "candidate_results_digest": economic_digest,
            "candidate_results_file_digest": result_file_digest.hexdigest(),
            "candidate_outcomes": {
                "trade_candidate_count": trade_candidates,
                "no_trade_status_counts": dict(sorted(no_trade.items())),
                "net_pnl_min": min(pnl) if pnl else None,
                "net_pnl_max": max(pnl) if pnl else None,
                "net_pnl_mean": statistics.fmean(pnl) if pnl else None,
            },
            "cache": {
                "event": "HIT_SHARED",
                "execution_depth": "HIT_SHARED",
                "gate": "MISS_BUILT",
                "feature": "MISS_BUILT",
                "gate_digest": gate_manifest["state_digest"],
                "feature_digest": features.feature_digest,
            },
            "timing": timings,
            "memory": {
                "tracemalloc_current_bytes": current_trace,
                "tracemalloc_peak_bytes": peak_trace,
                "working_set_bytes": rss,
                "peak_working_set_bytes": peak_rss,
            },
            "screening_only": True,
        }
        assert_finite(cell_result)
        write_json_create(building / "cell_result.json", cell_result)
        completion = {
            "schema": "mwfd_03_cell_completion_v1",
            "status": "COMPLETED",
            "cell_id": cell.cell_id,
            "event_digest": event_meta["event_digest"],
            "candidate_family_digest": candidate_family.canonical_family_digest,
            "candidate_count": len(ordered_results),
            "candidate_results_digest": economic_digest,
            "candidate_results_file_digest": result_file_digest.hexdigest(),
            "wall_seconds": time.perf_counter() - total_wall,
            "cpu_seconds": time.process_time() - total_cpu,
        }
        write_json_create(building / "completion.json", completion)
        building.rename(target)
        return completion | {"resumed": False}
    except BaseException:
        if building.exists():
            shutil.rmtree(building)
        raise


def validate_phase1(run_root, smoke_cells, event_cache, family):
    checked = []
    for cell in smoke_cells:
        path = run_root / "checkpoints" / f"{cell.index:02d}-{cell.code}-{cell.venue}"
        completion = verify_cell_checkpoint(
            path,
            candidate_family=family,
            event_digest=event_cache.manifest["cells"][cell.cell_id]["event_digest"],
        )
        checked.append({"cell_id": cell.cell_id, "candidate_results_digest": completion["candidate_results_digest"]})
    return {
        "schema": "mwfd_03_phase1_smoke_v1",
        "status": "PASS",
        "cells": checked,
        "candidate_cell_count": len(smoke_cells) * len(family.candidates),
        "structural_failures": 0,
        "performance_used_for_selection": False,
    }


def warm_smoke(run_root, smoke_cells, event_cache, depth_manifest, family, account):
    records = []
    config = feature_config_for_candidates(family.candidates, account.max_quote_age_ns)
    for cell in smoke_cells:
        checkpoint = run_root / "checkpoints" / f"{cell.index:02d}-{cell.code}-{cell.venue}"
        event_meta = event_cache.manifest["cells"][cell.cell_id]
        started_wall, started_cpu = time.perf_counter(), time.process_time()
        events, access_wall, access_cpu = timed(lambda: load_probe_cell_events(event_cache, cell.cell_id))
        gate, gate_wall, gate_cpu = timed(lambda: load_gate_cache(
            checkpoint / "gate_cache",
            expected_event_digest=event_meta["event_digest"],
            expected_depth_cache_id=depth_manifest.cache_id,
        ))
        features, feature_wall, feature_cpu = timed(lambda: load_feature_cache(
            checkpoint / "feature_cache",
            expected_input_event_digest=event_meta["event_digest"],
        ))
        if features.config != config or len(features.rows) != len(events):
            raise ValueError("warm feature cache identity mismatch")
        results, sweep_wall, sweep_cpu = timed(lambda: run_fast_sweep(
            features, family.candidates, account=account, entry_feasibility=gate
        ))
        digest = results_digest(results)
        cold = read_json(checkpoint / "completion.json")
        if digest != cold["candidate_results_digest"]:
            raise ValueError("warm/cold economic result mismatch")
        records.append({
            "cell_id": cell.cell_id,
            "event_count": len(events),
            "candidate_count": len(results),
            "result_digest": digest,
            "cache": {"event": "HIT_SHARED", "gate": "HIT", "feature": "HIT", "depth": "NOT_READ"},
            "timing": {
                "event_access": {"wall_seconds": access_wall, "cpu_seconds": access_cpu},
                "gate_load": {"wall_seconds": gate_wall, "cpu_seconds": gate_cpu},
                "feature_load": {"wall_seconds": feature_wall, "cpu_seconds": feature_cpu},
                "candidate_sweep": {"wall_seconds": sweep_wall, "cpu_seconds": sweep_cpu},
                "total": {
                    "wall_seconds": time.perf_counter() - started_wall,
                    "cpu_seconds": time.process_time() - started_cpu,
                },
            },
        })
    return {
        "schema": "mwfd_03_warm_smoke_v1",
        "status": "PASS",
        "cells": records,
        "candidate_cell_rechecks": len(records) * len(family.candidates),
    }


def combine_outputs(run_root, sample, family):
    candidate_target = run_root / "candidate_cell_results.jsonl"
    cell_target = run_root / "cell_results.jsonl"
    candidate_digest = hashlib.sha256()
    cell_digest = hashlib.sha256()
    seen_pairs = set()
    candidate_count = 0
    cell_results = []
    with candidate_target.open("x", encoding="utf-8", newline="\n") as candidate_stream, cell_target.open("x", encoding="utf-8", newline="\n") as cell_stream:
        for cell in sample.cells:
            checkpoint = run_root / "checkpoints" / f"{cell.index:02d}-{cell.code}-{cell.venue}"
            cell_result = read_json(checkpoint / "cell_result.json")
            line = canonical_json(cell_result) + "\n"
            cell_stream.write(line)
            cell_digest.update(line.encode("utf-8"))
            cell_results.append(cell_result)
            with (checkpoint / "candidate_results.jsonl").open("r", encoding="utf-8") as source:
                for source_line in source:
                    row = json.loads(source_line)
                    pair = (row["cell_id"], row["parameter_identity"])
                    if pair in seen_pairs:
                        raise ValueError("duplicate candidate-cell output")
                    seen_pairs.add(pair)
                    canonical = canonical_json(row) + "\n"
                    candidate_stream.write(canonical)
                    candidate_digest.update(canonical.encode("utf-8"))
                    candidate_count += 1
    if candidate_count != len(sample.cells) * len(family.candidates) or len(seen_pairs) != candidate_count:
        raise ValueError("candidate-cell expected count reconciliation failed")
    return cell_results, {
        "candidate_cell_count": candidate_count,
        "candidate_cell_digest": candidate_digest.hexdigest(),
        "cell_count": len(cell_results),
        "cell_results_digest": cell_digest.hexdigest(),
    }


def runtime_summary(run_root, cells, materialization, warm, combined):
    stages = defaultdict(lambda: {"wall_seconds": 0.0, "cpu_seconds": 0.0})
    tiers = defaultdict(list)
    event_counts, runtimes = [], []
    peak_trace = peak_rss = 0
    for cell in cells:
        total = 0.0
        for stage, values in cell["timing"].items():
            stages[stage]["wall_seconds"] += values["wall_seconds"]
            stages[stage]["cpu_seconds"] += values["cpu_seconds"]
            total += values["wall_seconds"]
        tiers[cell["cell"]["tier"]].append((cell["event"]["count"], total, cell["timing"]["candidate_sweep"]["wall_seconds"], cell["memory"]))
        event_counts.append(cell["event"]["count"])
        runtimes.append(total)
        peak_trace = max(peak_trace, cell["memory"]["tracemalloc_peak_bytes"] or 0)
        peak_rss = max(peak_rss, cell["memory"]["peak_working_set_bytes"] or 0)
    by_tier = {}
    for tier in ("high", "medium", "low"):
        values = tiers[tier]
        runtime_values = [item[1] for item in values]
        sweep_values = [item[2] for item in values]
        by_tier[tier] = {
            "cell_count": len(values),
            "event_count": sum(item[0] for item in values),
            "runtime_p50_seconds": statistics.median(runtime_values),
            "runtime_p90_seconds": percentile(runtime_values, 0.9),
            "runtime_max_seconds": max(runtime_values),
            "candidate_sweep_total_seconds": sum(sweep_values),
            "candidate_sweep_p50_seconds": statistics.median(sweep_values),
            "peak_tracemalloc_bytes": max(item[3]["tracemalloc_peak_bytes"] or 0 for item in values),
            "peak_working_set_bytes": max(item[3]["peak_working_set_bytes"] or 0 for item in values),
        }
    correlation = statistics.correlation(event_counts, runtimes) if len(set(event_counts)) > 1 else None
    cell_wall = sum(runtimes)
    active_wall = materialization["timing"]["wall_seconds"] + cell_wall + sum(
        item["timing"]["total"]["wall_seconds"] for item in warm["cells"]
    )
    result = {
        "schema": "mwfd_03_runtime_summary_v1",
        "status": "COMPLETED",
        "work": {
            "cells": 45, "unique_candidates": 663,
            "unique_candidate_cell_evaluations": combined["candidate_cell_count"],
            "warm_candidate_cell_rechecks": warm["candidate_cell_rechecks"],
        },
        "active_elapsed": {
            "wall_seconds": active_wall,
            "source_event_materialization_seconds": materialization["timing"]["wall_seconds"],
            "coldish_cell_pipeline_seconds": cell_wall,
            "warm_smoke_seconds": sum(item["timing"]["total"]["wall_seconds"] for item in warm["cells"]),
        },
        "stages": dict(stages),
        "tiers": by_tier,
        "event_count_runtime": {"pearson_correlation": correlation},
        "memory": {
            "peak_tracemalloc_bytes": peak_trace,
            "peak_working_set_bytes": peak_rss or None,
        },
        "cache_modes": {
            "coldish": "new gate/feature/checkpoint artifacts; immutable event/depth caches preserved",
            "warm": "3 preregistered smoke cells load persisted gate/feature caches and re-sweep",
        },
        "output_bytes": directory_bytes(run_root),
    }
    assert_finite(result)
    return result


def eligible_tiers(inventory):
    eligible = [cell for cell in inventory["cells"] if cell["admission"] == "ELIGIBLE_FOR_FAST_PROBE"]
    ordered = sorted(eligible, key=lambda cell: (cell["activity"]["event_count"], cell["cell_id"]))
    n = len(ordered)
    return {
        "low": ordered[: n // 3],
        "medium": ordered[n // 3: 2 * n // 3],
        "high": ordered[2 * n // 3:],
    }


def project_full_run(run_root, runtime, cell_results, inventory, materialization, free_bytes):
    measured = defaultdict(list)
    feature_bytes = event_bytes = gate_bytes = candidate_bytes = 0
    total_sample_events = 0
    for cell in cell_results:
        tier = cell["cell"]["tier"]
        cell_seconds = sum(item["wall_seconds"] for item in cell["timing"].values())
        events = cell["event"]["count"]
        measured[tier].append(cell_seconds / events)
        total_sample_events += events
        event_bytes += cell["event"]["bytes"]
        checkpoint = run_root / "checkpoints" / f"{cell['cell']['index']:02d}-{cell['cell']['code']}-{cell['cell']['venue']}"
        feature_bytes += directory_bytes(checkpoint / "feature_cache")
        gate_bytes += directory_bytes(checkpoint / "gate_cache")
        candidate_bytes += (checkpoint / "candidate_results.jsonl").stat().st_size
    tiers = eligible_tiers(inventory)
    projected_tiers = {}
    projected_compute = projected_low = projected_high = 0.0
    total_events = total_trades = 0
    for tier in ("high", "medium", "low"):
        cells = tiers[tier]
        events = sum(cell["activity"]["event_count"] for cell in cells)
        trades = sum(cell["activity"]["trade_count"] for cell in cells)
        rates = measured[tier]
        point_rate = statistics.median(rates)
        low_rate, high_rate = percentile(rates, 0.1), percentile(rates, 0.9)
        point, low, high = events * point_rate, events * low_rate, events * high_rate
        projected_tiers[tier] = {
            "cell_count": len(cells), "event_count": events, "trade_count": trades,
            "seconds_per_event_p50": point_rate,
            "seconds_per_event_p10": low_rate,
            "seconds_per_event_p90": high_rate,
            "projected_compute_seconds": point,
            "range_seconds": [low, high],
        }
        projected_compute += point
        projected_low += low
        projected_high += high
        total_events += events
        total_trades += trades
    materialize_seconds = materialization["timing"]["wall_seconds"]
    materialize_range = [materialize_seconds, materialize_seconds * 1.5]
    materialize_point = materialize_seconds * 1.15
    projected_total = materialize_point + projected_compute
    range_total = [materialize_range[0] + projected_low, materialize_range[1] + projected_high]
    candidate_cells = 1286 * 663
    projected_storage = (
        event_bytes / total_sample_events * total_events
        + feature_bytes / total_sample_events * total_events
        + gate_bytes / sum(cell["gate"]["evaluated"] for cell in cell_results) * total_trades
        + candidate_bytes / (45 * 663) * candidate_cells
    )
    sample_max_events = max(cell["event"]["count"] for cell in cell_results)
    full_max_events = max(cell["activity"]["event_count"] for cells in tiers.values() for cell in cells)
    observed_peak = runtime["memory"]["peak_tracemalloc_bytes"]
    projected_peak = observed_peak * max(1.0, full_max_events / sample_max_events)
    result = {
        "schema": "mwfd_03_runtime_projection_v1",
        "status": "PROJECTED_NOT_EXECUTED",
        "eligible_cells": 1286,
        "unique_candidates": 663,
        "projected_candidate_cells": candidate_cells,
        "inventory": {"event_count": total_events, "trade_count": total_trades, "tiers": projected_tiers},
        "single_worker": {
            "projected_total_seconds": projected_total,
            "range_seconds": range_total,
            "projected_output_bytes": projected_storage,
            "projected_peak_tracemalloc_bytes": projected_peak,
            "observed_peak_working_set_bytes": runtime["memory"]["peak_working_set_bytes"],
        },
        "bounded_multi_worker": {
            "status": "NOT_RECOMMENDED_WITHOUT_SEPARATE_CONTENTION_PROBE",
            "reason": "shared SQLite I/O contention and per-worker feature/result memory were not measured",
        },
        "capacity": {
            "free_bytes_at_projection": free_bytes,
            "projected_storage_fraction_of_free": projected_storage / free_bytes,
        },
        "method": {
            "compute": "activity-tier p50 seconds/event; p10-p90 range applied to eligible inventory events",
            "materialization": "actual one-pass time ×1.15 point, actual to ×1.5 range",
            "storage": "measured bytes/event, bytes/trade-state and bytes/candidate-cell scaled separately",
            "peak_memory": "observed tracemalloc peak scaled by max eligible/sample event-count ratio",
            "parallel_baseline": "one worker; no full parallel execution performed",
        },
        "limitations": [
            "45-cell stratified probe is not an exact full-run timing",
            "peak memory extrapolates beyond the largest sampled cell",
            "venue remains literal unknown and daily KRX metadata remains NOT_READY",
        ],
    }
    assert_finite(result)
    return result


def aggregate_gate(cell_results):
    counts = Counter()
    reasons = Counter()
    for cell in cell_results:
        gate = cell["gate"]
        for key in ("PASS", "FAIL", "UNKNOWN"):
            counts[key] += gate[key]
        reasons.update(gate["reasons"])
    evaluated = sum(counts.values())
    return {
        "schema": "mwfd_03_gate_summary_v1",
        "evaluated": evaluated,
        "PASS": counts["PASS"], "FAIL": counts["FAIL"], "UNKNOWN": counts["UNKNOWN"],
        "pass_rate": counts["PASS"] / evaluated if evaluated else None,
        "reconciled": counts["PASS"] + counts["FAIL"] + counts["UNKNOWN"] == evaluated,
        "reasons": dict(sorted(reasons.items())),
        "event_masking": False,
        "contract": "gate applies only to new entry evaluation; history/position/exit rows are preserved",
    }


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    run_root = Path(args.run_root).resolve(strict=True)
    mwfd02 = Path(args.mwfd02_root).resolve(strict=True)
    manifest = read_json(run_root / "run_manifest.json")
    if manifest["code_revision"] != args.code_revision:
        raise ValueError("run/code revision mismatch")
    sample = load_probe_sample(mwfd02 / "probe_admission.json", mwfd02 / "market_inventory.json")
    family = load_candidate_family(args.candidate_summary)
    if sample.ordered_cell_digest != manifest["sample"]["ordered_cell_digest"]:
        raise ValueError("sample changed after phase 0")
    if family.canonical_family_digest != manifest["candidate_family"]["canonical_family_digest"]:
        raise ValueError("candidate family changed after phase 0")
    source_digest = manifest["source"]["prefix_digest"]
    event_cache = load_probe_event_cache(
        run_root / "event_cache",
        expected_source_prefix_digest=source_digest,
        expected_sample_digest=sample.ordered_cell_digest,
        verify_payload=True,
    )
    depth = load_execution_depth_cache(
        manifest["shared_cache"]["execution_depth_path"],
        expected_source_prefix_digest=source_digest,
        verify_payload=False,
    )
    inventory = read_json(mwfd02 / "market_inventory.json")
    inventory_by_id = {cell["cell_id"]: cell for cell in inventory["cells"]}
    account = FastAccountAssumptions(**manifest["account"])
    feature_config = feature_config_for_candidates(family.candidates, account.max_quote_age_ns)
    smoke = tuple(next(cell for cell in sample.cells if cell.tier == tier) for tier in ("high", "medium", "low"))

    if args.resume_check:
        combined_path = run_root / "candidate_cell_results.jsonl"
        before_digest = hashlib.sha256(combined_path.read_bytes()).hexdigest()
        checked = []
        for cell in sample.cells:
            checkpoint = run_root / "checkpoints" / f"{cell.index:02d}-{cell.code}-{cell.venue}"
            checked.append(verify_cell_checkpoint(
                checkpoint, candidate_family=family,
                event_digest=event_cache.manifest["cells"][cell.cell_id]["event_digest"],
            )["candidate_results_digest"])
        after_digest = hashlib.sha256(combined_path.read_bytes()).hexdigest()
        result = {
            "schema": "mwfd_03_resume_validation_v1", "status": "PASS",
            "completed_cells_skipped": 45, "candidate_cell_output_rewritten": False,
            "combined_file_digest_before": before_digest,
            "combined_file_digest_after": after_digest,
            "checkpoint_economic_digest": json_digest(checked),
        }
        if before_digest != after_digest:
            raise ValueError("resume validation changed combined output")
        write_json_create(run_root / "resume_validation.json", result)
        preliminary = read_json(run_root / "full_run_admission_pre_resume.json")
        final_admission = preliminary | {
            "basis": preliminary["basis"] | {"resumability": "PASS"},
            "resume_validation": result,
        }
        write_json_create(run_root / "full_run_admission.json", final_admission)
        print(json.dumps(result), flush=True)
        return 0

    if not args.resume and (run_root / "checkpoints").exists():
        raise FileExistsError("checkpoints already exist; use --resume")
    execution_wall, execution_cpu = time.perf_counter(), time.process_time()
    tracemalloc.start()
    resumed = 0
    for cell in smoke:
        completion = execute_cell(
            run_root=run_root, cell=cell, event_cache=event_cache,
            depth_manifest=depth, source_prefix_digest=source_digest,
            candidate_family=family, feature_config=feature_config, account=account,
            inventory_cell=inventory_by_id[cell.cell_id],
        )
        resumed += int(completion["resumed"])
        print(json.dumps({"phase": 1, "cell": cell.cell_id, "resumed": completion["resumed"]}), flush=True)
    phase1 = validate_phase1(run_root, smoke, event_cache, family)
    if not (run_root / "phase1_smoke.json").exists():
        write_json_create(run_root / "phase1_smoke.json", phase1)
    elif read_json(run_root / "phase1_smoke.json") != phase1:
        raise ValueError("phase1 checkpoint changed")
    smoke_ids = {cell.cell_id for cell in smoke}
    for completed, cell in enumerate((cell for cell in sample.cells if cell.cell_id not in smoke_ids), start=1):
        completion = execute_cell(
            run_root=run_root, cell=cell, event_cache=event_cache,
            depth_manifest=depth, source_prefix_digest=source_digest,
            candidate_family=family, feature_config=feature_config, account=account,
            inventory_cell=inventory_by_id[cell.cell_id],
        )
        resumed += int(completion["resumed"])
        print(json.dumps({
            "phase": 2, "completed_of_42": completed,
            "cell": cell.cell_id, "resumed": completion["resumed"],
        }), flush=True)
    warm = warm_smoke(run_root, smoke, event_cache, depth, family, account)
    write_json_create(run_root / "warm_smoke.json", warm)
    cell_results, combined = combine_outputs(run_root, sample, family)
    materialization = read_json(run_root / "event_materialization.json")
    runtime = runtime_summary(run_root, cell_results, materialization, warm, combined)
    runtime["execution_process"] = {
        "wall_seconds": time.perf_counter() - execution_wall,
        "cpu_seconds": time.process_time() - execution_cpu,
        "resumed_cell_count": resumed,
    }
    runtime["output_bytes"] = directory_bytes(run_root)
    write_json_create(run_root / "runtime_summary.json", runtime)
    gate = aggregate_gate(cell_results)
    write_json_create(run_root / "gate_summary.json", gate)
    projection = project_full_run(
        run_root, runtime, cell_results, inventory, materialization,
        shutil.disk_usage(run_root.parent).free,
    )
    write_json_create(run_root / "runtime_projection.json", projection)
    failures = sum(1 for cell in cell_results if cell["status"] != "COMPLETED")
    admission_status = (
        "FULL_RUN_ADMITTED_WITH_CONDITIONS"
        if failures == 0 and combined["candidate_cell_count"] == 45 * 663 and gate["reconciled"]
        else "BLOCKED"
    )
    admission = {
        "schema": "mwfd_03_full_run_admission_v1",
        "status": admission_status,
        "basis": {
            "correctness": "PASS" if gate["reconciled"] else "FAIL",
            "completed_cells": len(cell_results), "failed_cells": failures,
            "candidate_cell_reconciliation": combined,
            "warm_cache_identity": warm["status"],
            "resumability": "PENDING_EXPLICIT_RESUME_CHECK",
            "projected_single_worker": projection["single_worker"],
        },
        "conditions": [
            "one worker with the same frozen source/sample/candidate/cache identities",
            "create-only per-cell checkpoints and free-disk preflight",
            "screening-only; no production-authoritative PnL claim",
            "venue remains unknown and NOT_READY daily metadata is not joined",
            "parallel execution requires a separate I/O and memory contention probe",
        ],
        "pnl_used_for_admission": False,
    }
    write_json_create(run_root / "full_run_admission_pre_resume.json", admission)
    completion = {
        "schema": "mwfd_03_probe_completion_v1", "status": "COMPLETED",
        "cells": 45, "candidates": 663,
        "candidate_cells": combined["candidate_cell_count"],
        "candidate_cell_digest": combined["candidate_cell_digest"],
        "gate_reconciled": gate["reconciled"],
        "full_run_admission_pre_resume": admission_status,
        "candidate_family_after": load_candidate_family(args.candidate_summary).manifest(),
        "sample_after": load_probe_sample(
            mwfd02 / "probe_admission.json", mwfd02 / "market_inventory.json"
        ).manifest(),
        "output_bytes_before_completion": directory_bytes(run_root),
    }
    if completion["candidate_family_after"]["canonical_family_digest"] != family.canonical_family_digest:
        raise ValueError("candidate family changed during run")
    if completion["sample_after"]["ordered_cell_digest"] != sample.ordered_cell_digest:
        raise ValueError("sample changed during run")
    write_json_create(run_root / "probe_completion.json", completion)
    tracemalloc.stop()
    print(json.dumps(completion), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
