from __future__ import annotations

import pytest

from collector.kiwoom.fid_read_ab_analysis import (
    ANALYSIS_SCHEMA,
    RESULT_LIMITED,
    RESULT_READY,
)
from collector.kiwoom.fid_read_ab_assessment import (
    OVERALL_HIGHER,
    OVERALL_LOWER,
    OVERALL_MIXED,
    OVERALL_NOT_ASSESSABLE,
    PATTERN_HIGHER,
    PATTERN_INSUFFICIENT,
    PATTERN_LOWER,
    PATTERN_MIXED,
    SECONDARY_CONCORDANT,
    SECONDARY_NOT_CONCORDANT,
    TRADE,
    QUOTE,
    assess_fid_read_ab,
)
from collector.kiwoom.fid_read_ab_diagnostic import (
    PHASE_A1,
    PHASE_A2,
    PHASE_B,
    PHASE_POST,
    PHASE_PRE,
)

PHASES = (PHASE_PRE, PHASE_A1, PHASE_B, PHASE_A2, PHASE_POST)
REAL_TYPES = (TRADE, QUOTE)


def metric(count, median):
    return {"count": count, "min": median, "median": median, "max": median}


def analysis_fixture(
    *,
    trade_fid=(100, 60, 110),
    quote_fid=(200, 100, 220),
    trade_processing=(150, 90, 160),
    quote_processing=(300, 170, 330),
    trade_queue=(50, 50, 50),
    quote_queue=(100, 100, 100),
    count=3,
    result=RESULT_READY,
):
    phase_values = {
        PHASE_A1: 0,
        PHASE_B: 1,
        PHASE_A2: 2,
    }
    by_phase_type = {}
    for phase in PHASES:
        by_phase_type[phase] = {}
        for real_type in REAL_TYPES:
            if phase in phase_values:
                idx = phase_values[phase]
                fid = trade_fid[idx] if real_type == TRADE else quote_fid[idx]
                processing = trade_processing[idx] if real_type == TRADE else quote_processing[idx]
                queue = trade_queue[idx] if real_type == TRADE else quote_queue[idx]
                phase_count = count
            else:
                fid = 1
                processing = 1
                queue = 1
                phase_count = 999  # PRE/POST must be ignored by the assessment.
            by_phase_type[phase][real_type] = {
                "samples": phase_count,
                "sampled_code_count": 1,
                "fid_read_ns": metric(phase_count, fid),
                "processing_ns": metric(phase_count, processing),
                "queue_submit_ns": metric(phase_count, queue),
                "fid_call_count": metric(phase_count, 3 if phase == PHASE_B else 6),
                "clock_difference_seconds": metric(phase_count, -999 if phase == PHASE_B else 999),
            }
    return {
        "schema": ANALYSIS_SCHEMA,
        "result": result,
        "issues": [] if result == RESULT_READY else [{"severity": "limited", "id": "fixture"}],
        "telemetry": {
            "present": True,
            "sample_count": 18,
            "by_phase": {},
            "by_phase_real_type": by_phase_type,
        },
    }


def test_primary_b_lower_for_both_real_types_is_descriptive_lower_pattern():
    assessment = assess_fid_read_ab(analysis_fixture())
    assert assessment["assessment"] == OVERALL_LOWER
    assert assessment["primary_by_real_type"][TRADE]["pattern"] == PATTERN_LOWER
    assert assessment["primary_by_real_type"][QUOTE]["pattern"] == PATTERN_LOWER
    assert assessment["secondary_relation"] == SECONDARY_CONCORDANT
    assert assessment["primary_by_real_type"][TRADE]["b_over_a1_median"] == pytest.approx(0.6)
    assert assessment["interpretation_contract"]["causal_verdict"] is False


def test_primary_b_higher_for_both_real_types_is_contrary_ordering_not_causal_verdict():
    assessment = assess_fid_read_ab(analysis_fixture(
        trade_fid=(100, 140, 110),
        quote_fid=(200, 260, 220),
        trade_processing=(150, 190, 160),
        quote_processing=(300, 380, 330),
    ))
    assert assessment["assessment"] == OVERALL_HIGHER
    assert all(
        assessment["primary_by_real_type"][kind]["pattern"] == PATTERN_HIGHER
        for kind in REAL_TYPES
    )
    assert assessment["secondary_relation"] == SECONDARY_CONCORDANT
    assert "proof" in assessment["notes"][1]


