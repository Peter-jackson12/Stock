"""실데이터 없이 계약 값·저장·재현성·이전 조회기 호환을 검증한다."""
from dataclasses import fields, replace
from decimal import Decimal, Inexact, localcontext
import hashlib
import json
from pathlib import Path

import pytest

from collector.kiwoom.tick_normalizer import normalize_tick
from collector.raw_v2 import RawV2Writer
from engine.tick_ordering import OrderedTick
import engine.tick_research_run as runner
from execution.reality_contract import input_capabilities, simulation_contract
from execution.tick_simulator import TickSimulator
from scripts.inspect_tick_research import inspect


def config(**changes):
    return dict(source="test", session_id="s", code="005930", venue="unknown",
                cash=100000, max_quote_age_ns=100, buy_latency_ns=5,
                sell_latency_ns=7, cancel_latency_ns=2, fee_rate="0.001") | changes


def events():
    quote = OrderedTick("test", "s", 1, 0, "005930", "unknown", "quote",
                        bid=10000, ask=10001, bid_size=10, ask_size=3,
                        market_second=32399, bid_sizes=(10, 10, 10), ask_sizes=(3, 3, 3))
    return [quote, replace(quote, seq=2, received_ns=1, kind="trade",
                           market_second=32400, price=10001, volume=30, is_buy=True)]


def run(root, stream=None, **changes):
    args = dict(output_root=root, dataset_label="synthetic", simulator_config=config(),
                close_ns=20, quantity=2)
    return runner.run_research(events() if stream is None else stream, **(args | changes))


def load(path):
    return json.loads(path.read_bytes())


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def test_contract_uses_validated_values_and_states_unmodeled_assumptions():
    sim = TickSimulator(**config(buy_latency_ns=0, sell_latency_ns=11,
                                cancel_latency_ns=13, max_quote_age_ns=17,
                                fee_rate=Decimal("0.0025")))
    contract = simulation_contract(sim)
    assert (contract["schema"], contract["version"]) == ("simulation_reality_v1", 1)
    assert contract["quote_depth_used_by_execution"] == 1
    assert contract["fill_model"] == "validated_best_ask_buy_best_bid_sell"
    assert contract["quote_validation_policy"] == "positive_two_sided_top_v1"
    assert contract["order_entry_latency_model"]["buy_ns"] == 0
    assert contract["order_entry_latency_model"]["sell_ns"] == 11
    assert contract["cancel_latency_model"]["value_ns"] == 13
    assert contract["stale_quote_policy"]["max_age_ns"] == 17
    assert contract["stale_quote_policy"]["eligible_age"] == "age_ns_lte_max_age_ns"
    assert contract["fee_model"]["rate"] == "0.0025"
    assert contract["fee_model"]["research_requires_explicit_rate"] is True
    assert contract["partial_fill_policy"] == "retain_remainder_until_filled_cancelled_or_expired"
    assert contract["queue_position_model"] == "none"
    assert contract["market_impact_model"] == "not_modeled"
    assert contract["feed_latency_model"]["status"] == "not_calibrated"
    assert contract["order_response_latency_model"]["status"] == "not_modeled"
    assert "value_ns" not in contract["feed_latency_model"]
    assert "value_ns" not in contract["order_response_latency_model"]
    assert contract["slippage_model"]["status"] == "not_modeled_separately"
    assert contract["liquidity_replenishment_policy"]["external_trade_consumption"] == "not_modeled"
    assert contract["same_timestamp_policy"]["cancel_fill_tie"] == "effective_cancellations_before_any_fills"
    assert contract["close_boundary_policy"]["strategy_on_close_or_timer"] is False


def test_json_is_deterministic_native_and_builders_do_not_share_mutable_state():
    sim = TickSimulator(**config())
    first = simulation_contract(sim)
    caps = input_capabilities(sim)
    assert json.loads(canonical(first)) == first
    assert json.loads(canonical(caps)) == caps
    assert canonical(first) == canonical(simulation_contract(sim))
    first["same_timestamp_policy"]["phases"].clear()
    caps["quote_snapshots"]["execution_depth_used"] = 10
    assert len(simulation_contract(sim)["same_timestamp_policy"]["phases"]) == 6
    assert input_capabilities(sim)["quote_snapshots"]["execution_depth_used"] == 1
    assert sim.now == 0 and sim.orders == {} and sim.fills == []


def test_decimal_context_is_identified_but_sticky_flags_are_not():
    sim = TickSimulator(**config())
    with localcontext() as context:
        context.prec = 28
        context.clear_flags()
        before = simulation_contract(sim)
        context.flags[Inexact] = True
        assert simulation_contract(sim) == before
        context.prec = 40
        after = simulation_contract(sim)
        assert after != before and after["decimal_context"]["prec"] == 40
        assert "flags" not in after["decimal_context"]


