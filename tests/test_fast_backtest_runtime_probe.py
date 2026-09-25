from dataclasses import replace
import hashlib
import json

import pytest

from engine.tick_ordering import OrderedTick
from research.fast_backtest.feasibility import EntryFeasibilityState
from research.fast_backtest.execution_depth import ExecutionDepthRecord, ExecutionDepthSpec, ExecutionDepthWriter
from research.fast_backtest.runtime_probe import (
    ProbeCell,
    ProbeEventCacheWriter,
    ProbeSample,
    _expected_probe_selection,
    json_digest,
    load_candidate_family,
    load_gate_cache,
    load_probe_cell_events,
    load_probe_event_cache,
    load_probe_sample,
    write_gate_cache,
)
from research.fast_backtest.sweep import parameter_identity
from research.fast_backtest.sweep import FastAccountAssumptions, feature_config_for_candidates
from scripts.run_mwfd_03_probe import execute_cell, verify_cell_checkpoint, warm_smoke


def event(seq, code, kind="quote"):
    base = OrderedTick(
        source="fixture", session_id="session", seq=seq, received_ns=seq,
        code=code, venue="unknown", kind=kind, market_second=32400,
        bid=99, ask=100, bid_size=1, ask_size=1,
        bid_sizes=(1, 1, 1), ask_sizes=(1, 1, 1),
    )
    return replace(base, price=100, volume=1, is_buy=True) if kind == "trade" else base


def sample(cells):
    values = tuple(
        ProbeCell(index + 1, f"{code}=unknown", code, "unknown", tier, count, f"hash-{index}")
        for index, (code, tier, count) in enumerate(cells)
    )
    return ProbeSample(values, "a" * 64, "b" * 64, json_digest([cell.to_dict() for cell in values]), "rule", len(values))


def test_sample_admission_recomputes_exact_strata_and_rejects_tamper(tmp_path):
    cells = []
    for index in range(45):
        code = f"{index:06d}"
        cells.append({
            "cell_id": f"{code}=unknown", "code": code, "venue": "unknown",
            "admission": "ELIGIBLE_FOR_FAST_PROBE",
            "activity": {"event_count": index + 1},
        })
    inventory = {"session_id": "session", "cells": cells}
    selected = _expected_probe_selection(inventory, "session")
    plan = {
        "status": "ADMITTED", "eligible_cells": 45,
        "selection_rule": "fixed", "selected_cells": selected,
    }
    inventory_path, plan_path = tmp_path / "inventory.json", tmp_path / "plan.json"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    loaded = load_probe_sample(plan_path, inventory_path)
    assert len(loaded.cells) == 45
    assert [cell.tier for cell in loaded.cells].count("high") == 15
    assert len({cell.cell_id for cell in loaded.cells}) == 45

    plan["selected_cells"][0]["event_count"] += 1
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="PROBE_ADMISSION_INVALID"):
        load_probe_sample(plan_path, inventory_path)


def test_candidate_family_freezes_source_count_identity_and_order(tmp_path):
    path = tmp_path / "summary.jsonl"
    params = [
        {"spread_max_pct": 0.1, "exit_rule": "fixed"},
        {"spread_max_pct": 0.2, "exit_rule": "fixed"},
        {"spread_max_pct": 0.1, "exit_rule": "fixed"},
    ]
    with path.open("w", encoding="utf-8") as stream:
        for index, value in enumerate(params):
            source = dict(value)
            exit_rule = source.pop("exit_rule")
            canonical = source | {"exit_rule": exit_rule}
            stream.write(json.dumps({
                "run_index": index, "params": source, "exit_rule": exit_rule,
                "parameter_identity": parameter_identity(canonical),
            }) + "\n")
    family = load_candidate_family(path, expected_unique=2)
    assert family.source_record_count == 3
    assert family.source_identity_count == len(family.candidates) == 2
    assert len(family.ordered_identity_digest) == len(family.canonical_family_digest) == 64


def test_probe_event_cache_roundtrip_counts_and_digest_fail_closed(tmp_path):
    selected = sample([("000001", "high", 2), ("000002", "low", 1)])
    writer = ProbeEventCacheWriter(
        tmp_path / "cache", source_prefix_digest="a" * 64, sample=selected,
    )
    writer.append(event(1, "000001"))
    writer.append(event(2, "999999"))
    writer.append(event(3, "000002", "trade"))
    writer.append(event(4, "000001", "trade"))
    manifest = writer.finish(observed_prefix_digest="a" * 64)
    loaded = load_probe_event_cache(
        manifest.cache_dir,
        expected_source_prefix_digest="a" * 64,
        expected_sample_digest=selected.ordered_cell_digest,
    )
    assert loaded.manifest["event_count"] == 3
    assert [item.seq for item in load_probe_cell_events(loaded, "000001=unknown")] == [1, 4]
    with pytest.raises(ValueError, match="source digest mismatch"):
        load_probe_event_cache(
            manifest.cache_dir,
            expected_source_prefix_digest="b" * 64,
            expected_sample_digest=selected.ordered_cell_digest,
        )


