"""현재 틱 연구의 실행 가정과 입력 형식 능력을 기술한다.

모델 선택기나 데이터 검사기가 아니다. 검증된 simulator의 값만 읽으며,
지원하는 필드와 실제 관측/원천 인증을 구분한다. 실행 상태를 변경하지 않는다.
"""
from decimal import getcontext
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from execution.tick_simulator import TickSimulator


def simulation_contract(sim: "TickSimulator") -> dict:
    """Fresh, JSON-native description of the existing fixed execution model."""
    context = getcontext()
    return {
        "schema": "simulation_reality_v1",
        "version": 1,
        "scope": "single_instrument_single_venue_long_only_market_orders",
        "market_data_granularity": "receive_order_trade_ticks_and_quote_snapshots",
        "quote_depth_used_by_execution": 1,
        "fill_model": "validated_best_ask_buy_best_bid_sell",
        "fill_quantity_policy": "min_remaining_displayed_side_liquidity_cash_or_position",
        "quote_validation_policy": "positive_two_sided_top_v1",
        "liquidity_model": "per_side_displayed_top_of_book_budget",
        "liquidity_replenishment_policy": {
            "quote": "every_new_quote_sequence_resets_both_budgets_even_if_values_unchanged",
            "trade_or_timer": "no_replenishment",
            "external_trade_consumption": "not_modeled",
        },
        "partial_fill_policy": "retain_remainder_until_filled_cancelled_or_expired",
        "resource_policy": "no_reservation_retry_when_cash_or_position_becomes_available",
        "queue_position_model": "none",
        "market_impact_model": "not_modeled",
        "feed_latency_model": {
            "status": "not_calibrated",
            "replay_clock": "local_monotonic_received_ns",
            "reason": "no_synchronized_exchange_local_clock_evidence",
        },
        "order_entry_latency_model": {
            "model": "constant_by_side",
            "buy_ns": sim.latency["buy"],
            "sell_ns": sim.latency["sell"],
            "calibration": "caller_assumption_not_verified",
        },
        "order_response_latency_model": {
            "status": "not_modeled",
            "account_and_fill_update": "during_match",
            "strategy_fill_observation": "poll_at_start_of_next_market_event_callback",
            "timer_fill_callback": False,
        },
        "cancel_latency_model": {
            "model": "constant",
            "value_ns": sim.cancel_latency,
            "calibration": "caller_assumption_not_verified",
            "fills_before_effective_cancel": "allowed",
        },
        "fee_model": {
            "model": "gross_notional_rate_per_fill_both_sides",
            "rate": str(sim.fee_rate),
            "research_requires_explicit_rate": True,
            "separate_tax_or_broker_schedule": "not_modeled",
            "currency_quantization": "none_beyond_decimal_context",
        },
        "slippage_model": {
            "status": "not_modeled_separately",
            "price_effects_already_present": ["bid_ask_spread", "quote_at_order_ready_time"],
        },
        "stale_quote_policy": {
            "max_age_ns": sim.replay.max_quote_age_ns,
            "eligible_age": "age_ns_lte_max_age_ns",
            "missing_invalid_stale": "withhold_fill_no_trade_price_fallback",
        },
        "same_timestamp_policy": {
            "id": "timers_old_quote_then_event_then_strategy_v1",
            "phases": [
                "validate_external_identity_sequence_and_receipt_time",
                "process_deadlines_lte_event_time_using_previous_quote",
                "apply_external_event_and_quote_budget_refresh",
                "match_existing_ready_orders",
                "invoke_market_event_strategy",
                "match_zero_latency_submissions_after_their_submission",
            ],
            "cancel_fill_tie": "effective_cancellations_before_any_fills",
            "order_priority": "submission_insertion_order_not_exchange_queue_position",
            "external_event_tie": "supplied_increasing_sequence_not_timestamp_batching",
            "cancel_requested_in_callback": "cannot_undo_prior_matching",
        },
        "close_boundary_policy": {
            "id": "exclusive_close_v1",
            "deadline_processing": "advance_through_close_ns_minus_one",
            "at_or_after_close": "no_fills_expire_remaining_orders",
            "holdings": "retain_without_forced_liquidation_or_valuation",
            "post_last_tick": "process_pre_close_deadlines_subject_to_quote_age",
            "strategy_on_close_or_timer": False,
        },
        "deterministic_replay": {
            "event_order": "single_source_session_increasing_seq_nondecreasing_received_ns",
            "chunking": "preserve_simulator_and_strategy_state_across_chunks",
            "randomness": "none",
            "numeric_context_requirement": "caller_keeps_decimal_context_stable_during_run",
        },
        "decimal_context": {
            "prec": context.prec,
            "rounding": context.rounding,
            "Emin": context.Emin,
            "Emax": context.Emax,
            "capitals": context.capitals,
            "clamp": context.clamp,
            "traps": {signal.__name__: enabled for signal, enabled in
                      sorted(context.traps.items(), key=lambda item: item[0].__name__)},
        },
    }


