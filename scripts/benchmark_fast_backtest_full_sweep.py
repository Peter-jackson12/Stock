"""보존된 2026-09-21 탐색 입력으로 663개 고유 후보의 fast sweep을 측정한다."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import ctypes
from ctypes import wintypes
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine.tick_ordering import OrderedTick
from research.fast_backtest.exact import compare_fast_exact
from research.fast_backtest.features import build_causal_features, write_feature_cache
from research.fast_backtest.input_cache import EventCacheSpec, build_event_cache, iter_cached_events
from research.fast_backtest.sweep import (
    FastAccountAssumptions,
    deduplicate_candidates,
    feature_config_for_candidates,
    result_record,
    run_fast_sweep,
)
from strategies.nxt_breakout.direction_window import POLICY as QUARANTINE_POLICY


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Benchmark Fast Backtest full unique sweep")
    value.add_argument("--events-jsonl", required=True)
    value.add_argument("--summary-jsonl", required=True)
    value.add_argument("--strict-report", required=True)
    value.add_argument("--output-root", required=True)
    value.add_argument("--expected-events", type=int, default=77_558)
    value.add_argument("--expected-candidates", type=int, default=663)
    value.add_argument("--exact-top-n", type=int, default=10)
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def iter_events(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            value = json.loads(line)
            for name in ("bid_sizes", "ask_sizes"):
                if value.get(name) is not None:
                    value[name] = tuple(value[name])
            yield OrderedTick(**value)


def load_candidates(path: Path):
    records = []
    rows_by_id = {}
    exact_seconds = 0.0
    exact_identities = set()
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            exact_seconds += float(row["elapsed_seconds"])
            exact_identities.add(row["parameter_identity"])
            rows_by_id[str(row["run_index"])] = row
            records.append({
                "candidate_id": str(row["run_index"]),
                "params": row["params"] | {"exit_rule": row["exit_rule"]},
            })
    return records, exact_seconds, exact_identities, rows_by_id


def working_set_bytes() -> int | None:
    if sys.platform != "win32":
        return None

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
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
    events_path = Path(args.events_jsonl).resolve()
    summary_path = Path(args.summary_jsonl).resolve()
    strict_path = Path(args.strict_report).resolve()
    output = Path(args.output_root).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = perf_counter()

    records, old_exact_seconds, exact_identities, rows_by_id = load_candidates(summary_path)
    strict = json.loads(strict_path.read_text(encoding="utf-8"))
    cutoff_second = int(strict["scope"]["end_market_second_exclusive"])
    close_ns = int(strict["scope"]["boundary_sentinel"]["received_ns"])
    candidates = deduplicate_candidates(records)
    if len(candidates) != args.expected_candidates:
        raise ValueError("unexpected unique candidate count")

    source_event_digest = sha256_file(events_path)
    source_summary_digest = sha256_file(summary_path)
    source_identity = hashlib.sha256(
        (source_event_digest + ":" + source_summary_digest).encode("ascii")
    ).hexdigest()
    phase = perf_counter()
    event_cache = build_event_cache(
        iter_events(events_path),
        spec=EventCacheSpec(
            source_dataset_identity=source_identity,
            trade_date="2026-09-21",
            instrument="005930",
            cutoff_market_second_exclusive=cutoff_second,
            policy=QUARANTINE_POLICY,
        ),
        cache_root=output / "input_cache",
    )
    input_cache_seconds = perf_counter() - phase
    if event_cache.event_count != args.expected_events:
        raise ValueError("unexpected event count")

    account = FastAccountAssumptions(
        cash=1_000_000,
        fee_rate=0.001,
        quantity=1,
        buy_latency_ns=1_000_000_000,
        sell_latency_ns=1_000_000_000,
        max_quote_age_ns=2_000_000_000,
        cooldown_ns=10_000_000_000,
        close_ns=close_ns,
    )
    config = feature_config_for_candidates(candidates, account.max_quote_age_ns)
    phase = perf_counter()
    features = build_causal_features(
        iter_cached_events(event_cache), config=config, input_event_digest=event_cache.event_digest
    )
    feature_seconds = perf_counter() - phase
    write_feature_cache(features, output / "feature_cache")

    phase = perf_counter()
    results = run_fast_sweep(features, candidates, account=account)
    sweep_seconds = perf_counter() - phase
    with (output / "fast_results.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        for result in results:
            stream.write(json.dumps(result_record(result), ensure_ascii=False, sort_keys=True) + "\n")

    saved_top_n_exact = []
    for result in results[:args.exact_top_n]:
        historical = rows_by_id[result.candidate_id]
        exact_path = Path(historical["result_path"])
        exact = json.loads(exact_path.read_text(encoding="utf-8"))
        parity_status, mismatches = compare_fast_exact(result, exact)
        saved_top_n_exact.append({
            "candidate_id": result.candidate_id,
            "parameter_identity": result.parameter_identity,
            "exact_result_path": str(exact_path),
            "historical_exact_elapsed_seconds": historical["elapsed_seconds"],
            "parity_status": parity_status,
            "mismatches": list(mismatches),
        })

    payload = {
        "schema": "fast_backtest_full_sweep_benchmark_v1",
        "engine": "fast_backtest_v1",
        "screening_only": True,
        "input": {
            "events_jsonl": str(events_path),
            "summary_jsonl": str(summary_path),
            "strict_report": str(strict_path),
            "event_count": event_cache.event_count,
            "event_digest": event_cache.event_digest,
            "source_event_digest": source_event_digest,
            "source_summary_digest": source_summary_digest,
            "input_cache_id": event_cache.cache_id,
            "historical_exact_runs": len(records),
            "historical_exact_unique_identities": len(exact_identities),
            "unique_fast_candidates": len(candidates),
        },
        "elapsed_seconds": {
            "historical_production_exact_total": old_exact_seconds,
            "verified_input_cache": input_cache_seconds,
            "feature_build": feature_seconds,
            "fast_unique_sweep": sweep_seconds,
            "fast_total": perf_counter() - started,
        },
        "peak_working_set_bytes": working_set_bytes(),
        "saved_top_n_exact": saved_top_n_exact,
        "top_screening_results": [result_record(result) for result in results[:10]],
        "candidate_268": next(
            result_record(result) for result in results if "268" in result.aliases
        ),
        "limitations": [
            "screening-only ordering; production exact remains authoritative",
            "historical exact total includes 693 runs, including 30 duplicate executions",
            "selected-v2 bounded input only; whole stream remains unassessed",
            "benchmark reuses frozen materialized events and does not retune candidate 268",
        ],
    }
    path = output / "benchmark.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