def test_probe_event_writer_does_not_publish_wrong_counts_or_source_digest(tmp_path):
    selected = sample([("000001", "high", 2)])
    writer = ProbeEventCacheWriter(tmp_path / "count", source_prefix_digest="a" * 64, sample=selected)
    writer.append(event(1, "000001"))
    with pytest.raises(ValueError, match="counts"):
        writer.finish(observed_prefix_digest="a" * 64)
    assert not (tmp_path / "count").exists()

    writer = ProbeEventCacheWriter(tmp_path / "digest", source_prefix_digest="a" * 64, sample=sample([("000001", "high", 1)]))
    writer.append(event(1, "000001"))
    with pytest.raises(ValueError, match="source prefix digest"):
        writer.finish(observed_prefix_digest="b" * 64)
    assert not (tmp_path / "digest").exists()


def test_gate_cache_roundtrip_and_identity_checks(tmp_path):
    states = {
        2: EntryFeasibilityState(2, 2, "PASS", "ok", 1, 1, 100_000_000),
        3: EntryFeasibilityState(3, 3, "UNKNOWN", "stale_quote", 1, 1, 100_000_000),
    }
    write_gate_cache(
        tmp_path / "gate", states=states, event_digest="a" * 64, depth_cache_id="depth",
    )
    assert load_gate_cache(
        tmp_path / "gate", expected_event_digest="a" * 64, expected_depth_cache_id="depth",
    ) == states
    with pytest.raises(ValueError, match="event digest mismatch"):
        load_gate_cache(
            tmp_path / "gate", expected_event_digest="b" * 64, expected_depth_cache_id="depth",
        )


def test_cell_checkpoint_and_warm_cache_preserve_economic_identity(tmp_path):
    candidate_source = tmp_path / "candidates.jsonl"
    with candidate_source.open("w", encoding="utf-8") as stream:
        for index, spread in enumerate((0.1, 0.2)):
            params = {"spread_max_pct": spread}
            canonical = params | {"exit_rule": "fixed"}
            stream.write(json.dumps({
                "run_index": index, "params": params, "exit_rule": "fixed",
                "parameter_identity": parameter_identity(canonical),
            }) + "\n")
    family = load_candidate_family(candidate_source, expected_unique=2)
    selected = sample([("000001", "high", 2)])
    event_writer = ProbeEventCacheWriter(
        tmp_path / "events", source_prefix_digest="a" * 64, sample=selected,
    )
    quote_event, trade_event = event(1, "000001"), event(2, "000001", "trade")
    event_writer.append(quote_event)
    event_writer.append(trade_event)
    event_writer.finish(observed_prefix_digest="a" * 64)
    event_cache = load_probe_event_cache(
        tmp_path / "events",
        expected_source_prefix_digest="a" * 64,
        expected_sample_digest=selected.ordered_cell_digest,
    )

    spec = ExecutionDepthSpec(
        source="fixture", session_id="session", market_date="2026-09-21",
        cutoff_market_second_exclusive=36000, source_prefix_digest="a" * 64,
        source_path_identity={"size": 1}, code_provenance={"test": "hash"},
    )
    depth_writer = ExecutionDepthWriter(tmp_path / "depth", spec)
    depth_writer.append(ExecutionDepthRecord(
        source="fixture", session_id="session", seq=1, received_ns=1,
        code="000001", venue="unknown",
        ask_prices=tuple(range(100, 110)), ask_sizes=(1_000_000,) + (0,) * 9,
        bid_prices=tuple(range(99, 89, -1)), bid_sizes=(1,) * 10,
        ask_status="COMPLETE", ask_reason=None, bid_status="COMPLETE", bid_reason=None,
        ask_depth_notional_10=100_000_000, bid_depth_notional_10=1_000,
        top_status="VALID", top_reason=None,
    ))
    depth = depth_writer.finish(observed_prefix_digest="a" * 64)
    account = FastAccountAssumptions(
        cash=1_000_000, fee_rate=0.001, quantity=1,
        buy_latency_ns=0, sell_latency_ns=0, max_quote_age_ns=100,
        cooldown_ns=10, close_ns=100,
    )
    inventory = {
        "event_weighted_gate": {
            "evaluated": 1, "PASS": 1, "FAIL": 0, "UNKNOWN": 0,
            "pass_rate": 1.0, "reasons": {"ask10_notional_gte_threshold": 1},
        },
        "clock_time_gate": {"PASS": 1, "FAIL": 0, "UNKNOWN": 0},
    }
    run_root = tmp_path / "run"
    run_root.mkdir()
    cell = selected.cells[0]
    execute_cell(
        run_root=run_root, cell=cell, event_cache=event_cache,
        depth_manifest=depth, source_prefix_digest="a" * 64,
        candidate_family=family,
        feature_config=feature_config_for_candidates(family.candidates, 100),
        account=account, inventory_cell=inventory,
    )
    checkpoint = run_root / "checkpoints" / "01-000001-unknown"
    cold = verify_cell_checkpoint(
        checkpoint, candidate_family=family,
        event_digest=event_cache.manifest["cells"][cell.cell_id]["event_digest"],
    )
    warm = warm_smoke(run_root, (cell,), event_cache, depth, family, account)
    assert warm["status"] == "PASS"
    assert warm["cells"][0]["result_digest"] == cold["candidate_results_digest"]
