"""
tests/test_engine_feature_source.py — 엔진의 피처 출처 배선 검증 (Phase B-1)

거래 목록이 두 경로에서 같은지는 scripts/verify_engine_feature_parity.py 가
실데이터로 대조한다(8일 x 전 종목, 약 2분). 여기서는 그 대조로는 잡히지 않는
**배선 규칙**만 빠르게 고정한다.

특히 중요한 것은 폴백 금지다. 피처가 없을 때 인라인 계산으로 조용히 넘어가면,
"피처 스토어를 쓰고 있다"고 믿으면서 실제로는 안 쓰는 상태가 만들어지고 그
사실이 어디에도 남지 않는다 — ARCHITECTURE_V2.md §3.5.1 이 발견 1번으로 지목한
실패 양상 그대로다. 그래서 없으면 에러다.

실행:
    uv run pytest tests/test_engine_feature_source.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.engine import (                                            # noqa: E402
    FEATURE_SOURCE_INLINE,
    FEATURE_SOURCE_STORE,
    BackTestEngine,
    MissingFeaturesError,
)
from engine.strategy import (                                          # noqa: E402
    REQUIRED_FEATURES,
    STORE_FEATURE_KEYS,
    apply_stored_metrics,
)
from features.base import FeatureSet                                   # noqa: E402
from features.store import FeatureStore                                # noqa: E402

DATE = "20220425"
CODE = "000270"
TIMES = ["090001", "090002", "090003"]


@pytest.fixture
def feature_root(tmp_path: Path) -> Path:
    """REQUIRED_FEATURES 만 담은 하루치 파일 하나."""
    frame = pd.DataFrame({
        "code": [CODE] * len(TIMES),
        "time": TIMES,
        **{name: np.arange(len(TIMES), dtype=float) for name in REQUIRED_FEATURES},
    })
    FeatureStore(version="fs_v1", root=tmp_path).write(
        DATE, frame, FeatureSet(version="fs_v1", features=())
    )
    return tmp_path


def make_engine(feature_root: Path | None, source: str = FEATURE_SOURCE_STORE) -> BackTestEngine:
    return BackTestEngine(part=1, split=1, feature_source=source, feature_root=feature_root)


# ---------------------------------------------------------------------------
# 피처 -> 레거시 키 매핑
# ---------------------------------------------------------------------------

def test_apply_stored_metrics_maps_feature_names_to_legacy_keys():
    """전략은 여전히 stock['ctotal'] 로 읽는다. 매핑이 끊기면 조건이 조용히 거짓이 된다."""
    columns = {name: np.array([10.0, 20.0, 30.0]) for name in REQUIRED_FEATURES}
    stock: dict = {}

    apply_stored_metrics(stock, 1, columns)

    assert stock["ctotal"] == 20.0            # tick_rate_cum
    assert stock["max10_trigger"] == 20.0     # trigger_dev_max_10
    assert stock["amt_10s"] == 20.0
    assert stock["cbv_1"] == 20.0
    assert set(stock) == set(STORE_FEATURE_KEYS.values())


def test_required_features_are_declared_in_mapping():
    """읽어오는 컬럼과 전략이 쓰는 키가 1:1 이어야 한다 — 한쪽만 늘면 조용히 0 이 된다."""
    assert set(REQUIRED_FEATURES) == set(STORE_FEATURE_KEYS)


# ---------------------------------------------------------------------------
# 폴백 금지
# ---------------------------------------------------------------------------

def test_missing_feature_file_raises_instead_of_falling_back(tmp_path: Path):
    engine = make_engine(tmp_path)
    with pytest.raises(MissingFeaturesError, match="피처 파일이 없습니다"):
        engine._load_day_features("20991231", [CODE])


def test_missing_code_in_feature_file_raises(feature_root: Path):
    engine = make_engine(feature_root)
    with pytest.raises(MissingFeaturesError, match="없는 종목"):
        engine._load_day_features(DATE, [CODE, "999999"])


def test_row_misalignment_raises(feature_root: Path):
    """피처 3행 vs LOB 4행 — 한 칸 밀리면 전략이 다른 시각의 값을 본다."""
    engine = make_engine(feature_root)
    features = engine._load_day_features(DATE, [CODE])[CODE]

    lob_times = np.array(TIMES + ["090004"])
    with pytest.raises(MissingFeaturesError, match="어긋납니다"):
        engine._check_feature_alignment(CODE, DATE, lob_times, features)


def test_aligned_rows_pass(feature_root: Path):
    engine = make_engine(feature_root)
    features = engine._load_day_features(DATE, [CODE])[CODE]

    engine._check_feature_alignment(CODE, DATE, np.array(TIMES), features)


# ---------------------------------------------------------------------------
# 런 정체성
# ---------------------------------------------------------------------------

def test_inline_run_records_feature_set_none(tmp_path: Path):
    """
    인라인 런은 fs_v1 을 읽지 않았으므로 그렇게 기록하면 안 된다.
    같은 거래가 나와도 출처가 다르면 다른 런이어야 한다 (run_id 의 입력).
    """
    assert make_engine(tmp_path, FEATURE_SOURCE_INLINE).feature_set_version == "none"
    assert make_engine(tmp_path, FEATURE_SOURCE_STORE).feature_set_version == "fs_v1"


def test_inline_source_does_not_touch_the_store(tmp_path: Path):
    engine = make_engine(tmp_path, FEATURE_SOURCE_INLINE)
    assert engine.feature_store is None
    assert engine._load_day_features("20991231", [CODE]) == {}


def test_unknown_feature_source_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="feature_source"):
        make_engine(tmp_path, "maybe")
