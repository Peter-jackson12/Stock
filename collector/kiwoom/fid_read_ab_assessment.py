"""Pre-registered descriptive A-B-A interpretation for FID read telemetry.

This module consumes the bounded analyzer output. It does not read raw data,
does not estimate causal effects, and does not infer a Qt/COM/GIL/native cause.
"""
from __future__ import annotations

import math

from collector.kiwoom.fid_read_ab_analysis import ANALYSIS_SCHEMA, RESULT_READY
from collector.kiwoom.fid_read_ab_diagnostic import PHASE_A1, PHASE_A2, PHASE_B

ASSESSMENT_SCHEMA = "fid_read_ab_assessment_v1"
MIN_PHASE_METRIC_SAMPLES = 3

TRADE = "주식체결"
QUOTE = "주식호가잔량"
REAL_TYPES = (TRADE, QUOTE)

PRIMARY_METRIC = "fid_read_ns"
SECONDARY_METRIC = "processing_ns"
GUARDRAIL_METRIC = "queue_submit_ns"

PATTERN_LOWER = "B_LOWER_THAN_BOTH_FULL"
PATTERN_HIGHER = "B_HIGHER_THAN_BOTH_FULL"
PATTERN_MIXED = "B_BETWEEN_OR_TIED"
PATTERN_INSUFFICIENT = "INSUFFICIENT_PHASE_METRIC_SAMPLES"
PATTERN_INVALID = "INVALID_PHASE_METRIC_SUMMARY"

OVERALL_LOWER = "PRIMARY_B_LOWER_BOTH_REAL_TYPES"
OVERALL_HIGHER = "PRIMARY_B_HIGHER_BOTH_REAL_TYPES"
OVERALL_MIXED = "PRIMARY_MIXED_ACROSS_REAL_TYPES"
OVERALL_NOT_ASSESSABLE = "PRIMARY_NOT_ASSESSABLE"

SECONDARY_CONCORDANT = "SECONDARY_CONCORDANT_WITH_PRIMARY"
SECONDARY_NOT_CONCORDANT = "SECONDARY_NOT_CONCORDANT_WITH_PRIMARY"
SECONDARY_NOT_ASSESSABLE = "SECONDARY_NOT_ASSESSABLE"


def _safe_ratio(numerator, denominator):
    if isinstance(numerator, bool) or isinstance(denominator, bool):
        return None
    if not isinstance(numerator, (int, float)) or not isinstance(denominator, (int, float)):
        return None
    if denominator <= 0:
        return None
    try:
        return numerator / denominator
    except OverflowError:  # huge integer medians are descriptive only; no ratio.
        return None


