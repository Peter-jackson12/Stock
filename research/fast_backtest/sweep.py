"""Feature cache 위에서만 동작하는 screening-only parameter sweep."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
from typing import Any, Iterable, Mapping

from engine.nxt_tick_engine import PARAMS, get_tick_size
from research.fast_backtest.features import CausalFeatureCache, FeatureRow
from research.fast_backtest.plan import canonical_json


@dataclass(frozen=True)
class FastAccountAssumptions:
    cash: float
    fee_rate: float
    quantity: int
    buy_latency_ns: int
    sell_latency_ns: int
    max_quote_age_ns: int
    cooldown_ns: int
    close_ns: int

    def __post_init__(self) -> None:
        if self.cash < 0 or not 0 <= self.fee_rate < 1:
            raise ValueError("invalid cash or fee rate")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("positive integer quantity required")
        for name in ("buy_latency_ns", "sell_latency_ns", "max_quote_age_ns", "cooldown_ns"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if type(self.close_ns) is not int or self.close_ns <= 0:
            raise ValueError("positive exclusive close required")


@dataclass(frozen=True)
class FastCandidate:
    candidate_id: str
    parameter_identity: str
    params: Mapping[str, Any]
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class FastFill:
    time_ns: int
    side: str
    quantity: int
    price: float
    fee: float
    quote_seq: int


@dataclass(frozen=True)
class FastSweepResult:
    candidate_id: str
    parameter_identity: str
    aliases: tuple[str, ...]
    screening_only: bool
    screening_status: str
    signals: int
    entry_signals: int
    exit_signals: int
    fills: int
    trades: int
    gross_pnl: float | None
    fees: float
    net_pnl: float | None
    max_adverse_pct: float | None
    terminal_position: int
    entry_decision_ns: tuple[int, ...]
    exit_decision_ns: tuple[int, ...]
    fill_records: tuple[FastFill, ...]


def _canonical_number(value: int | float | Decimal) -> str:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("nonfinite parameter")
    normalized = number.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


def _canonical_parameter(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if type(value) in (int, float, Decimal):
        return {"number": _canonical_number(value)}
    if isinstance(value, Mapping):
        return {str(key): _canonical_parameter(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_parameter(item) for item in value]
    raise ValueError(f"unsupported parameter type: {type(value).__name__}")


def parameter_identity(params: Mapping[str, Any]) -> str:
    if not isinstance(params, Mapping):
        raise ValueError("parameter mapping required")
    return hashlib.sha256(canonical_json(_canonical_parameter(params)).encode("utf-8")).hexdigest()


def deduplicate_candidates(records: Iterable[Mapping[str, Any]]) -> tuple[FastCandidate, ...]:
    grouped: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, Mapping) or not isinstance(record.get("params"), Mapping):
            raise ValueError("candidate record requires params mapping")
        source_id = str(record.get("candidate_id", index))
        identity = parameter_identity(record["params"])
        if identity not in grouped:
            grouped[identity] = {"candidate_id": source_id, "params": deepcopy(record["params"]), "aliases": []}
        grouped[identity]["aliases"].append(source_id)
    return tuple(
        FastCandidate(
            candidate_id=value["candidate_id"],
            parameter_identity=identity,
            params=value["params"],
            aliases=tuple(value["aliases"]),
        )
        for identity, value in sorted(grouped.items())
    )


def materialize_strategy_params(value: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    params = deepcopy(PARAMS)
    supplied = deepcopy(dict(value))
    exit_rule = supplied.pop("exit_rule", "fixed")
    if exit_rule not in params["exits"]:
        raise ValueError("unsupported exit rule")
    for section in ("entry", "nxt_overheat", "macro", "exits"):
        nested = supplied.pop(section, None)
        if nested is not None:
            if not isinstance(nested, Mapping):
                raise ValueError(f"{section} parameters must be a mapping")
            if section == "exits":
                for name, settings in nested.items():
                    params["exits"].setdefault(name, {}).update(settings)
            else:
                params[section].update(nested)
    if "fee_rate" in supplied:
        params["fee_rate"] = supplied.pop("fee_rate")
    for name in tuple(params["entry"]):
        if name in supplied:
            params["entry"][name] = supplied.pop(name)
    for name in tuple(params["exits"][exit_rule]):
        if name in supplied:
            params["exits"][exit_rule][name] = supplied.pop(name)
    if supplied:
        raise ValueError(f"unknown strategy parameter(s): {','.join(sorted(supplied))}")
    if params["macro"]["enabled"]:
        raise ValueError("point-in-time macro adapter not connected")
    return params, exit_rule


def feature_config_for_candidates(candidates: Iterable[FastCandidate], max_quote_age_ns: int):
    from research.fast_backtest.features import FeatureConfig

    recent, breakout, sessions = set(), set(), set()
    for candidate in candidates:
        params, _ = materialize_strategy_params(candidate.params)
        recent.add(int(params["entry"]["recent_ticks"]))
        breakout.add(int(params["entry"]["breakout_window_sec"]))
        sessions.add(int(params["entry"]["session_start_sec"]))
    return FeatureConfig(tuple(recent), tuple(breakout), tuple(sessions), max_quote_age_ns)


def _quote_at(row: FeatureRow, time_ns: int, max_age_ns: int) -> bool:
    return bool(
        row.quote_eligible
        and row.quote_received_ns is not None
        and 0 <= time_ns - row.quote_received_ns <= max_age_ns
    )


def evaluate_candidate(
    cache: CausalFeatureCache,
    candidate: FastCandidate,
    *,
    account: FastAccountAssumptions,
) -> FastSweepResult:
    params, exit_rule = materialize_strategy_params(candidate.params)
    entry = params["entry"]
    overheat = params["nxt_overheat"]
    exit_params = params["exits"][exit_rule]
    recent_key = str(entry["recent_ticks"])
    breakout_key = str(entry["breakout_window_sec"])
    session_key = str(entry["session_start_sec"])
    if not cache.rows:
        raise ValueError("empty feature cache")
    codes = {row.code for row in cache.rows}
    if len(codes) != 1:
        raise ValueError("Fast Backtest v1 sweep currently requires one instrument per feature cache")

    cash = float(account.cash)
    initial_cash = cash
    position = 0
    average = peak = 0.0
    pending: dict[str, Any] | None = None
    fills: list[FastFill] = []
    entry_decisions: list[int] = []
    exit_decisions: list[int] = []
    cooldown_until = 0
    pre_open = pre_last = None
    pre_volume = 0
    max_adverse = None
    quote_row: FeatureRow | None = None
    liquidity = {"buy": 0, "sell": 0}
    gross_pnl = fees = 0.0
    trades = 0

    def try_fill(time_ns: int) -> None:
        nonlocal pending, cash, position, average, peak, gross_pnl, fees, trades, cooldown_until
        if pending is None or pending["ready_ns"] > time_ns or quote_row is None:
            return
        if not _quote_at(quote_row, time_ns, account.max_quote_age_ns):
            return
        side = pending["side"]
        if liquidity[side] < account.quantity:
            return
        price = quote_row.ask if side == "buy" else quote_row.bid
        if price is None:
            return
        fee = price * account.quantity * account.fee_rate
        if side == "buy":
            cost = price * account.quantity + fee
            if cost > cash:
                return
            cash -= cost
            position += account.quantity
            average = price
            peak = price
        else:
            cash += price * account.quantity - fee
            gross_pnl += (price - average) * account.quantity
            position -= account.quantity
            if position == 0:
                trades += 1
                cooldown_until = time_ns + account.cooldown_ns
                average = peak = 0.0
        liquidity[side] -= account.quantity
        fees += fee
        fills.append(FastFill(time_ns, side, account.quantity, price, fee, quote_row.quote_seq))
        pending = None

    for row in cache.rows:
        if row.received_ns >= account.close_ns:
            break
        if pending is not None and pending["ready_ns"] <= row.received_ns:
            try_fill(pending["ready_ns"])
        if row.kind == "quote":
            quote_row = row
            liquidity = {
                "buy": row.ask_size if row.quote_eligible and row.ask_size is not None else 0,
                "sell": row.bid_size if row.quote_eligible and row.bid_size is not None else 0,
            }
        try_fill(row.received_ns)

        if row.kind == "trade":
            if overheat["window"][0] <= row.market_second < overheat["window"][1]:
                if pre_open is None:
                    pre_open = row.trade_price
                pre_last = row.trade_price
                pre_volume += row.trade_volume
            if position:
                peak = max(peak, row.trade_price)

        if position and pending is None and _quote_at(row, row.received_ns, account.max_quote_age_ns):
            pnl = (row.bid - average) / average
            max_adverse = pnl if max_adverse is None else min(max_adverse, pnl)
            trigger = pnl <= float(exit_params["stop_loss_pct"])
            if exit_rule == "fixed":
                trigger = trigger or pnl >= float(exit_params["take_profit_pct"])
            elif exit_rule == "tick_trail":
                trigger = trigger or row.bid <= peak - get_tick_size(peak) * int(exit_params["trail_ticks"])
            else:
                armed = peak >= average * (1 + float(exit_params["arm_threshold_pct"]))
                if armed:
                    trigger = trigger or (
                        row.bid <= average + get_tick_size(average) * int(exit_params["breakeven_offset_ticks"])
                        or row.bid <= peak - get_tick_size(peak) * int(exit_params["trail_ticks"])
                    )
            if trigger:
                exit_decisions.append(row.received_ns)
                pending = {"side": "sell", "ready_ns": row.received_ns + account.sell_latency_ns}
                try_fill(row.received_ns)
                continue

        if row.kind != "trade" or position or pending is not None or row.received_ns < cooldown_until:
            continue
        open_price = row.open_price_by_session.get(session_key)
        ratio = row.buy_ratio_by_ticks.get(recent_key)
        volume = row.recent_volume_by_ticks.get(recent_key)
        prior_high = row.prior_high_by_window.get(breakout_key)
        exhausted = bool(
            pre_open is not None
            and pre_last / pre_open - 1 >= float(overheat["gain_threshold"])
            and pre_volume >= int(overheat["volume_threshold"])
        )
        eligible = bool(
            not exhausted
            and open_price is not None
            and _quote_at(row, row.received_ns, account.max_quote_age_ns)
            and row.bid3 is not None
            and row.ask3 is not None
            and row.ask3 > 0
            and ratio is not None
            and volume is not None
            and prior_high is not None
            and row.spread_pct is not None
            and row.trade_price >= open_price
            and row.trade_price >= prior_high
            and row.spread_pct <= float(entry["spread_max_pct"])
            and ratio >= float(entry["buy_ratio_min"])
            and row.bid3 > float(entry["obi_min_ratio"]) * row.ask3
            and volume >= int(entry["min_vol_15t"])
        )
        if eligible:
            entry_decisions.append(row.received_ns)
            pending = {"side": "buy", "ready_ns": row.received_ns + account.buy_latency_ns}
            try_fill(row.received_ns)

    if pending is not None and pending["ready_ns"] < account.close_ns:
        try_fill(pending["ready_ns"])
    net_pnl = cash - initial_cash if position == 0 else None
    status = "COMPLETE_FLAT" if position == 0 else "OPEN_POSITION_UNRANKED"
    return FastSweepResult(
        candidate_id=candidate.candidate_id,
        parameter_identity=candidate.parameter_identity,
        aliases=candidate.aliases,
        screening_only=True,
        screening_status=status,
        signals=len(entry_decisions) + len(exit_decisions),
        entry_signals=len(entry_decisions),
        exit_signals=len(exit_decisions),
        fills=len(fills),
        trades=trades,
        gross_pnl=gross_pnl if position == 0 else None,
        fees=fees,
        net_pnl=net_pnl,
        max_adverse_pct=max_adverse,
        terminal_position=position,
        entry_decision_ns=tuple(entry_decisions),
        exit_decision_ns=tuple(exit_decisions),
        fill_records=tuple(fills),
    )


def rank_results(results: Iterable[FastSweepResult]) -> tuple[FastSweepResult, ...]:
    return tuple(sorted(
        results,
        key=lambda result: (
            result.net_pnl is None,
            -(result.net_pnl if result.net_pnl is not None else float("-inf")),
            result.parameter_identity,
        ),
    ))


def run_fast_sweep(
    cache: CausalFeatureCache,
    candidates: Iterable[FastCandidate],
    *,
    account: FastAccountAssumptions,
) -> tuple[FastSweepResult, ...]:
    return rank_results(evaluate_candidate(cache, candidate, account=account) for candidate in candidates)


def result_record(result: FastSweepResult) -> dict[str, Any]:
    return asdict(result)
