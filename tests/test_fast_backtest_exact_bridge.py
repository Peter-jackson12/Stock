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


def exact(total=0, signals=(), fills=()):
    return {
        "strategy_signals": list(signals),
        "account": {"fills": list(fills)},
        "performance_accounting": {
            "total_pnl": {"numerator": int(total), "denominator": 1},
        },
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


def test_mismatch_is_preserved_instead_of_overwriting_fast_result():
    candidate = deduplicate_candidates([{"candidate_id": "one", "params": {"spread_max_pct": 0.1}}])[0]
    fast = result(candidate.parameter_identity, candidate.candidate_id, 5)
    records = run_exact_bridge([fast], [candidate], top_n=1, exact_runner=lambda _: exact(total=4))
    record = records[0]
    assert record.parity_status == "FAST_EXACT_MISMATCH"
    assert record.mismatches == ("net_pnl",)
    assert record.fast_result["net_pnl"] == 5
    assert record.exact_result["performance_accounting"]["total_pnl"]["numerator"] == 4


def test_exact_failure_is_recorded_without_fabricating_result():
    candidate = deduplicate_candidates([{"candidate_id": "one", "params": {"spread_max_pct": 0.1}}])[0]
    fast = result(candidate.parameter_identity, candidate.candidate_id, 0)

    def fail(_):
        raise RuntimeError("fixture")

    record = run_exact_bridge([fast], [candidate], top_n=1, exact_runner=fail)[0]
    assert record.parity_status == "EXACT_REPLAY_FAILED"
    assert record.exact_result is None
    assert record.exact_error == "RuntimeError: fixture"
