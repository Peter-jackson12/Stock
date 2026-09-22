"""자원 재시도 계약의 추가 설명과 기존 v1 호환성 검증."""
from execution.reality_contract import simulation_contract, input_capabilities
from execution.tick_simulator import TickSimulator
from tests.execution_audit_support import Pair, config, quote
from tests.test_documentation import ROOT, link_errors


def test_resource_clarification_preserves_v1_field_types():
    contract = simulation_contract(TickSimulator(**config()))
    assert (contract["schema"], contract["version"]) == ("simulation_reality_v1", 1)
    assert contract["resource_policy"] == "no_reservation_retry_when_cash_or_position_becomes_available"
    retry = contract["resource_retry_policy"]
    assert retry["reservation"] == "none"
    assert retry["allocation_round"] == "one_submission_order_pass"
    assert retry["later_orders_observe_earlier_fills"] is True
    assert retry["earlier_blocked_order"] == "retry_on_next_matching_round_not_revisited_in_same_round"
    assert retry["fixed_point_iteration"] is False
    assert contract["deterministic_replay"]["advance_subdivision_invariant"] is False
    assert contract["same_timestamp_policy"]["matching_rounds"]["deadline_endpoint_tie"] == "two_separate_rounds"


def test_numeric_limit_is_not_disguised_as_a_corrected_solvency_policy():
    contract = simulation_contract(TickSimulator(**config()))
    assert contract["numeric_safety"]["post_debit_solvency_guard"] is False
    assert contract["numeric_safety"]["stable_context_alone_guarantees_solvency"] is False
    assert contract["numeric_safety"]["known_issue"].startswith("NUM-1_")
    assert contract["scope"] == "single_instrument_single_venue_long_only_market_orders"
    assert input_capabilities(TickSimulator(**config()))["dataset_evidence"]["capture_completeness"] == "not_verified"


def test_new_resource_description_does_not_share_mutable_lists():
    sim = TickSimulator(**config())
    contract = simulation_contract(sim)
    contract["resource_retry_policy"]["retry_still_requires"].clear()
    assert simulation_contract(sim)["resource_retry_policy"]["retry_still_requires"] == [
        "ready", "not_cancelled", "valid_fresh_quote", "remaining_side_budget"]


def test_deadline_reserved_at_call_start_remains_after_order_fills():
    pair = Pair(config(cash=3, buy_latency_ns=1, cancel_latency_ns=2, max_quote_age_ns=2))
    pair.apply("event", quote()).apply("submit", "S", "sell", 1)
    pair.apply("submit", "B", "buy", 1).apply("cancel", "B").apply("advance", 3)
    # B는 1에 완전히 체결되지만 이 advance 호출에서 예약했던 2의 라운드는 남는다.
    assert [(fill.side, fill.time_ns) for fill in pair.sim.fills] == [("buy", 1), ("sell", 2)]
    assert pair.sim.cash == 2 and pair.sim.position == 0
    assert pair.sim.orders["B"].status == "filled"


def test_immediate_cancel_preserves_prior_same_ns_partial_fill_only():
    pair = Pair(config(cash=10))
    pair.apply("event", quote(ask_size=1)).apply("submit", "B", "buy", 2)
    pair.apply("cancel", "B").apply("event", quote(2, 0, ask_size=2))
    assert [(fill.quantity, fill.time_ns) for fill in pair.sim.fills] == [(1, 0)]
    assert pair.sim.orders["B"].status == "cancelled" and pair.sim.orders["B"].remaining == 1
    assert pair.sim.cash == 7 and pair.sim.position == 1


def test_oracle_spec_navigation_is_valid():
    assert link_errors(ROOT, "tests/EXECUTION_ORACLE_SPEC.md") == []