@pytest.mark.parametrize("venue", ["unknown", "NXT", "KRX"])
def test_input_format_support_does_not_certify_venue_or_field_coverage(venue):
    caps = input_capabilities(TickSimulator(**config(venue=venue)))
    names = {field.name for field in fields(OrderedTick)}
    assert caps["execution_input"] == "OrderedTick"
    assert caps["scope"] == "format_support_not_observed_dataset_capabilities"
    assert caps["trade_ticks"] == {"supported": True, "price_volume_direction": "nullable_fields"}
    assert caps["quote_snapshots"]["normalized_price_depth_per_side"] == 1
    assert caps["quote_snapshots"]["strategy_entry_quantity_depth_required"] == 3
    assert caps["market_by_order"] == {"supported": False, "exchange_order_ids": False}
    assert {"bid", "ask", "bid_sizes", "ask_sizes", "received_ns", "is_buy"} <= names
    assert not {"exchange_ts_raw", "received_at_utc", "exchange_order_id", "bid_prices", "ask_prices"} & names
    assert caps["exchange_timestamp"]["in_execution_input"] is False
    assert caps["exchange_timestamp"]["precision_verified"] is False
    assert caps["venue"] == {"label": venue, "verification_status": "not_verified"}
    assert caps["dataset_evidence"]["observed_field_coverage"] == "not_inspected_by_contract"
    assert caps["dataset_evidence"]["capture_completeness"] == "not_verified"
    assert caps["trade_direction_policy"]["actual_normalization_policy"] == "not_verified_by_contract"


@pytest.mark.parametrize("raw_time,precision", [("090000", "second"), (None, "unknown")])
def test_normalizer_time_support_is_not_a_feed_latency_measurement(raw_time, precision):
    normalized = normalize_tick(
        source="test", session_id="s", seq=1, received_ns=123,
        received_at_utc="2026-09-22T00:00:02+00:00", code="005930", venue="unknown",
        real_type="주식체결", fids={"10": "10001", "15": "+2", "20": raw_time},
        price_policy="positive_only", direction_policy="signed_volume")
    assert normalized.source_time_precision == precision
    assert normalized.exchange_ts_raw == raw_time
    assert normalized.event.market_second == 32402  # Receipt KST, not FID20.
    assert normalized.event.received_ns == 123
    caps = input_capabilities(TickSimulator(**config()))
    mapping = caps["upstream_format_support"]["kiwoom_fids_prototype_1"]
    assert mapping["exchange_time_fields"] == {"trade": "FID20", "quote": "FID21"}
    assert mapping["market_second"] == "KST_wall_second_from_received_at_utc_not_FID20_or_FID21"
    assert caps["exchange_timestamp"]["exchange_local_clock_synchronization"] == "not_verified"
    assert simulation_contract(TickSimulator(**config()))["feed_latency_model"]["status"] == "not_calibrated"


def test_signed_volume_does_not_turn_unsigned_trade_into_verified_direction():
    normalized = normalize_tick(
        source="test", session_id="s", seq=1, received_ns=0,
        received_at_utc="2026-09-22T00:00:00+00:00", code="005930", venue="unknown",
        real_type="주식체결", fids={"10": "10001", "15": " 2", "20": "090000"},
        price_policy="positive_only", direction_policy="signed_volume")
    assert normalized.event.is_buy is None
    assert "trade_direction_unverified" in normalized.issues
    caps = input_capabilities(TickSimulator(**config()))
    assert caps["upstream_format_support"]["kiwoom_fids_prototype_1"]["signed_volume_rule"] == "explicit_FID15_plus_or_minus_only"
    assert caps["trade_direction_policy"]["actual_normalization_policy"] == "not_verified_by_contract"


