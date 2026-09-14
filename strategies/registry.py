"""
strategies/registry.py — 전략 등록 및 로딩 (Phase C)

전략을 추가하는 절차를 "디렉토리 하나 + 데코레이터 하나"로 고정한다.
엔진 코드는 건드리지 않는다.

    strategies/
    └── nxt_breakout/
        ├── __init__.py
        ├── strategy.py          <- @register_strategy
        ├── rules.py             <- EntryRule / ExitRule 구현
        └── params/
            ├── default.yaml
            ├── aggressive.yaml
            └── conservative.yaml

사용:
    spec = load_strategy("nxt_breakout", variant="aggressive")
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Type

from strategies.base import StrategyParams, StrategySpec

__all__ = ["register_strategy", "load_strategy", "list_strategies", "load_params"]


STRATEGY_ROOT = Path(__file__).resolve().parent

_BUILDERS: dict[str, Callable[[StrategyParams], StrategySpec]] = {}


def register_strategy(strategy_id: str):
    """
    전략 빌더 등록 데코레이터.

        @register_strategy("nxt_breakout")
        def build(params: StrategyParams) -> StrategySpec:
            ...
    """
    def _decorator(builder: Callable[[StrategyParams], StrategySpec]):
        if strategy_id in _BUILDERS:
            raise ValueError(f"전략 ID 중복: {strategy_id}")
        _BUILDERS[strategy_id] = builder
        return builder
    return _decorator


def list_strategies() -> list[str]:
    return sorted(_BUILDERS)


def load_params(strategy_id: str, variant: str = "default") -> StrategyParams:
    """
    TODO(Phase C): strategies/<id>/params/<variant>.yaml 로드.

    구현 메모:
      - PyYAML 로 읽고 StrategyParams 로 감싼다
      - 파일이 없으면 사용 가능한 variant 목록을 함께 담아 에러를 낸다
      - 로드한 raw dict 은 그대로 RunManifest.params 에 들어가고
        hash_params() 로 param_hash 가 된다 -> 재현성의 근거
    """
    raise NotImplementedError("Phase C")


def load_strategy(strategy_id: str, variant: str = "default") -> StrategySpec:
    """
    TODO(Phase C): 파라미터를 로드하고 등록된 빌더로 StrategySpec 을 만든다.

        params = load_params(strategy_id, variant)
        return _BUILDERS[strategy_id](params)
    """
    raise NotImplementedError("Phase C")


def bootstrap() -> None:
    """
    TODO(Phase C): strategies/ 하위 패키지를 자동 import 해 데코레이터를 발동시킨다.
    (pkgutil.iter_modules 로 순회)
    """
    raise NotImplementedError("Phase C")
