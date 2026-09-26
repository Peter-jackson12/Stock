from dataclasses import replace

from research.fast_backtest.exact import run_exact_bridge
from research.fast_backtest.sweep import FastSweepResult, deduplicate_candidates


def result(identity, candidate_id, pnl):
    return FastSweepResult(
        candidate_id=candidate_id,
        parameter_identity=identity,
        aliases=(candidate_id,),
        screening_only=True,
        screening_status="COMPLETE_FLAT",
        signals=0,
        entry_signals=0,
        exit_signals=0,
        fills=0,
        trades=0,
        gross_pnl=0,
        fees=0,
        net_pnl=pnl,
        max_adverse_pct=None,
        terminal_position=0,
        entry_decision_ns=(),
        exit_decision_ns=(),
        fill_records=(),
    )


def exact(total=0, signals=(), fills=(), **accounting_changes):
    accounting = {
        "schema": "portfolio_performance_accounting_v1",
        "status": "flat_complete",
        "total_pnl": {"numerator": int(total), "denominator": 1},
        "cash_reconciled": True,
        "position_reconciled": True,
        "accounting_identity_reconciled": True,
    } | accounting_changes
    return {
        "status": "completed_flat",
        "input_complete": True,
        "diagnostics_only": False,
        "error": None,
        "strategy_signals": list(signals),
        "account": {"fills": list(fills)},
        "performance_accounting": accounting,
    }


def test_exact_bridge_calls_only_top_n_and_marks_screening_only():
    candidates = deduplicate_candidates([
        {"candidate_id": "one", "params": {"spread_max_pct": 0.1}},
        {"candidate_id": "two", "params": {"spread_max_pct": 0.2}},
        {"candidate_id": "three", "params": {"spread_max_pct": 0.3}},
    ])
    results = [result(candidate.parameter_identity, candidate.candidate_id, 3 - index)
               for index, candidate in enumerate(candidates)]
    called = []

    def runner(candidate):
        called.append(candidate.candidate_id)
        return exact(total=next(item.net_pnl for item in results if item.parameter_identity == candidate.parameter_identity))

    records = run_exact_bridge(results, candidates, top_n=2, exact_runner=runner)
    assert called == [results[0].candidate_id, results[1].candidate_id]
    assert len(records) == 2
    assert all(record.screening_only for record in records)
    assert all(record.parity_status == "PASS" for record in records)
    assert all(record.accounting_status == "PASS" for record in records)
    assert all(record.exact_acceptance_status == "PASS" for record in records)


def test_mismatch_is_preserved_instead_of_overwriting_fast_result():
    candidate = deduplicate_candidates([{"candidate_id": "one", "params": {"spread_max_pct": 0.1}}])[0]
    fast = result(candidate.parameter_identity, candidate.candidate_id, 5)
    records = run_exact_bridge([fast], [candidate], top_n=1, exact_runner=lambda _: exact(total=4))
    record = records[0]
    assert record.parity_status == "FAST_EXACT_MISMATCH"
    assert record.mismatches == ("net_pnl",)
    assert record.fast_result["net_pnl"] == 5
    assert record.exact_result["performance_accounting"]["total_pnl"]["numerator"] == 4
    assert record.accounting_status == "PASS"
    assert record.exact_acceptance_status == "REJECTED"


def test_exact_failure_is_recorded_without_fabricating_result():
    candidate = deduplicate_candidates([{"candidate_id": "one", "params": {"spread_max_pct": 0.1}}])[0]
    fast = result(candidate.parameter_identity, candidate.candidate_id, 0)

    def fail(_):
        raise RuntimeError("fixture")

    record = run_exact_bridge([fast], [candidate], top_n=1, exact_runner=fail)[0]
    assert record.parity_status == "EXACT_REPLAY_FAILED"
    assert record.exact_result is None
    assert record.exact_error == "RuntimeError: fixture"


def test_accounting_reconciliation_is_separate_from_fast_parity():
    candidate = deduplicate_candidates([{"candidate_id": "one", "params": {"spread_max_pct": 0.1}}])[0]
    fast = result(candidate.parameter_identity, candidate.candidate_id, 5)
    record = run_exact_bridge(
        [fast],
        [candidate],
        top_n=1,
        exact_runner=lambda _: exact(total=5, cash_reconciled=False),
    )[0]

    assert record.parity_status == "PASS"
    assert record.mismatches == ()
    assert record.accounting_status == "ACCOUNTING_REJECTED"
    assert record.accounting_issues == ("cash_reconciled",)
    assert record.exact_acceptance_status == "REJECTED"


def test_unpriced_open_accounting_cannot_be_authoritative_acceptance():
    candidate = deduplicate_candidates([{"candidate_id": "one", "params": {"spread_max_pct": 0.1}}])[0]
    fast = result(candidate.parameter_identity, candidate.candidate_id, None)
    payload = exact(
        total=0,
        status="open_unpriced",
        accounting_identity_reconciled=None,
        total_pnl=None,
    )
    payload["status"] = "completed_with_open_position"
    record = run_exact_bridge([fast], [candidate], top_n=1, exact_runner=lambda _: payload)[0]

    assert record.accounting_status == "ACCOUNTING_REJECTED"
    assert "accounting_status" in record.accounting_issues
    assert "accounting_identity_reconciled" in record.accounting_issues
    assert "total_pnl" in record.accounting_issues
    assert record.exact_acceptance_status == "REJECTED"