def test_mixed_trade_quote_primary_directions_stay_mixed():
    assessment = assess_fid_read_ab(analysis_fixture(
        trade_fid=(100, 60, 110),
        quote_fid=(200, 260, 220),
    ))
    assert assessment["assessment"] == OVERALL_MIXED
    assert assessment["primary_by_real_type"][TRADE]["pattern"] == PATTERN_LOWER
    assert assessment["primary_by_real_type"][QUOTE]["pattern"] == PATTERN_HIGHER


def test_b_between_or_tied_is_mixed_not_promoted():
    assessment = assess_fid_read_ab(analysis_fixture(
        trade_fid=(100, 100, 110),
        quote_fid=(200, 210, 220),
    ))
    assert assessment["assessment"] == OVERALL_MIXED
    assert assessment["primary_by_real_type"][TRADE]["pattern"] == PATTERN_MIXED
    assert assessment["primary_by_real_type"][QUOTE]["pattern"] == PATTERN_MIXED


def test_minimum_three_metric_samples_per_phase_is_fixed_coverage_guardrail():
    report = analysis_fixture()
    report["telemetry"]["by_phase_real_type"][PHASE_B][TRADE]["fid_read_ns"]["count"] = 2
    assessment = assess_fid_read_ab(report)
    assert assessment["assessment"] == OVERALL_NOT_ASSESSABLE
    assert assessment["primary_by_real_type"][TRADE]["pattern"] == PATTERN_INSUFFICIENT
    assert assessment["minimum_samples_per_phase"] == 3
    assert assessment["interpretation_contract"]["effect_size_threshold"] is None


def test_non_ready_bounded_analysis_blocks_aba_assessment():
    assessment = assess_fid_read_ab(analysis_fixture(result=RESULT_LIMITED))
    assert assessment["assessment"] == OVERALL_NOT_ASSESSABLE
    assert assessment["blocked_by_analysis_result"] is True
    assert assessment["primary_by_real_type"] == {}


def test_secondary_processing_disagreement_cannot_override_primary_fid_read_label():
    assessment = assess_fid_read_ab(analysis_fixture(
        trade_fid=(100, 60, 110),
        quote_fid=(200, 100, 220),
        trade_processing=(150, 190, 160),
        quote_processing=(300, 380, 330),
    ))
    assert assessment["assessment"] == OVERALL_LOWER
    assert assessment["secondary_relation"] == SECONDARY_NOT_CONCORDANT
    assert assessment["secondary_by_real_type"][TRADE]["pattern"] == PATTERN_HIGHER


def test_pre_post_and_clock_difference_are_excluded_from_automatic_assessment():
    report = analysis_fixture()
    before = assess_fid_read_ab(report)
    for phase in (PHASE_PRE, PHASE_POST):
        for kind in REAL_TYPES:
            typed = report["telemetry"]["by_phase_real_type"][phase][kind]
            typed["fid_read_ns"] = metric(9999, 10**30)
            typed["processing_ns"] = metric(9999, 10**30)
            typed["clock_difference_seconds"] = metric(9999, -10**30)
    after = assess_fid_read_ab(report)
    assert before["assessment"] == after["assessment"] == OVERALL_LOWER
    assert "clock_difference_seconds" in after["excluded_from_automatic_assessment"]
    assert "POST_90S_FULL" in after["excluded_from_automatic_assessment"]


def test_queue_submit_is_guardrail_only():
    assessment = assess_fid_read_ab(analysis_fixture(
        trade_queue=(100, 1000, 110),
        quote_queue=(200, 2000, 220),
    ))
    assert assessment["assessment"] == OVERALL_LOWER
    assert assessment["guardrail_by_real_type"][TRADE]["pattern"] == PATTERN_HIGHER
    assert assessment["guardrail_by_real_type"][QUOTE]["pattern"] == PATTERN_HIGHER


def test_wrong_analysis_schema_is_rejected():
    report = analysis_fixture()
    report["schema"] = "other"
    with pytest.raises(ValueError, match="fid_read_ab_analysis_v1"):
        assess_fid_read_ab(report)
