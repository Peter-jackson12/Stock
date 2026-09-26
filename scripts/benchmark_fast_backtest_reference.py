"""고정 #268 selected-v2 reference를 fast cache와 production exact로 비교한다.

이 스크립트는 frozen/local NTFS raw와 기존 eligible selected-v2 보고서를 요구한다.
private event iterator 사용은 기존 selected runner와 동일한 입력을 재사용하기 위한 v1
경계이며 입력 적격성을 새로 승격하지 않는다.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter
import tracemalloc

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine.nxt_selected_prefix_smoke import (
    _load_selected_report,
    _load_strict_report,
    _same_path,
    _selected_v2_events,
    run_nxt_selected_prefix_smoke,
)
from research.fast_backtest.exact import compare_fast_exact
from research.fast_backtest.features import build_causal_features, write_feature_cache
from research.fast_backtest.input_cache import EventCacheSpec, build_event_cache, event_line, iter_cached_events
from research.fast_backtest.sweep import (
    FastAccountAssumptions,
    deduplicate_candidates,
    evaluate_candidate,
    feature_config_for_candidates,
    materialize_strategy_params,
)
from strategies.nxt_breakout.direction_window import POLICY as QUARANTINE_POLICY


REFERENCE_PARAMS = {
    "spread_max_pct": 0.0025,
    "buy_ratio_min": 0.55,
    "obi_min_ratio": 1.0,
    "min_vol_15t": 10,
    "recent_ticks": 15,
    "breakout_window_sec": 30,
    "session_start_sec": 32400,
    "exit_rule": "tick_trail",
    "trail_ticks": 5,
    "stop_loss_pct": -0.0025,
}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Benchmark fixed Fast Backtest reference #268")
    value.add_argument("--raw", required=True)
    value.add_argument("--selected-report", required=True)
    value.add_argument("--output-root", required=True)
    return value


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    output = Path(args.output_root).resolve()
    output.mkdir(parents=True, exist_ok=False)
    selected_path, selected_bytes, selected = _load_selected_report(args.selected_report)
    strict_path, _, strict = _load_strict_report(selected["input"]["strict_prefix_report_path"])
    raw_path = Path(args.raw).absolute()
    if not _same_path(raw_path, selected["input"]["raw_path"]) or not _same_path(raw_path, strict["input"]["path"]):
        raise ValueError("raw path must match both selected and strict reports")
    instruments = dict(selected["input"]["selected_instruments"])
    if instruments != {"005930": "unknown"}:
        raise ValueError("reference benchmark requires 005930=unknown")
    cutoff_second = strict["scope"]["end_market_second_exclusive"]
    close_ns = strict["scope"]["boundary_sentinel"]["received_ns"]
    expected = (
        selected["selected_policy_result"]["counts"]["selected_clean_ticks"]
        + selected["selected_policy_result"]["quarantine"]["selected_unknown_direction_pairs"]
    )

    started = perf_counter()
    tracemalloc.start()
    phase = perf_counter()
    events = tuple(_selected_v2_events(
        raw_path,
        selected_report=selected,
        strict_report=strict,
        instruments=instruments,
    ))
    materialization_seconds = perf_counter() - phase
    if len(events) != expected:
        raise ValueError("selected event count changed")
    selected_digest = hashlib.sha256()
    for event in events:
        selected_digest.update(event_line(event))

    phase = perf_counter()
    event_cache = build_event_cache(
        events,
        spec=EventCacheSpec(
            source_dataset_identity=hashlib.sha256(selected_bytes).hexdigest(),
            trade_date="2026-09-21",
            instrument="005930",
            cutoff_market_second_exclusive=cutoff_second,
            policy=QUARANTINE_POLICY,
        ),
        cache_root=output / "input_cache",
    )
    cache_seconds = perf_counter() - phase
    if event_cache.event_digest != selected_digest.hexdigest():
        raise ValueError("selected event digest changed while caching")

    candidate = deduplicate_candidates([{"candidate_id": "268", "params": REFERENCE_PARAMS}])[0]
    exact_params, exact_exit_rule = materialize_strategy_params(candidate.params)
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
    config = feature_config_for_candidates([candidate], account.max_quote_age_ns)
    phase = perf_counter()
    features = build_causal_features(
        iter_cached_events(event_cache),
        config=config,
        input_event_digest=event_cache.event_digest,
    )
    feature_seconds = perf_counter() - phase
    write_feature_cache(features, output / "feature_cache")
    phase = perf_counter()
    fast = evaluate_candidate(features, candidate, account=account)
    fast_seconds = perf_counter() - phase

    phase = perf_counter()
    exact_path = run_nxt_selected_prefix_smoke(
        raw_path,
        selected_path,
        output_root=output / "exact",
        simulator_config={
            "instruments": instruments,
            "cash": 1_000_000,
            "fee_rate": "0.001",
            "max_quote_age_ns": 2_000_000_000,
            "buy_latency_ns": 1_000_000_000,
            "sell_latency_ns": 1_000_000_000,
            "cancel_latency_ns": 1_000_000_000,
        },
        quantity=1,
        exit_rule=exact_exit_rule,
        cooldown_ns=10_000_000_000,
        params=exact_params,
        dataset_label="fast-backtest-v1-reference-268",
    )
    exact_seconds = perf_counter() - phase
    exact = json.loads(exact_path.read_text(encoding="utf-8"))
    parity_status, mismatches = compare_fast_exact(fast, exact)
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    payload = {
        "schema": "fast_backtest_reference_benchmark_v1",
        "engine": "fast_backtest_v1",
        "screening_only": True,
        "candidate_id": "268",
        "parameter_identity": candidate.parameter_identity,
        "input": {
            "selected_report": str(selected_path),
            "strict_report": str(strict_path),
            "raw_path": str(raw_path),
            "event_count": len(events),
            "event_digest": event_cache.event_digest,
            "input_cache_id": event_cache.cache_id,
        },
        "fast": asdict(fast),
        "exact_result_path": str(exact_path),
        "exact": {
            "status": exact["status"],
            "signals": exact["strategy_signals"],
            "fills": exact["account"]["fills"],
            "performance_accounting": exact["performance_accounting"],
        },
        "parity_status": parity_status,
        "mismatches": list(mismatches),
        "known_reference": {
            "buy_price": 264000,
            "sell_price": 269000,
            "fees": 533,
            "net_pnl": 4467,
        },
        "elapsed_seconds": {
            "selected_event_materialization": materialization_seconds,
            "verified_input_cache": cache_seconds,
            "feature_build": feature_seconds,
            "fast_single_candidate": fast_seconds,
            "production_exact_single_candidate": exact_seconds,
            "total": perf_counter() - started,
        },
        "peak_traced_memory_bytes": peak_bytes,
        "limitations": [
            "selected-v2 bounded input only; whole stream remains unassessed",
            "performance_research_assessed remains false",
            "benchmark does not retune candidate 268",
        ],
    }
    path = output / "benchmark.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")
    print(path)
    return 0 if parity_status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