def input_capabilities(sim: "TickSimulator") -> dict:
    """Describe schema support, never infer observed coverage from a run label.

    run_raw_v2 also passes only OrderedTick to execution. Raw envelope support
    below is a format description, not an assertion that this run used raw-v2.
    Actual provenance remains in result.input_provenance; this does not scan it.
    """
    return {
        "schema": "input_capability_v1",
        "version": 1,
        "scope": "format_support_not_observed_dataset_capabilities",
        "execution_input": "OrderedTick",
        "trade_ticks": {"supported": True, "price_volume_direction": "nullable_fields"},
        "quote_snapshots": {
            "supported": True,
            "normalized_price_depth_per_side": 1,
            "quantity_ladders": "optional_tuples_not_full_price_depth",
            "strategy_entry_quantity_depth_required": 3,
            "execution_depth_used": 1,
        },
        "market_by_order": {"supported": False, "exchange_order_ids": False},
        "exchange_timestamp": {
            "in_execution_input": False,
            "precision_verified": False,
            "exchange_local_clock_synchronization": "not_verified",
        },
        "local_receipt_timestamp": {
            "execution_field": "received_ns",
            "representation": "nonnegative_integer_monotonic_nanoseconds",
            "hardware_resolution_or_accuracy": "not_verified",
            "utc_wall_time_in_execution_input": False,
        },
        "trade_direction_policy": {
            "execution_field": "is_buy",
            "research_requirement": "boolean_on_every_processed_trade",
            "actual_normalization_policy": "not_verified_by_contract",
        },
        "market_second": {
            "field": "optional_caller_supplied_wall_second",
            "research_requirement": "integer_0_to_86399_nondecreasing",
            "not_derived_from": "received_ns",
        },
        "venue": {"label": sim.venue, "verification_status": "not_verified"},
        "dataset_evidence": {
            "observed_field_coverage": "not_inspected_by_contract",
            "capture_completeness": "not_verified",
            "source_accuracy": "not_verified",
            "stream_integrity": "see_run_completion_and_reader_evidence_not_this_contract",
        },
        "upstream_format_support": {
            "scope": "reference_formats_not_certification_of_this_run",
            "raw_v2_envelope": {
                "received_at_utc": "required_valid_utc_text",
                "exchange_ts_raw": "optional_original_text",
                "source_time_precision": "required_nonempty_label_not_precision_proof",
                "raw_fields": "opaque_dictionary_no_certified_depth_or_mbo_contract",
                "forwarded_to_execution": "normalized_event_only",
            },
            "kiwoom_fids_prototype_1": {
                "exchange_time_fields": {"trade": "FID20", "quote": "FID21"},
                "source_time_precision": "second_for_valid_HHMMSS_else_unknown",
                "market_second": "KST_wall_second_from_received_at_utc_not_FID20_or_FID21",
                "direction_policies": ["unknown", "signed_volume"],
                "signed_volume_rule": "explicit_FID15_plus_or_minus_only",
                "quote_quantities": "ten_if_valid_else_top_three_if_valid_else_none",
                "raw_price_ladders": "preserved_raw_only_not_executable_depth",
            },
        },
    }
