"""Fast Backtest v1 create-only end-to-end research pipeline."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable, Mapping

from engine.tick_ordering import OrderedTick
from research.fast_backtest.exact import production_exact_runner, run_exact_bridge
from research.fast_backtest.features import build_causal_features, write_feature_cache
from research.fast_backtest.input_cache import (
    EventCacheSpec,
    build_event_cache,
    iter_cached_events,
)
from research.fast_backtest.plan import FastBacktestPlan
from research.fast_backtest.sweep import (
    FastAccountAssumptions,
    deduplicate_candidates,
    feature_config_for_candidates,
    result_record,
    run_fast_sweep,
)
from research.fast_backtest.universe import (
    UniverseFilter,
    apply_cheap_filters,
    read_metadata_csv,
    select_historical_snapshot,
)


SCHEMA = "fast_backtest_run_manifest_v1"


def _write(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def read_events_jsonl(path: str | Path) -> Iterable[OrderedTick]:
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            for name in ("bid_sizes", "ask_sizes"):
                if value.get(name) is not None:
                    value[name] = tuple(value[name])
            try:
                yield OrderedTick(**value)
            except TypeError as exc:
                raise ValueError(f"invalid OrderedTick JSON at line {line_number}") from exc


def read_candidates(path: str | Path) -> list[Mapping[str, Any]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    records = value.get("candidates") if isinstance(value, Mapping) else value
    if not isinstance(records, list) or not records:
        raise ValueError("nonempty candidate list required")
    return records


def run_fast_backtest_pipeline(
    *,
    plan: FastBacktestPlan,
    metadata_csv: str | Path,
    events_jsonl: str | Path,
    candidate_records: Iterable[Mapping[str, Any]],
) -> Path:
    if len(plan.trade_dates) != 1 or len(plan.instruments) != 1:
        raise ValueError("Fast Backtest v1 runner currently accepts one trade date/instrument per run")
    output = Path(plan.output_directory).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = perf_counter()

    rows = read_metadata_csv(metadata_csv)
    universe = select_historical_snapshot(
        rows,
        trade_date=plan.trade_dates[0],
        mode=plan.universe_mode,
        posthoc_same_day_opt_in=plan.posthoc_same_day_opt_in,
    )
    filters = UniverseFilter(**dict(plan.cheap_filter_spec))
    universe = apply_cheap_filters(universe, filters)
    selected_codes = {row.code for row in universe.rows}
    if plan.instruments[0] not in selected_codes:
        raise ValueError("planned instrument was removed by the historical cheap universe")
    _write(output / "plan.json", plan.to_dict())
    _write(output / "universe.json", {
        **{key: value for key, value in asdict(universe).items() if key != "rows"},
        "row_count": len(universe.rows),
        "selected_instrument": plan.instruments[0],
        "cheap_filter_spec": asdict(filters),
    })

    records = tuple(candidate_records)
    candidates = deduplicate_candidates(records)
    planned_identities = tuple(sorted(plan.parameter_identities))
    actual_identities = tuple(sorted(candidate.parameter_identity for candidate in candidates))
    if planned_identities != actual_identities:
        raise ValueError("candidate identities do not match the preregistered plan")

    provenance = dict(plan.tick_input_provenance)
    cache_spec = EventCacheSpec(
        source_dataset_identity=provenance["source_dataset_identity"],
        trade_date=plan.trade_dates[0],
        instrument=plan.instruments[0],
        cutoff_market_second_exclusive=plan.cutoff_market_second_exclusive,
        policy=provenance["policy"],
    )
    phase = perf_counter()
    event_cache = build_event_cache(
        read_events_jsonl(events_jsonl),
        spec=cache_spec,
        cache_root=output / "input_cache",
    )
    input_seconds = perf_counter() - phase

    assumptions = dict(plan.account_assumptions)
    account = FastAccountAssumptions(
        cash=float(assumptions["cash"]),
        fee_rate=float(assumptions["fee_rate"]),
        quantity=int(assumptions["quantity"]),
        buy_latency_ns=int(assumptions["buy_latency_ns"]),
        sell_latency_ns=int(assumptions["sell_latency_ns"]),
        max_quote_age_ns=int(assumptions["max_quote_age_ns"]),
        cooldown_ns=int(assumptions["cooldown_ns"]),
        close_ns=int(assumptions["close_ns"]),
    )
    feature_config = feature_config_for_candidates(candidates, account.max_quote_age_ns)
    phase = perf_counter()
    feature_cache = build_causal_features(
        iter_cached_events(event_cache),
        config=feature_config,
        input_event_digest=event_cache.event_digest,
    )
    feature_seconds = perf_counter() - phase
    write_feature_cache(feature_cache, output / "feature_cache")

    phase = perf_counter()
    fast_results = run_fast_sweep(feature_cache, candidates, account=account)
    sweep_seconds = perf_counter() - phase
    _write(output / "fast_results.json", {
        "schema": "fast_backtest_screening_results_v1",
        "screening_only": True,
        "results": [result_record(result) for result in fast_results],
    })

    exact_root = output / "exact"
    exact_root.mkdir(exist_ok=False)
    simulator_config = {
        "source": event_cache.source,
        "session_id": event_cache.session_id,
        "instruments": {plan.instruments[0]: feature_cache.rows[0].venue},
        "cash": assumptions["cash"],
        "fee_rate": assumptions["fee_rate"],
        "max_quote_age_ns": account.max_quote_age_ns,
        "buy_latency_ns": account.buy_latency_ns,
        "sell_latency_ns": account.sell_latency_ns,
        "cancel_latency_ns": int(assumptions["cancel_latency_ns"]),
    }
    exact_runner = production_exact_runner(
        iter_cached_events(event_cache),
        output_root=exact_root,
        dataset_label=f"fast-backtest-v1:{plan.digest}",
        simulator_config=simulator_config,
        close_ns=account.close_ns,
        quantity=account.quantity,
        cooldown_ns=account.cooldown_ns,
        unknown_direction_policy=provenance["policy"],
        input_provenance={
            "kind": "fast_backtest_verified_event_cache_v1",
            "event_cache_id": event_cache.cache_id,
            "event_digest": event_cache.event_digest,
            "source_dataset_identity": cache_spec.source_dataset_identity,
            "performance_research_assessed": False,
        },
    )
    phase = perf_counter()
    exact_records = run_exact_bridge(
        fast_results,
        candidates,
        top_n=plan.exact_top_n,
        exact_runner=exact_runner,
    )
    exact_seconds = perf_counter() - phase
    _write(output / "exact_bridge.json", {
        "schema": "fast_backtest_exact_bridge_v1",
        "records": [asdict(record) for record in exact_records],
    })

    parity = (
        "NOT_RUN" if not exact_records
        else "PASS" if all(record.parity_status == "PASS" for record in exact_records)
        else "MISMATCH"
    )
    manifest = {
        "schema": SCHEMA,
        "engine": "fast_backtest_v1",
        "screening_only": True,
        "code_revision": plan.code_revision,
        "plan_digest": plan.digest,
        "universe_source": plan.universe_source,
        "metadata_as_of_date": universe.metadata_as_of_date,
        "universe_mode": universe.universe_mode,
        "non_causal": universe.non_causal,
        "universe_row_count": len(universe.rows),
        "size_filter_basis": universe.size_filter_basis,
        "historical_float_market_cap_status": "UNAVAILABLE",
        "input_cache_id": event_cache.cache_id,
        "input_event_digest": event_cache.event_digest,
        "input_event_count": event_cache.event_count,
        "feature_cache_digest": feature_cache.feature_digest,
        "feature_row_count": len(feature_cache.rows),
        "candidate_count": len(records),
        "deduplicated_count": len(candidates),
        "exact_replay_candidate_ids": [record.candidate_id for record in exact_records],
        "parity_status": parity,
        "elapsed_seconds": {
            "input_materialization": input_seconds,
            "feature_build": feature_seconds,
            "fast_sweep": sweep_seconds,
            "top_n_exact": exact_seconds,
            "total": perf_counter() - started,
        },
        "limitations": [
            "fast results are screening_only and are not production-authoritative",
            "historical float-market-cap unavailable; total market cap is the size-filter proxy",
            "one trade date and instrument per v1 run",
            "input eligibility is inherited from caller provenance and is not upgraded by this pipeline",
        ],
    }
    path = output / "manifest.json"
    _write(path, manifest)
    return path
