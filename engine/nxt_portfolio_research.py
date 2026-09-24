"""NXT breakout 한 전략을 공유계좌 엔진에서 실행하고 결과 JSON을 보존한다.

현재는 normalized in-memory event만 받는다. raw reader, qualification, 실제 주문,
다중 전략 arbitration은 연결하지 않는다. dataset_label은 caller label일 뿐 원본 인증이 아니다.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from engine.portfolio_session import PortfolioRunFailed, run_portfolio
from execution.portfolio_accounting import (
    account_portfolio_fills,
    value_portfolio_at_bid_marks,
)
from execution.portfolio_simulator import PortfolioFill, PortfolioSimulator
from strategies.nxt_breakout.portfolio_adapter import NxtPortfolioStrategy


class NxtPortfolioRunFailed(RuntimeError):
    def __init__(self, path: Path, cause: Exception):
        super().__init__(f"NXT portfolio replay failed; diagnostics: {path}: {cause}")
        self.path = path


def _default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported result type: {type(value).__name__}")


def _json(value):
    return json.dumps(value, default=_default, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _write(path: Path, value) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _strategy_code_identity() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    names = (
        "engine/nxt_portfolio_research.py",
        "execution/portfolio_accounting.py",
        "strategies/nxt_breakout/portfolio_adapter.py",
        "strategies/nxt_breakout/tick_research.py",
        "strategies/nxt_breakout/direction_window.py",
        "engine/nxt_tick_engine.py",
    )
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}


def _fraction_record(value):
    if value is None:
        return None
    if not isinstance(value, Fraction):
        value = Fraction(value)
    return {
        "numerator": str(value.numerator),
        "denominator": str(value.denominator),
    }


def _performance_accounting(report: dict) -> dict:
    account = report["account"]
    settings = report["settings"]
    fills = tuple(
        PortfolioFill(
            item["order_id"],
            item["code"],
            item["venue"],
            item["time_ns"],
            item["side"],
            item["quantity"],
            Decimal(str(item["price"])),
            Decimal(str(item["fee"])),
            item["quote_seq"],
        )
        for item in account["fills"]
    )
    ledger = account_portfolio_fills(fills)
    valuation = value_portfolio_at_bid_marks(
        ledger,
        initial_cash=settings["cash"],
        current_cash=account["cash"],
        bid_marks={},
    )

    actual_positions = dict(account["positions"])
    ledger_positions = {
        item.code: item.quantity
        for item in ledger.positions
    }
    for code in settings["instruments"]:
        ledger_positions.setdefault(code, 0)
    position_reconciled = actual_positions == ledger_positions

    positions = [
        {
            "code": item.code,
            "quantity": item.quantity,
            "cost_basis": _fraction_record(item.cost_basis),
            "average_cost": _fraction_record(item.average_cost),
        }
        for item in ledger.positions
    ]
    flat = not any(actual_positions.values())
    return {
        "schema": "portfolio_performance_accounting_v1",
        "status": "flat_complete" if flat else "open_unpriced",
        "cost_basis_method": ledger.cost_basis_method,
        "marking_policy": valuation.marking_policy,
        "mark_integration": "not_connected",
        "hypothetical_exit_fee_included": valuation.hypothetical_exit_fee_included,
        "legacy_top_level_pnl_fields_populated": False,
        "fill_count": ledger.fill_count,
        "positions": positions,
        "realized_pnl": _fraction_record(valuation.realized_pnl),
        "unrealized_pnl": _fraction_record(valuation.unrealized_pnl),
        "total_pnl": _fraction_record(valuation.total_pnl),
        "equity": _fraction_record(valuation.equity),
        "initial_cash": _fraction_record(valuation.initial_cash),
        "current_cash": _fraction_record(valuation.current_cash),
        "expected_cash_from_fills": _fraction_record(valuation.expected_cash_from_fills),
        "cash_reconciled": valuation.cash_reconciled,
        "position_reconciled": position_reconciled,
        "accounting_identity_reconciled": valuation.accounting_identity_reconciled,
        "unpriced_codes": list(valuation.unpriced_codes),
        "bid_marks": {
            code: _fraction_record(value)
            for code, value in valuation.marks.items()
        },
    }


def _finalize_report(report: dict, *, dataset_label: str, strategy: NxtPortfolioStrategy,
                     started_at: str, input_provenance) -> dict:
    report = deepcopy(report)
    strategy_settings = strategy.settings()
    settings = deepcopy(report["settings"])
    settings["strategy"] = strategy_settings
    code_identity = dict(report["code_sha256"])
    code_identity.update(_strategy_code_identity())
    signals = [asdict(signal) for signal in strategy.signals]
    performance_accounting = _performance_accounting(report)
    report.update(
        dataset_label=dataset_label,
        settings=settings,
        code_sha256=code_identity,
        strategy={
            "strategy_id": strategy.strategy_id,
            "adapter_version": strategy.adapter_version,
        },
        strategy_signals=signals,
        performance_accounting=performance_accounting,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
        input_provenance=deepcopy(input_provenance),
    )
    report["limitations"] = [
        item for item in report["limitations"]
        if item not in (
            "no_live_broker_no_nxt_multi_asset_adapter",
            "no_pnl_valuation_no_forced_liquidation",
        )
    ] + [
        "nxt_breakout_strategy_only_no_multi_strategy_arbitration",
        "no_live_broker",
        "no_performance_certified_raw_adapter",
        "performance_accounting_subrecord_only_legacy_top_level_pnl_fields_unpopulated",
        "open_position_final_bid_mark_integration_not_connected",
        "no_forced_liquidation",
        "no_trade_store_conversion",
    ]
    identity = {
        "events": report["event_sha256"],
        "settings": settings,
        "code": code_identity,
        "intents": report["order_intents"],
        "signals": signals,
        "performance_accounting": performance_accounting,
        "input_provenance": input_provenance,
    }
    report["reproducibility_key"] = hashlib.sha256(
        _json(identity).encode("utf-8")
    ).hexdigest()
    return json.loads(_json(report))


def run_nxt_portfolio(events, *, output_root, dataset_label, simulator_config,
                      close_ns, quantity, exit_rule="fixed",
                      cooldown_ns=10_000_000_000, params=None,
                      unknown_direction_policy="strict",
                      input_provenance=None):
    """Persist one NXT strategy run over a shared account.

    Invalid config fails before an output directory is created. Once event iteration
    begins, PortfolioRunFailed is written as diagnostics_only before re-raising.
    Reruns use fresh UUID directories and never overwrite prior evidence.
    """
    if not isinstance(dataset_label, str) or not dataset_label.strip():
        raise ValueError("dataset label required")
    config = dict(simulator_config)
    if "fee_rate" not in config:
        raise ValueError("explicit fee_rate required; no silent zero-cost run")
    if "instruments" not in config:
        raise ValueError("portfolio instruments required")
    strategy = NxtPortfolioStrategy(
        instruments=config["instruments"],
        quantity=quantity,
        exit_rule=exit_rule,
        cooldown_ns=cooldown_ns,
        params=params,
        unknown_direction_policy=unknown_direction_policy,
    )
    strategy_settings = strategy.settings()
    provenance = deepcopy(input_provenance)
    _json(strategy_settings)
    _json(provenance)
    # Validate account/risk configuration before creating persistent output.
    PortfolioSimulator(**config)

    root = Path(output_root).resolve()
    run_dir = root / uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=False)
    path = run_dir / "result.json"
    started_at = datetime.now(timezone.utc).isoformat()
    _write(path, {
        "schema": "portfolio_research_result_v1",
        "status": "running",
        "dataset_label": dataset_label,
        "raw_identity_verified": False,
        "settings": {"strategy": strategy_settings},
        "input_provenance": provenance,
        "started_at": started_at,
    })

    try:
        report = run_portfolio(
            events,
            simulator_config=config,
            close_ns=close_ns,
            strategy=strategy,
            strategy_id=strategy.strategy_id,
        )
    except PortfolioRunFailed as exc:
        report = _finalize_report(
            exc.report,
            dataset_label=dataset_label,
            strategy=strategy,
            started_at=started_at,
            input_provenance=provenance,
        )
        _write(path, report)
        cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
        raise NxtPortfolioRunFailed(path, cause) from exc
    report = _finalize_report(
        report,
        dataset_label=dataset_label,
        strategy=strategy,
        started_at=started_at,
        input_provenance=provenance,
    )
    _write(path, report)
    return path
