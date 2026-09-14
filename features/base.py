"""
features/base.py — L2 피처 레이어의 핵심 계약 (Phase B/D)

이 파일의 존재 이유 한 문장:
    **백테스트와 실전이 같은 숫자를 보게 하기 위해서.**

백테스트는 과거 배열을 뒤로 슬라이스해 피처를 만들고(engine/strategy.py 의 현재 방식),
실전은 앞으로 흘러오는 이벤트를 누적해 피처를 만든다. 두 코드를 따로 짜면 반드시
미세하게 어긋나고, 그 어긋남은 에러가 아니라 "백테스트에서만 잘 되는 전략" 이라는
형태로 드러난다.

그래서 하나의 피처를 batch / stream 두 구현으로 **같은 클래스 안에** 선언하고,
tests/test_feature_parity.py 가 둘이 같은 값을 내는지 기계로 검증한다.
패리티 테스트를 통과하지 못한 피처는 실전에 올릴 수 없다.

참고: ARCHITECTURE_V2.md §3
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

import numpy as np

__all__ = [
    "BatchContext",
    "StreamingState",
    "Feature",
    "MicroFeature",
    "MacroFeature",
    "FeatureSet",
]


# ---------------------------------------------------------------------------
# 배치 계산 입력
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class BatchContext:
    """
    하루치 한 종목의 원천 데이터. 배치 피처 계산의 입력.

    컬럼은 LOB 스키마(51컬럼) 또는 raw tick 스키마를 numpy 배열로 펼친 것.
    피처는 필요한 컬럼만 deps 로 선언하고 여기서 꺼내 쓴다.
    """
    code: str
    date: str                                   # "YYYYMMDD"
    columns: Mapping[str, np.ndarray]           # "close" -> ndarray, "buy_vol" -> ndarray ...
    resolution: str = "bar_1s"                  # "bar_1s" | "tick"

    # 이미 계산된 하위 피처 (의존성 해석 결과가 여기에 채워진다)
    computed: dict[str, np.ndarray] = field(default_factory=dict)

    def col(self, name: str) -> np.ndarray:
        if name in self.computed:
            return self.computed[name]
        return self.columns[name]

    def __len__(self) -> int:
        return len(next(iter(self.columns.values())))


# ---------------------------------------------------------------------------
# 스트리밍 계산 상태
# ---------------------------------------------------------------------------

@runtime_checkable
class StreamingState(Protocol):
    """
    증분 계산 상태. 장중 실시간에서 이벤트 1건마다 update() 가 호출된다.

    계약:
      - update() 는 반드시 O(1) 이어야 한다. 내부에서 과거 전체를 재스캔하면
        초당 1,500 이벤트를 감당하지 못한다.
        (참고: 현재 strategy.py 의 min()/max() 윈도우 재스캔은 monotonic deque 로
         바꾸면 amortized O(1) 이 된다 — ARCHITECTURE_V2.md §3.7)
      - ready 가 False 인 동안의 반환값은 신뢰할 수 없다. 전략에 노출되지 않는다.
    """

    def update(self, event: Mapping[str, Any]) -> float:
        """이벤트 1건 반영 후 현재 값 반환."""
        ...

    @property
    def ready(self) -> bool:
        """warmup 충족 여부."""
        ...


# ---------------------------------------------------------------------------
# 피처 기반 클래스
# ---------------------------------------------------------------------------

class Feature(ABC):
    """
    모든 피처의 기반. batch 와 stream 을 **반드시 함께** 구현한다.

    한쪽만 구현하고 싶은 유혹이 들 때: 그 피처는 실전에 못 올리거나
    (stream 없음), 과거 검증을 못 한다(batch 없음). 둘 다 치명적이다.
    """

    #: 피처 이름. 저장 시 parquet 컬럼명이 된다.
    name: str = ""

    #: 계산식이 바뀌면 올린다. 바뀐 채로 같은 이름을 쓰면
    #: 과거 런의 재현성이 조용히 깨진다.
    version: str = "1.0.0"

    #: 의존하는 원천 컬럼 또는 다른 피처 이름
    deps: tuple[str, ...] = ()

    #: 유효한 값이 나오기까지 필요한 이벤트/초 수
    warmup: int = 0

    #: 시간 해상도
    resolution: str = "bar_1s"

    @abstractmethod
    def batch(self, ctx: BatchContext) -> np.ndarray:
        """하루치 전체를 벡터화 계산. 장 마감 후 1회 실행."""

    @abstractmethod
    def stream(self) -> StreamingState:
        """증분 계산 상태 객체 생성. 장중 실시간 실행."""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Feature {self.name}@{self.version} warmup={self.warmup}>"


class MicroFeature(Feature):
    """
    미시 피처 — 당일 틱/초봉 기반 (cbv, obi, amt_10s ...).
    장중에 값이 계속 변한다.
    """
    resolution = "bar_1s"


class MacroFeature(Feature):
    """
    거시 피처 — 일봉 매트릭스 기반 (시총, 유통비율, 20일 평균 거래대금 ...).

    ⚠️ 시점 규칙(point-in-time): 반드시 **전일까지의 확정 데이터**만 쓴다.
       당일 일봉은 장이 끝나야 확정되므로, 당일 종가 기준 시총으로 당일 진입을
       필터링하면 그 자체가 룩어헤드다.

    하루 중 값이 변하지 않는다. 그래서
      - batch() 는 하루치 길이만큼 같은 값을 채운 배열을 돌려주고 (미시 피처와
        같은 프레임에 나란히 붙을 수 있게),
      - stream() 은 그 값을 계속 돌려주는 상수 상태를 돌려준다.
    하위 클래스는 **_daily_value() 하나만 구현하면 된다.**

    값을 뽑으려면 '어느 종목의 어느 날' 인지 알아야 한다. batch() 는 BatchContext
    에서 자동으로 그 맥락을 바인딩하고, 실시간 경로는 stream() 전에 bind() 를
    직접 불러 준다.

        feature.bind("005930", "20260911").stream()

    이 클래스가 ARCHITECTURE_V2.md §1.5 에서 진단한 '끊어진 연결선'을 잇는 지점이다.
    현재 data_loader 는 mkt/float/tradamt 를 로드만 하고 전략에 전달하지 않는다.
    """
    resolution = "daily"
    warmup = 0

    #: bind() 로 채워지는 조회 맥락
    _code: str = ""
    _date: str = ""

    def bind(self, code: str, date: str) -> "MacroFeature":
        """조회 맥락(종목·날짜)을 지정한다. batch() 는 자동으로 부른다."""
        self._code = code
        self._date = date
        return self

    def batch(self, ctx: BatchContext) -> np.ndarray:
        """하루 내내 같은 값 -> 길이만 맞춘 상수 배열."""
        self.bind(ctx.code, ctx.date)
        return np.full(len(ctx), self._daily_value(), dtype=float)

    def stream(self) -> StreamingState:
        return _ConstantState(self._daily_value())

    def _daily_value(self) -> float:
        """
        전일 확정 일봉 매트릭스에서 당일용 값 1개를 뽑는다. 하위 클래스가 구현한다.
        self._code / self._date 를 조회 맥락으로 쓴다.
        """
        raise NotImplementedError


class _ConstantState:
    """MacroFeature 용 — 하루 종일 같은 값을 돌려주는 스트리밍 상태."""

    __slots__ = ("_value",)

    def __init__(self, value: float) -> None:
        self._value = value

    def update(self, event: Mapping[str, Any]) -> float:
        return self._value

    @property
    def ready(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# 피처셋
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FeatureSet:
    """
    함께 계산·저장되는 피처들의 묶음. 저장 디렉토리 버전의 단위다.

        sampledata/features/fs_v1/20260911.parquet

    계산식이 바뀌면 fs_v1 -> fs_v2 로 디렉토리를 분기한다.
    기존 런의 재현성이 깨지지 않게 하기 위함 (ARCHITECTURE_V2.md §3.5).
    """

    version: str                        # "fs_v1"
    features: tuple[Feature, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.features)

    @property
    def max_warmup(self) -> int:
        return max((f.warmup for f in self.features), default=0)

    def manifest(self) -> dict[str, Any]:
        """_manifest.json 에 기록할 내용 — 어떤 피처가 어떤 버전으로 들어갔는가."""
        return {
            "feature_set_version": self.version,
            "features": [
                {"name": f.name, "version": f.version, "warmup": f.warmup,
                 "deps": list(f.deps), "resolution": f.resolution}
                for f in self.features
            ],
        }

    def resolve_order(self) -> list[Feature]:
        """
        deps 를 위상 정렬해 계산 순서를 정한다 (피처가 다른 피처에 의존할 수 있다).

        deps 에는 원천 컬럼명(buy_vol 등)과 피처명이 섞여 있다. 이 피처셋 안에
        있으면 피처로 보고 먼저 계산하고, 없으면 원천 컬럼으로 취급해 넘어간다.
        순환 의존은 즉시 에러다 — 조용히 무한루프를 도는 게 최악이다.
        """
        by_name = {f.name: f for f in self.features}
        order: list[Feature] = []
        state: dict[str, str] = {}

        def visit(name: str, stack: tuple[str, ...]) -> None:
            if state.get(name) == "done":
                return
            if state.get(name) == "visiting":
                cycle = " -> ".join(stack + (name,))
                raise ValueError(f"피처 의존성에 순환이 있습니다: {cycle}")
            if name not in by_name:          # 원천 컬럼 — 계산 대상이 아니다
                return
            state[name] = "visiting"
            for dep in by_name[name].deps:
                visit(dep, stack + (name,))
            state[name] = "done"
            order.append(by_name[name])

        for feature in self.features:
            visit(feature.name, ())
        return order
