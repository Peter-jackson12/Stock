"""
tests/test_feature_store.py — L2 피처 스토어와 레지스트리 검증 (Phase B)

피처 스토어의 존재 이유는 두 가지다.

  1) **컬럼 선택 읽기** — 60개 컬럼 중 3개만 쓸 때 그 3개만 I/O 한다.
     파라미터 스윕이 피처 재계산 없이 도는 근거이자, L2 만 parquet 을 쓰는 이유다.
  2) **버전 격리** — 계산식이 바뀌면 fs_v2 로 분기한다. 같은 디렉토리에 다른
     계산식이 섞이면 과거 백테스트의 재현성이 조용히 깨진다.

둘 다 '되는지' 를 말이 아니라 파일에서 확인한다.

실행:
    uv run pytest tests/test_feature_store.py -v

참고: ARCHITECTURE_V2.md §3.5
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from features import registry                                        # noqa: E402
from features.base import BatchContext, Feature, FeatureSet          # noqa: E402
from features.builders.microstructure import (                       # noqa: E402
    OrderBookImbalance,
    RollingBuyVolMean,
)
from features.store import KEY_COLUMNS, FeatureStore                 # noqa: E402


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------

FEATURE_NAMES = [f"f{i:02d}" for i in range(30)]


@pytest.fixture
def feature_set() -> FeatureSet:
    return FeatureSet(version="fs_v1", features=tuple(RollingBuyVolMean(w) for w in (5, 10)))


@pytest.fixture
def day_frame() -> pd.DataFrame:
    """종목 3개 x 500행 x 피처 30개 — 컬럼 선택 이점이 드러날 정도의 크기."""
    rng = np.random.default_rng(20220425)
    frames = []
    for code in ("000270", "005930", "035720"):
        n = 500
        data = {"code": code, "time": [f"09{m:02d}{s:02d}" for m, s in
                                       zip(np.repeat(np.arange(n // 60 + 1), 60)[:n],
                                           np.tile(np.arange(60), n // 60 + 1)[:n])]}
        for name in FEATURE_NAMES:
            data[name] = rng.normal(size=n)
        frames.append(pd.DataFrame(data))
    return pd.concat(frames, ignore_index=True)


@pytest.fixture
def store(tmp_path) -> FeatureStore:
    return FeatureStore(version="fs_v1", root=tmp_path / "features")


# ---------------------------------------------------------------------------
# 1. 쓰기 — 정렬, row group, 압축
# ---------------------------------------------------------------------------

def test_write_creates_file_and_manifest(store, day_frame, feature_set):
    path = store.write("20220425", day_frame, feature_set)
    assert path.exists()
    assert store.manifest_path.exists()
    assert store.available_dates() == ["20220425"]

    manifest = store.read_manifest()
    assert manifest["feature_set_version"] == "fs_v1"
    assert {f["name"] for f in manifest["features"]} == {"cbv_5", "cbv_10"}


def test_written_rows_are_sorted_by_code_and_time(store, day_frame, feature_set):
    shuffled = day_frame.sample(frac=1.0, random_state=1).reset_index(drop=True)
    store.write("20220425", shuffled, feature_set)

    out = store.read("20220425")
    assert out[["code", "time"]].equals(
        out[["code", "time"]].sort_values(["code", "time"], kind="stable").reset_index(drop=True)
    )


def test_one_row_group_per_code(store, day_frame, feature_set):
    """
    종목 단위 row group 이라야 특정 종목만 읽을 때 그 구간만 건드린다.
    (predicate pushdown 이 걸리는 물리적 근거)
    """
    store.write("20220425", day_frame, feature_set)
    meta = pq.ParquetFile(store.path_for("20220425")).metadata
    assert meta.num_row_groups == day_frame["code"].nunique()


def test_written_file_is_zstd_compressed(store, day_frame, feature_set):
    store.write("20220425", day_frame, feature_set)
    meta = pq.ParquetFile(store.path_for("20220425")).metadata
    compressions = {
        meta.row_group(0).column(i).compression for i in range(meta.row_group(0).num_columns)
    }
    assert compressions == {"ZSTD"}


def test_write_rejects_frame_without_key_columns(store, feature_set):
    with pytest.raises(ValueError, match="키 컬럼"):
        store.write("20220425", pd.DataFrame({"cbv_5": [1.0]}), feature_set)


def test_write_rejects_empty_frame(store, feature_set):
    with pytest.raises(ValueError, match="기록할 피처가 없습니다"):
        store.write("20220425", pd.DataFrame(), feature_set)


# ---------------------------------------------------------------------------
# 2. 컬럼 선택 읽기 — 이 계층의 핵심 이점
# ---------------------------------------------------------------------------

def test_read_returns_only_requested_columns(store, day_frame, feature_set):
    store.write("20220425", day_frame, feature_set)
    out = store.read("20220425", names=["f00", "f01", "f02"])
    assert list(out.columns) == [*KEY_COLUMNS, "f00", "f01", "f02"]


def test_read_always_includes_key_columns(store, day_frame, feature_set):
    """키가 빠지면 어느 종목의 몇 시 값인지 알 수 없다 — 쓸모없는 숫자가 된다."""
    store.write("20220425", day_frame, feature_set)
    out = store.read("20220425", names=["f00"])
    for key in KEY_COLUMNS:
        assert key in out.columns


def test_column_selection_reads_a_fraction_of_the_file(store, day_frame, feature_set):
    """
    말이 아니라 파일에서 확인한다. 32개 컬럼 중 3개만 요청하면 그 3개의 물리
    크기만큼만 읽으면 된다. SQLite 였다면 행 전체를 읽고 나머지를 버렸을 것이다.
    """
    store.write("20220425", day_frame, feature_set)
    stats = store.file_stats("20220425")

    wanted = ["f00", "f01", "f02"]
    selected_bytes = sum(stats["column_bytes"][c] for c in [*KEY_COLUMNS, *wanted])
    total_bytes = sum(stats["column_bytes"].values())

    assert selected_bytes < total_bytes * 0.25, (
        f"컬럼 선택 이점이 없다: {selected_bytes}/{total_bytes} 바이트"
    )


def test_read_rejects_unknown_feature_name(store, day_frame, feature_set):
    """오타를 조용히 넘기면 '그 피처가 없는 백테스트'가 조용히 돌아간다."""
    store.write("20220425", day_frame, feature_set)
    with pytest.raises(KeyError, match="없는 피처"):
        store.read("20220425", names=["f00", "typo_feature"])


def test_read_filters_by_code(store, day_frame, feature_set):
    store.write("20220425", day_frame, feature_set)
    out = store.read("20220425", codes=["005930"])
    assert set(out["code"]) == {"005930"}
    assert len(out) == (day_frame["code"] == "005930").sum()


def test_read_missing_date_is_explicit(store):
    with pytest.raises(FileNotFoundError, match="피처 파일이 없습니다"):
        store.read("19990101")


def test_columns_of_reads_schema_only(store, day_frame, feature_set):
    store.write("20220425", day_frame, feature_set)
    columns = store.columns_of("20220425")
    assert columns[:2] == list(KEY_COLUMNS)
    assert "f29" in columns


# ---------------------------------------------------------------------------
# 3. 날짜 범위 읽기 — 파일당 하루라서 범위 = 파일 목록
# ---------------------------------------------------------------------------

def test_read_range_concats_days_with_date_column(store, day_frame, feature_set):
    for date in ("20220425", "20220426", "20220427"):
        store.write(date, day_frame, feature_set)

    out = store.read_range("20220425", "20220426", names=["f00"])
    assert set(out["date"]) == {"20220425", "20220426"}
    assert list(out.columns) == ["date", *KEY_COLUMNS, "f00"]
    assert len(out) == 2 * len(day_frame)


def test_read_range_on_empty_window(store, day_frame, feature_set):
    store.write("20220425", day_frame, feature_set)
    out = store.read_range("20230101", "20230131")
    assert out.empty


# ---------------------------------------------------------------------------
# 4. 버전 격리 — 같은 디렉토리에 다른 계산식이 섞이면 재현성이 깨진다
# ---------------------------------------------------------------------------

def test_write_rejects_same_name_with_different_version(store, day_frame, feature_set):
    store.write("20220425", day_frame, feature_set)

    changed = RollingBuyVolMean(5)
    changed.version = "2.0.0"                     # 계산식이 바뀐 상황
    conflicting = FeatureSet(version="fs_v1", features=(changed,))

    with pytest.raises(ValueError, match="fs_v2 로 분기"):
        store.write("20220426", day_frame, conflicting)


def test_write_rejects_mismatched_feature_set_version(store, day_frame, feature_set):
    other = FeatureSet(version="fs_v2", features=feature_set.features)
    with pytest.raises(ValueError, match="피처셋 버전이 다릅니다"):
        store.write("20220425", day_frame, feature_set)
        store.write("20220426", day_frame, other)


def test_versions_are_isolated_by_directory(tmp_path, day_frame, feature_set):
    """fs_v1 과 fs_v2 는 서로를 모른다 — 과거 런의 재현성이 유지되는 구조."""
    v1 = FeatureStore(version="fs_v1", root=tmp_path / "features")
    v2 = FeatureStore(version="fs_v2", root=tmp_path / "features")

    v1.write("20220425", day_frame, feature_set)
    v2.write("20220425", day_frame, FeatureSet(version="fs_v2", features=feature_set.features))

    assert v1.path_for("20220425") != v2.path_for("20220425")
    assert v1.read_manifest()["feature_set_version"] == "fs_v1"
    assert v2.read_manifest()["feature_set_version"] == "fs_v2"


# ---------------------------------------------------------------------------
# 5. 레지스트리 — 자기 기술(self-describing) 구조
# ---------------------------------------------------------------------------

def test_bootstrap_registers_every_builder_feature():
    features = registry.bootstrap()
    for name in ("cbv_10", "cbv_ratio_max_10", "trigger_dev_min_10", "amt_10s",
                 "tick_rate_cum", "obi_top3", "mkt_cap", "upper_limit"):
        assert name in features


def test_bootstrap_is_idempotent():
    first = registry.bootstrap()
    second = registry.bootstrap()
    assert set(first) == set(second)


def test_duplicate_registration_is_rejected():
    registry.bootstrap()
    with pytest.raises(ValueError, match="이름 중복"):
        registry.register(RollingBuyVolMean(10))


def test_get_unknown_feature_lists_what_exists():
    registry.bootstrap()
    with pytest.raises(KeyError, match="등록되지 않은 피처"):
        registry.get("cbv_999")


def test_resolve_deps_returns_requested_features():
    registry.bootstrap()
    order = registry.resolve_deps(["amt_10s", "cbv_10"])
    assert [f.name for f in order] == ["amt_10s", "cbv_10"]


def test_resolve_deps_orders_dependencies_first():
    """피처가 다른 피처에 의존하면 의존 대상이 먼저 계산되어야 한다."""

    class Derived(Feature):
        name = "derived"
        deps = ("base_feature",)

        def batch(self, ctx: BatchContext) -> np.ndarray:
            return ctx.col("base_feature") * 2

        def stream(self):
            raise NotImplementedError

    class Base(Feature):
        name = "base_feature"
        deps = ("close",)

        def batch(self, ctx: BatchContext) -> np.ndarray:
            return np.asarray(ctx.col("close"))

        def stream(self):
            raise NotImplementedError

    fs = FeatureSet(version="fs_test", features=(Derived(), Base()))
    assert [f.name for f in fs.resolve_order()] == ["base_feature", "derived"]


def test_cyclic_dependency_is_reported_not_looped():
    """순환 의존으로 조용히 무한루프 도는 게 최악이다."""

    class A(Feature):
        name = "cycle_a"
        deps = ("cycle_b",)

        def batch(self, ctx):
            raise NotImplementedError

        def stream(self):
            raise NotImplementedError

    class B(Feature):
        name = "cycle_b"
        deps = ("cycle_a",)

        def batch(self, ctx):
            raise NotImplementedError

        def stream(self):
            raise NotImplementedError

    fs = FeatureSet(version="fs_test", features=(A(), B()))
    with pytest.raises(ValueError, match="순환"):
        fs.resolve_order()


def test_feature_set_manifest_is_self_describing():
    """
    전략이 required_features 를 선언하면 매니페스트가 '무엇을 어떤 버전으로
    계산했는지' 를 남긴다. 이 자기 기술성이 문서보다 강하다 (§3.1).
    """
    fs = FeatureSet(version="fs_v1", features=(RollingBuyVolMean(10), OrderBookImbalance()))
    manifest = fs.manifest()

    assert manifest["feature_set_version"] == "fs_v1"
    entry = {f["name"]: f for f in manifest["features"]}
    assert entry["cbv_10"]["warmup"] == 11
    assert entry["cbv_10"]["version"] == "1.0.0"
    assert entry["obi_top3"]["resolution"] == "tick"
    assert fs.max_warmup == 11
