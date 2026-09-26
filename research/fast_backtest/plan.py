"""Fast Backtest v1의 불변 실행 계획 계약."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "fast_backtest_plan_v1"
UNIVERSE_MODES = frozenset({"causal_preopen", "posthoc_same_day"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _date(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date") from exc


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


@dataclass(frozen=True)
class FastBacktestPlan:
    trade_dates: tuple[str, ...]
    instruments: tuple[str, ...]
    universe_source: str
    universe_mode: str
    historical_metadata_source: str
    universe_decision_cutoff: str
    cheap_filter_spec: Mapping[str, Any]
    tick_input_provenance: Mapping[str, Any]
    cutoff_market_second_exclusive: int
    account_assumptions: Mapping[str, Any]
    candidate_source: str
    parameter_identities: tuple[str, ...]
    exact_top_n: int
    exact_ranking: tuple[str, ...]
    output_directory: str
    code_revision: str
    schema: str = SCHEMA
    screening_only: bool = True
    seed: int | None = None
    posthoc_same_day_opt_in: bool = False
    limitations: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise ValueError(f"unsupported plan schema: {self.schema}")
        if self.screening_only is not True:
            raise ValueError("Fast Backtest v1 must remain screening_only")
        if not self.trade_dates:
            raise ValueError("at least one trade date required")
        normalized_dates = tuple(_date(value, "trade date") for value in self.trade_dates)
        if len(set(normalized_dates)) != len(normalized_dates):
            raise ValueError("duplicate trade date")
        object.__setattr__(self, "trade_dates", normalized_dates)
        if any(not isinstance(code, str) or not code for code in self.instruments):
            raise ValueError("instrument codes must be nonempty strings")
        if len(set(self.instruments)) != len(self.instruments):
            raise ValueError("duplicate instrument")
        if self.universe_mode not in UNIVERSE_MODES:
            raise ValueError("unsupported universe mode")
        if self.universe_mode == "posthoc_same_day" and not self.posthoc_same_day_opt_in:
            raise ValueError("posthoc_same_day requires explicit opt-in")
        if type(self.cutoff_market_second_exclusive) is not int or not 0 < self.cutoff_market_second_exclusive <= 86400:
            raise ValueError("cutoff must be an exclusive market second in (0, 86400]")
        if type(self.exact_top_n) is not int or self.exact_top_n < 0:
            raise ValueError("exact_top_n must be a nonnegative integer")
        if not self.parameter_identities or any(
            not isinstance(identity, str) or not identity for identity in self.parameter_identities
        ):
            raise ValueError("parameter identities required")
        if not self.exact_ranking:
            raise ValueError("deterministic exact ranking required")
        _decision_cutoff = self.universe_decision_cutoff
        try:
            parsed_cutoff = datetime.fromisoformat(
                _decision_cutoff.replace("Z", "+00:00")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("universe_decision_cutoff must be a timezone-aware ISO timestamp") from exc
        if parsed_cutoff.tzinfo is None or parsed_cutoff.utcoffset() is None:
            raise ValueError("universe_decision_cutoff must be a timezone-aware ISO timestamp")
        if self.seed is not None and type(self.seed) is not int:
            raise ValueError("seed must be an integer")
        for value, name in (
            (self.universe_source, "universe_source"),
            (self.historical_metadata_source, "historical_metadata_source"),
            (self.universe_decision_cutoff, "universe_decision_cutoff"),
            (self.candidate_source, "candidate_source"),
            (self.output_directory, "output_directory"),
            (self.code_revision, "code_revision"),
        ):
            _nonempty(value, name)
        if not isinstance(self.cheap_filter_spec, Mapping):
            raise ValueError("cheap_filter_spec must be a mapping")
        if not isinstance(self.tick_input_provenance, Mapping) or not self.tick_input_provenance:
            raise ValueError("tick_input_provenance must be a nonempty mapping")
        if not isinstance(self.account_assumptions, Mapping) or not self.account_assumptions:
            raise ValueError("account_assumptions must be a nonempty mapping")
        canonical_json(self.to_dict())

    @property
    def non_causal(self) -> bool:
        return self.universe_mode == "posthoc_same_day"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["non_causal"] = self.non_causal
        value["size_filter_basis"] = "total_market_cap_proxy"
        return value

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FastBacktestPlan":
        if not isinstance(value, Mapping):
            raise ValueError("plan must be a mapping")
        fields = dict(value)
        fields.pop("non_causal", None)
        fields.pop("size_filter_basis", None)
        for name in ("trade_dates", "instruments", "parameter_identities", "exact_ranking", "limitations"):
            if name in fields:
                fields[name] = tuple(fields[name])
        return cls(**fields)

    @classmethod
    def read(cls, path: str | Path) -> "FastBacktestPlan":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
