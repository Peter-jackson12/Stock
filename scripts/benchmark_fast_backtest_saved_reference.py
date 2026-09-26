"""보존된 materialized events와 production exact 결과의 fast parity를 측정한다."""
from __future__ import annotations

import argparse
from dataclasses import asdict
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
    evaluate_candidate,
    feature_config_for_candidates,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Benchmark a saved exact reference")
    value.add_argument("--events-jsonl", required=True)
    value.add_argument("--exact-result", required=True)
    value.add_argument("--trade-date", required=True)
    value.add_argument("--candidate-id", default="268")
    value.add_argument("--output-root", required=True)
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


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    events_path = Path(args.events_jsonl).resolve()
    exact_path = Path(args.exact_result).resolve()
    output = Path(args.output_root).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = perf_counter()
    exact = json.loads(exact_path.read_text(encoding="utf-8"))
    settings = exact["settings"]
    strategy = settings["strategy"]
    candidate = deduplicate_candidates([{
        "candidate_id": args.candidate_id,
        "params": strategy["params"] | {"exit_rule": strategy["exit_rule"]},
    }])[0]
    cutoff = int(exact["input_provenance"]["end_market_second_exclusive"])
    close_ns = int(exact["input_provenance"]["boundary_sentinel"]["received_ns"])
    event_digest = sha256_file(events_path)
    if event_digest != exact["event_sha256"]:
        raise ValueError("saved event digest differs from exact result")

    phase = perf_counter()
    event_cache = build_event_cache(
        iter_events(events_path),
        spec=EventCacheSpec(
            source_dataset_identity=event_digest,
            trade_date=args.trade_date,
            instrument=next(iter(settings["instruments"])),
            cutoff_market_second_exclusive=cutoff,
            policy=strategy["unknown_direction_policy"],
        ),
        cache_root=output / "input_cache",
    )
    cache_seconds = perf_counter() - phase
    if event_cache.event_count != exact["event_count"]:
        raise ValueError("saved event count differs from exact result")
    account = FastAccountAssumptions(
        cash=float(settings["cash"]),
        fee_rate=float(settings["fee_rate"]),
        quantity=int(strategy["quantity"]),
        buy_latency_ns=int(settings["buy_latency_ns"]),
        sell_latency_ns=int(settings["sell_latency_ns"]),
        max_quote_age_ns=int(settings["max_quote_age_ns"]),
        cooldown_ns=int(strategy["cooldown_ns"]),
        close_ns=close_ns,
    )
    phase = perf_counter()
    features = build_causal_features(
        iter_cached_events(event_cache),
        config=feature_config_for_candidates([candidate], account.max_quote_age_ns),
        input_event_digest=event_cache.event_digest,
    )
    feature_seconds = perf_counter() - phase
    write_feature_cache(features, output / "feature_cache")
    phase = perf_counter()
    fast = evaluate_candidate(features, candidate, account=account)
    fast_seconds = perf_counter() - phase
    status, mismatches = compare_fast_exact(fast, exact)
    payload = {
        "schema": "fast_backtest_saved_reference_benchmark_v1",
        "engine": "fast_backtest_v1",
        "screening_only": True,
        "trade_date": args.trade_date,
        "candidate_id": candidate.candidate_id,
        "parameter_identity": candidate.parameter_identity,
        "input": {
            "events_jsonl": str(events_path),
            "exact_result": str(exact_path),
            "source_event_digest": event_digest,
            "event_count": event_cache.event_count,
            "event_digest": event_cache.event_digest,
            "input_cache_id": event_cache.cache_id,
        },
        "fast": asdict(fast),
        "exact": {
            "status": exact["status"],
            "signals": exact["strategy_signals"],
            "fills": exact["account"]["fills"],
            "performance_accounting": exact["performance_accounting"],
        },
        "parity_status": status,
        "mismatches": list(mismatches),
        "elapsed_seconds": {
            "verified_input_cache": cache_seconds,
            "feature_build": feature_seconds,
            "fast_single_candidate": fast_seconds,
            "total": perf_counter() - started,
        },
        "limitations": [
            "saved production exact result is reused; no new production replay",
            "fast result is screening-only",
        ],
    }
    path = output / "benchmark.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(path)
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
