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

__all__ = ["register", "get", "all_features", "build_feature_set", "resolve_deps"]


_REGISTRY: dict[str, Feature] = {}


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
    TODO(Phase B): 요청된 피처 + 그 의존 피처를 위상 정렬해 계산 순서로 반환.

    구현 메모:
      - deps 에는 원천 컬럼명(buy_vol 등)과 피처명이 섞여 있다.
        레지스트리에 있으면 피처, 없으면 원천 컬럼으로 취급한다.
      - 순환 의존은 즉시 예외로 보고할 것 (조용히 무한루프 도는 게 최악).
    """
    raise NotImplementedError("Phase B")


def build_feature_set(version: str, names: Iterable[str]) -> FeatureSet:
    """이름 목록으로 FeatureSet 구성 (저장 버전 단위)."""
    return FeatureSet(version=version, features=tuple(resolve_deps(names)))


def bootstrap() -> None:
    """
    TODO(Phase B): builders/ 하위 모듈을 import 해 기본 피처를 등록한다.

        from features.builders.microstructure import default_micro_features
        for f in default_micro_features():
            register(f)
    """
    raise NotImplementedError("Phase B")
