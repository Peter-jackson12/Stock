from dataclasses import asdict, replace
import json

import pytest

from engine.tick_ordering import OrderedTick
from research.fast_backtest.input_cache import (
    EventCacheSpec,
    build_event_cache,
    iter_cached_events,
    load_event_cache,
)
from research.fast_backtest.plan import FastBacktestPlan


def event(seq=1, second=32400, **changes):
    base = OrderedTick(
        source="fixture",
        session_id="session",
        seq=seq,
        received_ns=seq,
        code="005930",
        venue="unknown",
        kind="quote",
        bid=100,
        ask=101,
        bid_size=10,
        ask_size=10,
        market_second=second,
        bid_sizes=(10, 10, 10),
        ask_sizes=(10, 10, 10),
    )
    return replace(base, **changes)


def plan(**changes):
    base = dict(
        trade_dates=("2026-09-21",),
        instruments=("005930",),
        universe_source="fixture",
        universe_mode="causal_preopen",
        historical_metadata_source="fixture.csv",
        cheap_filter_spec={},
        tick_input_provenance={"source_dataset_identity": "fixture", "policy": "strict"},
        cutoff_market_second_exclusive=36000,
        account_assumptions={"cash": 1000000},
        candidate_source="fixture.json",
        parameter_identities=("abc",),
        exact_top_n=1,
        exact_ranking=("net_pnl_desc", "parameter_identity_asc"),
        output_directory="out",
        code_revision="deadbeef",
    )
    return FastBacktestPlan(**(base | changes))


def test_plan_defaults_to_causal_screening_and_has_stable_digest(tmp_path):
    value = plan()
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(value.to_dict()), encoding="utf-8")
    loaded = FastBacktestPlan.read(path)
    assert loaded.digest == value.digest
    assert loaded.non_causal is False
    assert loaded.screening_only is True
    assert loaded.to_dict()["size_filter_basis"] == "total_market_cap_proxy"


def test_posthoc_plan_requires_explicit_opt_in():
    with pytest.raises(ValueError, match="explicit opt-in"):
        plan(universe_mode="posthoc_same_day")


def test_input_cache_is_content_addressed_verified_and_create_only(tmp_path):
    events = [event(1), event(2, kind="trade", price=101, volume=10, is_buy=True)]
    spec = EventCacheSpec("dataset-a", "2026-09-21", "005930", 36000, "strict")
    first = build_event_cache(events, spec=spec, cache_root=tmp_path)
    second = build_event_cache(events, spec=spec, cache_root=tmp_path)

    assert first.cache_id == second.cache_id
    assert first.event_count == 2
    assert list(iter_cached_events(first)) == events
    assert load_event_cache(first.cache_dir).event_digest == first.event_digest


def test_input_cache_identity_changes_by_date_instrument_cutoff_and_policy(tmp_path):
    base = EventCacheSpec("dataset", "2026-09-21", "005930", 36000, "strict")
    first = build_event_cache([event()], spec=base, cache_root=tmp_path)
    changed = EventCacheSpec("dataset", "2026-09-22", "005930", 36000, "strict")
    second = build_event_cache([event()], spec=changed, cache_root=tmp_path)
    assert first.cache_id != second.cache_id


def test_input_cache_rejects_event_at_exclusive_cutoff(tmp_path):
    spec = EventCacheSpec("dataset", "2026-09-21", "005930", 36000, "strict")
    with pytest.raises(ValueError, match="pre-cutoff"):
        build_event_cache([event(second=36000)], spec=spec, cache_root=tmp_path)


def test_input_cache_detects_payload_tampering(tmp_path):
    spec = EventCacheSpec("dataset", "2026-09-21", "005930", 36000, "strict")
    manifest = build_event_cache([event()], spec=spec, cache_root=tmp_path)
    with (manifest.cache_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(asdict(event(2))) + "\n")
    with pytest.raises(ValueError, match="payload verification"):
        load_event_cache(manifest.cache_dir)