def test_saved_contract_hash_and_reproducibility_identity_cover_both_contracts(tmp_path):
    saved = load(run(tmp_path))
    contracts = {name: saved[name] for name in ("simulation_contract", "input_capabilities")}
    digest = hashlib.sha256(canonical(contracts).encode("utf-8")).hexdigest()
    assert saved["contract_sha256"] == digest
    identity = dict(events=saved["event_sha256"], settings=saved["settings"],
                    code=saved["code_sha256"], provenance=saved["input_provenance"], contracts=digest)
    assert saved["reproducibility_key"] == hashlib.sha256(canonical(identity).encode("utf-8")).hexdigest()
    source = Path(__file__).resolve().parents[1] / "execution/reality_contract.py"
    assert saved["code_sha256"]["execution/reality_contract.py"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert saved["schema"] == "tick_research_result_v1" and len(saved["code_sha256"]) == 7
    # Golden economic trace: the contract does not change the existing fill rule.
    assert saved["final_cash"] == "79977.998" and saved["open_quantity"] == 2
    assert saved["fills"] == [dict(order_id="nxt-fixed-1", time_ns=6, side="buy",
                                    quantity=2, price="10001", fee="20.002", quote_seq=1)]
    assert saved["order_audit"] == [[1, "nxt-fixed-1", "submitted"], [6, "nxt-fixed-1", "filled"]]


def test_reruns_and_decimal_context_identity(tmp_path):
    with localcontext() as context:
        context.prec = 28
        first = load(run(tmp_path))
        second = load(run(tmp_path))
        assert first["simulation_contract"] == second["simulation_contract"]
        assert first["input_capabilities"] == second["input_capabilities"]
        assert first["contract_sha256"] == second["contract_sha256"]
        assert first["reproducibility_key"] == second["reproducibility_key"]
        context.prec = 40
        changed = load(run(tmp_path))
    assert changed["contract_sha256"] != first["contract_sha256"]
    assert changed["reproducibility_key"] != first["reproducibility_key"]


def test_contract_content_changes_identity_even_with_same_code_hashes(tmp_path, monkeypatch):
    first = load(run(tmp_path))
    original = runner.simulation_contract
    monkeypatch.setattr(runner, "simulation_contract", lambda sim: original(sim) | {"version": 2})
    second = load(run(tmp_path))
    assert first["code_sha256"] == second["code_sha256"]
    assert first["event_sha256"] == second["event_sha256"]
    assert first["fills"] == second["fills"]
    assert first["contract_sha256"] != second["contract_sha256"]
    assert first["reproducibility_key"] != second["reproducibility_key"]


@pytest.mark.parametrize("failed", [False, True])
def test_running_and_final_records_retain_same_contracts(tmp_path, monkeypatch, failed):
    written = []
    original = runner._write
    def record(path, value):
        written.append(json.loads(runner._json(value)))
        original(path, value)
    monkeypatch.setattr(runner, "_write", record)
    stream = events()
    if failed:
        stream.append(replace(stream[-1], seq=3, received_ns=7, session_id="wrong"))
        with pytest.raises(runner.ResearchRunFailed) as caught:
            run(tmp_path, stream)
        path = caught.value.path
    else:
        path = run(tmp_path, stream)
    assert len(written) == 2 and written[0]["status"] == "running"
    for name in ("simulation_contract", "input_capabilities", "contract_sha256"):
        assert written[0][name] == written[1][name] == load(path)[name]
    assert written[1]["status"] == ("failed" if failed else "completed_with_open_position")
    assert inspect(path)["diagnostics_only"] is failed


def test_empty_input_and_caller_claims_do_not_upgrade_capabilities(tmp_path):
    saved = load(run(tmp_path, [], input_provenance={"venue_verified": True, "mbo": True}))
    assert saved["status"] == "completed_empty_input"
    assert saved["input_capabilities"]["market_by_order"]["supported"] is False
    assert saved["input_capabilities"]["venue"]["verification_status"] == "not_verified"
    assert saved["input_capabilities"]["dataset_evidence"]["capture_completeness"] == "not_verified"
    assert saved["raw_identity_verified"] is False


def test_raw_v2_keeps_same_execution_boundary_without_timestamp_or_depth_upgrade(tmp_path):
    database = tmp_path / "synthetic.db"
    with RawV2Writer(database, source="test", session_id="s", market_date="2026-09-22",
                     feed_scope="synthetic") as writer:
        for event in events():
            writer.append(event, received_at_utc="2026-09-22T00:00:02+00:00",
                          raw_fields={"synthetic": True}, exchange_ts_raw=None,
                          source_time_precision="unknown")
        writer.finish(close_ns=20)
    saved = load(runner.run_raw_v2(database, output_root=tmp_path / "raw_runs",
                                  simulator_config=config(), quantity=2))
    memory = load(run(tmp_path / "memory_runs"))
    assert saved["simulation_contract"] == memory["simulation_contract"]
    assert saved["input_capabilities"] == memory["input_capabilities"]
    assert saved["fills"] == memory["fills"]
    assert saved["input_provenance"]["reader"] == "raw_v2_reader_2"
    assert saved["input_complete"] is True
    assert saved["input_capabilities"]["exchange_timestamp"]["in_execution_input"] is False
    assert saved["input_capabilities"]["exchange_timestamp"]["precision_verified"] is False
    assert saved["input_capabilities"]["dataset_evidence"]["capture_completeness"] == "not_verified"


def test_existing_result_reader_accepts_old_and_additive_new_results(tmp_path):
    new_path = run(tmp_path)
    old_style = load(new_path)
    for name in ("simulation_contract", "input_capabilities", "contract_sha256"):
        old_style.pop(name)
    old_path = tmp_path / "old-style-result.json"
    old_path.write_text(canonical(old_style), encoding="utf-8")
    assert inspect(old_path) == inspect(new_path)


@pytest.mark.parametrize("option", ["queue_position_model", "market_impact_model",
                                    "feed_latency_ns", "order_response_latency_ns",
                                    "quote_depth_used_by_execution"])
def test_unsupported_model_configuration_fails_before_output_or_consumption(tmp_path, option):
    class NeverRead:
        def __iter__(self):
            raise AssertionError("invalid configuration consumed input")
    root = tmp_path / "not-created"
    with pytest.raises(TypeError, match=option):
        run(root, NeverRead(), simulator_config=config(**{option: "unsupported"}))
    assert not root.exists()
