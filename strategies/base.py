"""
strategies/base.py — L3 전략 레이어 계약 (Phase C)

핵심 분해:  Strategy = EntryRule x ExitRule[] x Filter[]

engine/nxt_tick_engine.py 는 이미 이 구조를 암시하고 있다. 하나의 진입 규칙에
세 개의 청산 규칙(A_Fixed / B_2TickTrail / C_StepTrail)을 동시에 돌려 비교한다.
다만 그 조합이 함수 안의 딕셔너리로 하드코딩되어 있을 뿐이다.

이걸 정식 구조로 올리면 "네 번째 청산 규칙 추가" 비용이
ExitRule 구현 1개 + yaml 한 줄로 떨어진다.

⚠️ 규칙: **숫자는 코드에 들어가지 않는다.**
   지금 check_entry_conditions() 의 2.3 / 31 / 700 / 0.18 같은 매직넘버는
   전부 params/*.yaml 로 나간다. 이 규칙 하나만 지켜도 전략 N개 관리가 가능해진다.

참고: ARCHITECTURE_V2.md §4
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional, Sequence

from core.contracts import MarketSnapshot, Position, Signal

__all__ = [
    "StrategyParams",
    "Filter",
    "EntryRule",
    "ExitRule",
    "StrategySpec",
]


# ---------------------------------------------------------------------------
# 파라미터
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class StrategyParams:
    """
    params/*.yaml 을 로드한 결과.

    dict 을 그대로 들고 다니는 대신 이 래퍼를 쓰는 이유:
      - get() 이 키 오타를 즉시 잡는다 (조용한 기본값 0 이 최악)
      - hash() 가 런 매니페스트의 param_hash 로 그대로 들어간다
    """
    raw: Mapping[str, Any]
    variant: str = "default"

    def get(self, path: str) -> Any:
        """점 표기 조회: params.get("entry.buy_ratio_min")"""
        node: Any = self.raw
        for key in path.split("."):
            if not isinstance(node, Mapping) or key not in node:
                raise KeyError(f"파라미터 없음: {path} (variant={self.variant})")
            node = node[key]
        return node

    def section(self, name: str) -> Mapping[str, Any]:
        return self.get(name)


# ---------------------------------------------------------------------------
# 구성 요소
# ---------------------------------------------------------------------------

class Filter(ABC):
    """
    진입 자격 필터. 시그널 발생 '이전' 단계에서 종목/시점을 걸러낸다.

    두 종류가 있다:
      - 거시 필터: 시총·유통비율·거래대금 (일 1회 판정, 종목 단위)
        -> ARCHITECTURE_V2.md §1.5 의 '끊어진 연결선'을 잇는 자리
      - 상황 필터: NXT 프리마켓 과열, 호가 스프레드 (시점 단위)
    """

    id: str = ""

    @abstractmethod
    def allows(self, snap: MarketSnapshot, params: StrategyParams) -> bool:
        """진입을 허용하면 True."""


class EntryRule(ABC):
    """
    진입 규칙. 피처와 파라미터만 보고 판단한다.

    ⚠️ 이 안에서 DB 를 열거나 브로커를 부르면 안 된다.
       전략은 자신이 백테스트 중인지 실전 중인지 알지 못해야 한다.
    """

    id: str = ""
    required_features: tuple[str, ...] = ()

    @abstractmethod
    def evaluate(self, snap: MarketSnapshot, params: StrategyParams) -> Optional[Signal]:
        """진입 시그널 또는 None."""


class ExitRule(ABC):
    """
    청산 규칙. 포지션 보유 중에만 호출된다.

    백테스트에서는 여러 ExitRule 을 **동시에** 돌려 성적을 비교하고
    (nxt_tick_engine 의 3대 트레일링 컷 비교가 이것),
    실전에서는 그중 하나만 활성화한다.
    """

    id: str = ""
    required_features: tuple[str, ...] = ()

    @abstractmethod
    def evaluate(
        self, snap: MarketSnapshot, position: Position, params: StrategyParams
    ) -> Optional[Signal]:
        """청산 시그널 또는 None."""


# ---------------------------------------------------------------------------
# 전략 명세
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class StrategySpec:
    """
    하나의 전략을 완전히 기술하는 명세.

    required_features 를 선언하면 L2 가 무엇을 계산할지 알고,
    실시간 엔진이 어떤 스트리밍 상태를 띄울지 알고,
    대시보드가 이 전략이 무엇을 보는지 안다.
    """

    id: str                                     # "nxt_breakout"
    version: str                                # "1.2.0"
    engine: Literal["tick", "bar"]              # 어느 해상도 엔진에서 도는가
    entry: EntryRule
    exits: tuple[ExitRule, ...]                 # 복수 — 백테스트 비교용
    filters: tuple[Filter, ...] = ()
    params: Optional[StrategyParams] = None
    description: str = ""

    @property
    def required_features(self) -> tuple[str, ...]:
        """진입·청산 규칙이 선언한 피처의 합집합. L2 계산 대상이 된다."""
        names: list[str] = list(self.entry.required_features)
        for rule in self.exits:
            names.extend(rule.required_features)
        return tuple(dict.fromkeys(names))      # 순서 보존 dedupe

    def active_exit(self, exit_id: str) -> ExitRule:
        """실전 투입 시 하나만 고른다."""
        for rule in self.exits:
            if rule.id == exit_id:
                return rule
        raise KeyError(f"청산 규칙 없음: {exit_id} (가능: {[r.id for r in self.exits]})")
