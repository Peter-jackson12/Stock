"""
features/registry.py — 피처 등록 및 의존성 해석 (Phase B)

전략이 required_features = ("cbv_10", "obi_top3") 라고 선언하면
레지스트리가 해당 피처 객체를 찾아주고, 의존성을 위상 정렬해 계산 순서를 정한다.

이 자기 기술(self-describing) 구조 덕분에:
  - 배치 빌더는 '무엇을 계산할지' 를 안다
  - 실시간 엔진은 '어떤 스트리밍 상태를 띄울지' 를 안다
  - 대시보드는 '이 전략이 무엇을 보는지' 를 안다
전략이 N개가 되면 이 자기 기술성이 문서보다 훨씬 강력하다.
"""

from __future__ import annotations

from typing import Callable, Iterable

from features.base import Feature, FeatureSet

__all__ = [
    "register", "get", "all_features", "build_feature_set",
    "resolve_deps", "bootstrap", "reset",
]


_REGISTRY: dict[str, Feature] = {}
_BOOTSTRAPPED = False


def register(feature: Feature) -> Feature:
    """피처 인스턴스 등록. 이름 중복은 즉시 에러."""
    if not feature.name:
        raise ValueError(f"피처에 name 이 없습니다: {feature!r}")
    if feature.name in _REGISTRY:
        existing = _REGISTRY[feature.name]
        raise ValueError(
            f"피처 이름 중복: {feature.name} "
            f"(기존 {existing.version} vs 신규 {feature.version}). "
            f"계산식이 다르면 이름을 바꾸고, 같은 피처의 개선이면 version 을 올리세요."
        )
    _REGISTRY[feature.name] = feature
    return feature


def get(name: str) -> Feature:
    if name not in _REGISTRY:
        raise KeyError(
            f"등록되지 않은 피처: {name}. "
            f"등록된 피처: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]


def all_features() -> dict[str, Feature]:
    return dict(_REGISTRY)


def resolve_deps(names: Iterable[str]) -> list[Feature]:
    """
    요청된 피처 + 그 의존 피처를 위상 정렬해 계산 순서로 반환.

    deps 에는 원천 컬럼명(buy_vol, close, sec ...)과 다른 피처 이름이 섞여 있다.
    레지스트리에 있으면 피처로 보고 먼저 계산하고, 없으면 원천 컬럼으로 취급해
    넘어간다. 거시 피처의 'daily:mkt' 처럼 접두사가 붙은 것도 원천 취급이다.

    순환 의존은 즉시 예외다. 조용히 무한루프를 도는 게 최악이다.
    """
    order: list[Feature] = []
    state: dict[str, str] = {}

    def visit(name: str, stack: tuple[str, ...]) -> None:
        if state.get(name) == "done":
            return
        if state.get(name) == "visiting":
            raise ValueError(
                "피처 의존성에 순환이 있습니다: " + " -> ".join(stack + (name,))
            )
        if name not in _REGISTRY:          # 원천 컬럼 — 계산 대상이 아니다
            return
        state[name] = "visiting"
        for dep in _REGISTRY[name].deps:
            visit(dep, stack + (name,))
        state[name] = "done"
        order.append(_REGISTRY[name])

    for name in names:
        if name not in _REGISTRY:
            raise KeyError(
                f"등록되지 않은 피처: {name}. 등록된 피처: {sorted(_REGISTRY)}"
            )
        visit(name, ())
    return order


def build_feature_set(version: str, names: Iterable[str]) -> FeatureSet:
    """이름 목록으로 FeatureSet 구성 (저장 버전 단위)."""
    return FeatureSet(version=version, features=tuple(resolve_deps(names)))


def bootstrap(*, csv_path: str | None = None) -> dict[str, Feature]:
    """
    builders/ 하위 모듈을 import 해 기본 피처를 등록한다. 여러 번 불러도 안전하다.

    이 함수가 '자기 기술(self-describing)' 구조의 입구다. 한 번 부르면
      - 배치 빌더는 무엇을 계산할지 알고
      - 실시간 엔진은 어떤 스트리밍 상태를 띄울지 알고
      - 대시보드는 이 전략이 무엇을 보는지 안다.
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return all_features()

    from features.builders.microstructure import default_micro_features
    from features.builders.macro import default_macro_features

    for feature in default_micro_features():
        register(feature)
    for feature in default_macro_features(csv_path):
        register(feature)

    _BOOTSTRAPPED = True
    return all_features()


def reset() -> None:
    """레지스트리 비우기 (테스트용)."""
    global _BOOTSTRAPPED
    _REGISTRY.clear()
    _BOOTSTRAPPED = False
