"""상위 후보만 기존 production exact engine으로 보내는 bridge."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from engine.nxt_portfolio_research import run_nxt_portfolio
from engine.tick_ordering import OrderedTick
from research.fast_backtest.sweep import (
    FastCandidate,
    FastSweepResult,
    materialize_strategy_params,
)


@dataclass(frozen=True)
class ExactBridgeRecord:
    candidate_id: str
    parameter_identity: str
    screening_only: bool
    fast_result: Mapping[str, Any]
    exact_result: Mapping[str, Any] | None
    parity_status: str
    mismatches: tuple[str, ...]
    exact_error: str | None


def _fraction(value: Mapping[str, Any] | None) -> float | None:
    if value is None:
        return None
    return float(Fraction(int(value["numerator"]), int(value["denominator"])))


def compare_fast_exact(fast: FastSweepResult, exact: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    mismatches: list[str] = []
    signals = exact.get("strategy_signals", [])
    entry_times = tuple(item["time_ns"] for item in signals if item.get("side") == "buy")
    exit_times = tuple(item["time_ns"] for item in signals if item.get("side") == "sell")
    fills = exact.get("account", {}).get("fills", [])
    trades = sum(item.get("side") == "sell" for item in fills)
    exact_pnl = _fraction(exact.get("performance_accounting", {}).get("total_pnl"))
    if entry_times != fast.entry_decision_ns:
        mismatches.append("entry_decision_ns")
    if exit_times != fast.exit_decision_ns:
        mismatches.append("exit_decision_ns")
    if len(fills) != fast.fills:
        mismatches.append("fill_count")
    if trades != fast.trades:
        mismatches.append("trade_count")
    if (exact_pnl is None) != (fast.net_pnl is None) or (
        exact_pnl is not None and fast.net_pnl is not None and abs(exact_pnl - fast.net_pnl) > 1e-9
    ):
        mismatches.append("net_pnl")
    return ("PASS" if not mismatches else "FAST_EXACT_MISMATCH", tuple(mismatches))


def run_exact_bridge(
    ranked_fast_results: Iterable[FastSweepResult],
    candidates: Iterable[FastCandidate],
    *,
    top_n: int,
    exact_runner: Callable[[FastCandidate], Mapping[str, Any]],
) -> tuple[ExactBridgeRecord, ...]:
    if type(top_n) is not int or top_n < 0:
        raise ValueError("top_n must be nonnegative")
    by_identity = {candidate.parameter_identity: candidate for candidate in candidates}
    records: list[ExactBridgeRecord] = []
    for fast in tuple(ranked_fast_results)[:top_n]:
        candidate = by_identity.get(fast.parameter_identity)
        if candidate is None:
            raise ValueError("ranked result has no candidate")
        try:
            exact = exact_runner(candidate)
            status, mismatches = compare_fast_exact(fast, exact)
            error = None
        except Exception as exc:
            exact = None
            status = "EXACT_REPLAY_FAILED"
            mismatches = ()
            error = f"{type(exc).__name__}: {exc}"
        records.append(ExactBridgeRecord(
            candidate_id=fast.candidate_id,
            parameter_identity=fast.parameter_identity,
            screening_only=True,
            fast_result=asdict(fast),
            exact_result=exact,
            parity_status=status,
            mismatches=mismatches,
            exact_error=error,
        ))
    return tuple(records)


def production_exact_runner(
    events: Iterable[OrderedTick],
    *,
    output_root: str | Path,
    dataset_label: str,
    simulator_config: Mapping[str, Any],
    close_ns: int,
    quantity: int,
    cooldown_ns: int,
    unknown_direction_policy: str,
    input_provenance: Mapping[str, Any],
) -> Callable[[FastCandidate], Mapping[str, Any]]:
    materialized_events = tuple(events)
    root = Path(output_root)

    def run(candidate: FastCandidate) -> Mapping[str, Any]:
        params, exit_rule = materialize_strategy_params(candidate.params)
        path = run_nxt_portfolio(
            materialized_events,
            output_root=root,
            dataset_label=f"{dataset_label}:{candidate.candidate_id}",
            simulator_config=dict(simulator_config),
            close_ns=close_ns,
            quantity=quantity,
            exit_rule=exit_rule,
            cooldown_ns=cooldown_ns,
            params=params,
            unknown_direction_policy=unknown_direction_policy,
            input_provenance=dict(input_provenance) | {
                "fast_backtest_parameter_identity": candidate.parameter_identity,
                "fast_backtest_screening_only": True,
            },
        )
        return json.loads(path.read_text(encoding="utf-8"))

    return run
