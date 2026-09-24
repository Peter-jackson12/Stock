"""NXT breakout 한 전략을 공유계좌 엔진에서 실행하고 결과 JSON을 보존한다.

현재는 normalized in-memory event만 받는다. raw reader, qualification, 실제 주문,
다중 전략 arbitration은 연결하지 않는다. dataset_label은 caller label일 뿐 원본 인증이 아니다.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from engine.portfolio_session import PortfolioRunFailed, run_portfolio
from execution.portfolio_simulator import PortfolioSimulator
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
        "strategies/nxt_breakout/portfolio_adapter.py",
        "strategies/nxt_breakout/tick_research.py",
        "engine/nxt_tick_engine.py",
    )
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}


def _finalize_report(report: dict, *, dataset_label: str, strategy: NxtPortfolioStrategy,
                     started_at: str) -> dict:
    report = deepcopy(report)
    strategy_settings = strategy.settings()
    settings = deepcopy(report["settings"])
    settings["strategy"] = strategy_settings
    code_identity = dict(report["code_sha256"])
    code_identity.update(_strategy_code_identity())
    signals = [asdict(signal) for signal in strategy.signals]
    report.update(
        dataset_label=dataset_label,
        settings=settings,
        code_sha256=code_identity,
        strategy={
            "strategy_id": strategy.strategy_id,
            "adapter_version": strategy.adapter_version,
        },
        strategy_signals=signals,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )
    report["limitations"] = [
        item for item in report["limitations"]
        if item != "no_live_broker_no_nxt_multi_asset_adapter"
    ] + [
        "nxt_breakout_strategy_only_no_multi_strategy_arbitration",
        "no_live_broker_or_raw_input_adapter",
        "no_pnl_valuation_or_trade_store_conversion",
    ]
    identity = {
        "events": report["event_sha256"],
        "settings": settings,
        "code": code_identity,
        "intents": report["order_intents"],
        "signals": signals,
    }
    report["reproducibility_key"] = hashlib.sha256(
        _json(identity).encode("utf-8")
    ).hexdigest()
    return json.loads(_json(report))


def run_nxt_portfolio(events, *, output_root, dataset_label, simulator_config,
                      close_ns, quantity, exit_rule="fixed",
                      cooldown_ns=10_000_000_000, params=None):
    """Persist one NXT strategy run over a shared account.

    Invalid config fails before an output directory is created. Once event iteration
    begins, PortfolioRunFailed is written as diagnostics_only before re-raising.
    Reruns use fresh UUID directories and never overwrite prior evidence.
    """
    if not isinstance(dataset_label, str) or not dataset_label.strip():
        raise ValueError("dataset label required")
    config = deepcopy(simulator_config)
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
    )
    strategy_settings = strategy.settings()
    _json(strategy_settings)
    # Validate account/risk configuration before creating persistent output.
    PortfolioSimulator(**deepcopy(config))

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
        )
        _write(path, report)
        cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
        raise NxtPortfolioRunFailed(path, cause) from exc
    report = _finalize_report(
        report,
        dataset_label=dataset_label,
        strategy=strategy,
        started_at=started_at,
    )
    _write(path, report)
    return path