def _duration(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return (not isinstance(value, float) or math.isfinite(value)) and value >= 0


def _metric_phase(telemetry: dict, real_type: str, phase: str, metric: str) -> dict:
    """Read one assessed summary. Malformed summaries are errors, never zero-count or directional."""
    by_phase_type = telemetry.get("by_phase_real_type")
    phase_value = by_phase_type.get(phase) if isinstance(by_phase_type, dict) else None
    typed = phase_value.get(real_type) if isinstance(phase_value, dict) else None
    value = typed.get(metric) if isinstance(typed, dict) else None
    if not isinstance(value, dict):
        return {"count": None, "median": None, "valid": False, "error": "summary_missing_or_not_object"}
    count = value.get("count")
    median = value.get("median")
    low, high = value.get("min"), value.get("max")
    if type(count) is not int or count < 0:
        return {"count": None, "median": None, "valid": False, "error": "count_not_a_nonnegative_integer"}
    if count == 0:
        if median is not None or low is not None or high is not None:
            return {"count": count, "median": None, "valid": False, "error": "empty_summary_has_statistics"}
        return {"count": 0, "median": None, "valid": True}
    if not (_duration(median) and _duration(low) and _duration(high) and low <= median <= high):
        return {"count": count, "median": None, "valid": False,
                "error": "statistics_must_be_finite_nonnegative_and_ordered"}
    return {"count": count, "median": median, "valid": True}


def _classify_metric(telemetry: dict, real_type: str, metric: str) -> dict:
    phases = {
        phase: _metric_phase(telemetry, real_type, phase, metric)
        for phase in (PHASE_A1, PHASE_B, PHASE_A2)
    }
    if not all(phases[phase]["valid"] for phase in phases):
        pattern = PATTERN_INVALID
    elif not all(
        phases[phase]["count"] >= MIN_PHASE_METRIC_SAMPLES and phases[phase]["median"] is not None
        for phase in phases
    ):
        pattern = PATTERN_INSUFFICIENT
    else:
        a1 = phases[PHASE_A1]["median"]
        b = phases[PHASE_B]["median"]
        a2 = phases[PHASE_A2]["median"]
        if b < min(a1, a2):
            pattern = PATTERN_LOWER
        elif b > max(a1, a2):
            pattern = PATTERN_HIGHER
        else:
            pattern = PATTERN_MIXED

    a1 = phases[PHASE_A1]["median"]
    b = phases[PHASE_B]["median"]
    a2 = phases[PHASE_A2]["median"]
    return {
        "metric": metric,
        "real_type": real_type,
        "minimum_samples_per_phase": MIN_PHASE_METRIC_SAMPLES,
        "phases": phases,
        "pattern": pattern,
        "b_over_a1_median": _safe_ratio(b, a1),
        "b_over_a2_median": _safe_ratio(b, a2),
        "a2_over_a1_median": _safe_ratio(a2, a1),
        "threshold_note": (
            "No effect-size threshold is used. Ordering is descriptive only; "
            "magnitude and statistical sufficiency are not certified."
        ),
    }


def _overall_primary(primary_by_type: dict) -> str:
    patterns = [primary_by_type[real_type]["pattern"] for real_type in REAL_TYPES]
    if PATTERN_INSUFFICIENT in patterns or PATTERN_INVALID in patterns:
        return OVERALL_NOT_ASSESSABLE
    if all(pattern == PATTERN_LOWER for pattern in patterns):
        return OVERALL_LOWER
    if all(pattern == PATTERN_HIGHER for pattern in patterns):
        return OVERALL_HIGHER
    return OVERALL_MIXED


def _secondary_relation(primary: str, secondary_by_type: dict) -> str:
    secondary = [secondary_by_type[real_type]["pattern"] for real_type in REAL_TYPES]
    if PATTERN_INSUFFICIENT in secondary or PATTERN_INVALID in secondary:
        return SECONDARY_NOT_ASSESSABLE
    if primary == OVERALL_LOWER and all(pattern == PATTERN_LOWER for pattern in secondary):
        return SECONDARY_CONCORDANT
    if primary == OVERALL_HIGHER and all(pattern == PATTERN_HIGHER for pattern in secondary):
        return SECONDARY_CONCORDANT
    return SECONDARY_NOT_CONCORDANT


def assess_fid_read_ab(analysis: dict) -> dict:
    """Apply the fixed pre-registered descriptive rule to analyzer output."""
    if not isinstance(analysis, dict):
        raise ValueError("analysis object required")
    if analysis.get("schema") != ANALYSIS_SCHEMA:
        raise ValueError("fid_read_ab_analysis_v1 required")

    analysis_result = analysis.get("result")
    telemetry = analysis.get("telemetry")
    if not isinstance(telemetry, dict):
        telemetry = {}

    # READY is re-checked against its own issue list; a label alone is not trusted.
    if analysis_result != RESULT_READY or analysis.get("issues") != []:
        return {
            "schema": ASSESSMENT_SCHEMA,
            "assessment": OVERALL_NOT_ASSESSABLE,
            "analysis_result": analysis_result,
            "blocked_by_analysis_result": True,
            "primary_metric": PRIMARY_METRIC,
            "minimum_samples_per_phase": MIN_PHASE_METRIC_SAMPLES,
            "sample_threshold_role": "coverage_guardrail_not_statistical_power",
            "primary_by_real_type": {},
            "secondary_by_real_type": {},
            "guardrail_by_real_type": {},
            "secondary_relation": SECONDARY_NOT_ASSESSABLE,
            "excluded_from_automatic_assessment": [
                "clock_difference_seconds",
                "callback_count_per_30_seconds",
                "resource_history",
                "PRE_SUBSCRIPTION_FULL",
                "POST_90S_FULL",
            ],
            "notes": [
                "assessment is descriptive and not a causal verdict",
                "analyzer must be CAPTURE_COMPLETE_ANALYSIS_READY before A-B-A ordering is assessed",
            ],
        }

    primary_by_type = {
        real_type: _classify_metric(telemetry, real_type, PRIMARY_METRIC)
        for real_type in REAL_TYPES
    }
    secondary_by_type = {
        real_type: _classify_metric(telemetry, real_type, SECONDARY_METRIC)
        for real_type in REAL_TYPES
    }
    guardrail_by_type = {
        real_type: _classify_metric(telemetry, real_type, GUARDRAIL_METRIC)
        for real_type in REAL_TYPES
    }
    primary = _overall_primary(primary_by_type)
    metric_errors = [
        {"metric": item["metric"], "real_type": item["real_type"], "phase": phase, "error": value["error"]}
        for group in (primary_by_type, secondary_by_type, guardrail_by_type)
        for item in group.values()
        for phase, value in item["phases"].items()
        if not value["valid"]
    ]

    return {
        "schema": ASSESSMENT_SCHEMA,
        "assessment": primary,
        "analysis_result": analysis_result,
        "blocked_by_analysis_result": False,
        "primary_metric": PRIMARY_METRIC,
        "minimum_samples_per_phase": MIN_PHASE_METRIC_SAMPLES,
        "sample_threshold_role": "coverage_guardrail_not_statistical_power",
        "primary_by_real_type": primary_by_type,
        "secondary_metric": SECONDARY_METRIC,
        "secondary_by_real_type": secondary_by_type,
        "secondary_relation": _secondary_relation(primary, secondary_by_type),
        "guardrail_metric": GUARDRAIL_METRIC,
        "guardrail_by_real_type": guardrail_by_type,
        "metric_evidence_errors": metric_errors,
        "excluded_from_automatic_assessment": [
            "clock_difference_seconds",
            "callback_count_per_30_seconds",
            "resource_history",
            "PRE_SUBSCRIPTION_FULL",
            "POST_90S_FULL",
        ],
        "interpretation_contract": {
            "trade_and_quote_are_never_pooled": True,
            "primary_rule": "B median is compared with both A1 and A2 medians separately for each real_type",
            "effect_size_threshold": None,
            "p_value_or_significance_test": None,
            "post_hoc_threshold_tuning": False,
            "pure_com_cost_experiment": False,
            "backlog_reset_between_phases": False,
            "causal_verdict": False,
        },
        "notes": [
            "B_LOWER_THAN_BOTH_FULL is compatible with lower sampled FID-read-path time during ESSENTIAL, not proof of cause",
            "B_HIGHER_THAN_BOTH_FULL is a contrary descriptive ordering, not proof that fewer reads are harmful",
            "B_BETWEEN_OR_TIED is mixed/tied descriptive evidence",
            "processing_ns is secondary corroboration only and cannot override the primary fid_read_ns label",
            "queue_submit_ns is a guardrail description only",
            "clock difference is excluded from automatic assessment and is not network latency",
            "callback counts divided by 30 are excluded from automatic assessment and are not certified service rates",
            "PRE and POST are excluded from A-B-A comparison",
        ],
    }
