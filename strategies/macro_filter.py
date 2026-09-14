"""
strategies/macro_filter.py — 거시 필터와 null 정책 (ARCHITECTURE_V2.md §3.6.1)

이 파일이 존재하는 이유는 임계값 비교가 아니라 **결손 처리**다.

fs_v1 20220425 실측: 200종목 중 거시 피처가 있는 종목은 3개(커버리지 1.5%).
`params/default.yaml` 의 `filters.macro.enabled` 를 true 로 켜는 순간, 정책이
정의돼 있지 않으면 둘 중 하나가 **에러 없이** 일어난다.

    null 을 '조건 미달' 로 보면   -> 유니버스의 98.5%가 조용히 사라진다
    null 을 '판정 불가 -> 통과' 로 보면 -> 필터가 꺼진 채 켜져 있다고 착각한다

둘 다 백테스트는 정상 종료된다. 이런 종류의 실패가 가장 위험하다. 그래서 정책을
`filters.macro.on_missing` 파라미터로 **명시하게 만들고**, 판정 결과가 왜 그렇게
나왔는지(어느 피처가 null 이었는지)를 사유로 돌려준다.

L2/L3 경계: 피처는 값만 계산하고 임계값 비교는 여기(L3)서 한다. `obi_top3` 가
비율만 계산하고 `1.2` 비교는 params 에 있는 것과 같은 규칙이다 (§2).

⚠️ 시점 규칙: 입력으로 받는 거시 피처 값은 반드시 **전일까지 확정된** 값이어야
   한다. features/builders/macro.py 의 DailyMatrix.prior_rows() 가 그것을 강제한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "ON_MISSING_REJECT",
    "ON_MISSING_SKIP_FILTER",
    "ON_MISSING_POLICIES",
    "MacroDecision",
    "MacroFilterConfig",
    "evaluate_macro_filter",
]

#: 판정 불가 -> 진입 금지 (보수적, 기본값)
ON_MISSING_REJECT = "reject"

#: 판정 불가 -> 해당 조건만 미적용하고 통과 (관대)
ON_MISSING_SKIP_FILTER = "skip_filter"

ON_MISSING_POLICIES = (ON_MISSING_REJECT, ON_MISSING_SKIP_FILTER)

#: 조건 이름 -> (피처 이름, 비교 방향). 방향 "min" 은 하한, "max" 는 상한.
_CONDITIONS: tuple[tuple[str, str, str], ...] = (
    ("mkt_cap_min_eok", "mkt_cap", "min"),
    ("mkt_cap_max_eok", "mkt_cap", "max"),
    ("float_ratio_max_pct", "float_ratio", "max"),
    ("ytd_tradamt_min_eok", "ytd_tradamt_20", "min"),
)


@dataclass(frozen=True, slots=True)
class MacroDecision:
    """
    판정 결과. allowed 만 보지 말고 reasons 를 로그/매니페스트에 남길 것.

    '통과했다' 와 '판정할 수 없어 통과시켰다' 는 완전히 다른 사건인데, boolean
    하나로는 구분되지 않는다. skipped 가 비어 있지 않은 통과는 후자다.
    """
    allowed: bool
    reasons: tuple[str, ...] = ()          # 거부 사유 (allowed=False 일 때)
    skipped: tuple[str, ...] = ()          # 값이 없어 적용하지 못한 조건

    @property
    def evaluated_fully(self) -> bool:
        """모든 조건을 실제로 판정했는가. False 면 필터가 부분적으로만 걸린 것이다."""
        return not self.skipped


@dataclass(frozen=True, slots=True)
class MacroFilterConfig:
    """params 의 filters.macro 섹션을 검증해 담는다."""
    enabled: bool = False
    on_missing: str = ON_MISSING_REJECT
    thresholds: Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def from_params(cls, section: Mapping[str, Any]) -> "MacroFilterConfig":
        on_missing = str(section.get("on_missing", ON_MISSING_REJECT))
        if on_missing not in ON_MISSING_POLICIES:
            raise ValueError(
                f"filters.macro.on_missing 은 {ON_MISSING_POLICIES} 중 하나여야 합니다: "
                f"{on_missing!r}"
            )
        thresholds = {
            name: float(section[name])
            for name, _feature, _direction in _CONDITIONS
            if section.get(name) is not None
        }
        return cls(
            enabled=bool(section.get("enabled", False)),
            on_missing=on_missing,
            thresholds=thresholds,
        )


def _is_missing(value: Any) -> bool:
    """None / NaN 을 결손으로 본다. 0.0 은 결손이 아니라 값이다."""
    if value is None:
        return True
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return True


def evaluate_macro_filter(
    features: Mapping[str, Any],
    config: MacroFilterConfig,
) -> MacroDecision:
    """
    거시 피처 값으로 진입 자격을 판정한다.

        features  {"mkt_cap": 52000.0, "float_ratio": nan, ...}
        config    MacroFilterConfig.from_params(params["filters"]["macro"])

    비활성 상태면 아무 조건도 보지 않고 통과시킨다 (skipped 도 비어 있다 —
    '적용할 필터가 없는 것' 과 '적용하지 못한 것' 은 다르다).
    """
    if not config.enabled:
        return MacroDecision(allowed=True)

    reasons: list[str] = []
    skipped: list[str] = []

    for name, feature, direction in _CONDITIONS:
        limit = config.thresholds.get(name)
        if limit is None:
            continue

        value = features.get(feature)
        if _is_missing(value):
            if config.on_missing == ON_MISSING_REJECT:
                reasons.append(f"{feature} 값 없음 (on_missing=reject)")
            else:
                skipped.append(f"{name}: {feature} 값 없음")
            continue

        value = float(value)
        if direction == "min" and value < limit:
            reasons.append(f"{feature}={value:g} < {name}={limit:g}")
        elif direction == "max" and value > limit:
            reasons.append(f"{feature}={value:g} > {name}={limit:g}")

    return MacroDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        skipped=tuple(skipped),
    )
