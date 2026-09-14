"""
tests/test_macro_policy.py — 거시 피처 null 정책과 커버리지 게이트 (§3.6.1 / §3.6.2)

ARCHITECTURE_V2.md §3.6.1 이 지목한 위험은 결손 자체가 아니라 **결손 시 동작이
정의되어 있지 않다**는 것이었다. 정의되지 않은 채 filters.macro.enabled 를 켜면
유니버스의 98.5%가 조용히 사라지거나 필터가 조용히 무력화되고, 둘 다 백테스트는
정상 종료된다. 그 두 갈래가 이제 파라미터로 갈라지는지, 그리고 어느 쪽이든
'왜 그렇게 판정됐는지'가 남는지를 확인한다.

실행:
    uv run pytest tests/test_macro_policy.py -v
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

from features import registry                                          # noqa: E402
from features.base import FeatureSet                                   # noqa: E402
from features.builders.macro import MarketCap                          # noqa: E402
from features.builders.microstructure import (                         # noqa: E402
    OrderBookImbalance,
    RecentBuyRatio,
    RollingBuyVolMean,
)
from features.store import FeatureStore                                # noqa: E402
from scripts.build_features import measure_coverage, report_coverage   # noqa: E402
from strategies.macro_filter import (                                  # noqa: E402
    ON_MISSING_REJECT,
    ON_MISSING_SKIP_FILTER,
    MacroFilterConfig,
    evaluate_macro_filter,
)

FULL_PARAMS = {
    "enabled": True,
    "on_missing": ON_MISSING_REJECT,
    "mkt_cap_min_eok": 500,
    "mkt_cap_max_eok": 30000,
    "float_ratio_max_pct": 40.0,
    "ytd_tradamt_min_eok": 100,
}

PASSING = {"mkt_cap": 5000.0, "float_ratio": 30.0, "ytd_tradamt_20": 400.0}


def config(**overrides) -> MacroFilterConfig:
    return MacroFilterConfig.from_params({**FULL_PARAMS, **overrides})


# ---------------------------------------------------------------------------
# null 정책
# ---------------------------------------------------------------------------

def test_reject_blocks_entry_when_macro_value_is_missing():
    """보수적 정책 — 모르는 종목에는 돈을 넣지 않는다."""
    decision = evaluate_macro_filter({**PASSING, "mkt_cap": float("nan")}, config())

    assert decision.allowed is False
    assert any("mkt_cap 값 없음" in r for r in decision.reasons)


def test_skip_filter_passes_but_records_what_was_not_checked():
    """
    관대한 정책 — 통과시키되 '판정하지 못했다'를 남긴다.
    이 기록이 없으면 필터가 꺼진 채 켜져 있다고 착각하게 된다.
    """
    decision = evaluate_macro_filter(
        {**PASSING, "mkt_cap": float("nan")},
        config(on_missing=ON_MISSING_SKIP_FILTER),
    )

    assert decision.allowed is True
    assert decision.evaluated_fully is False
    assert any("mkt_cap" in s for s in decision.skipped)


def test_fully_evaluated_pass_has_nothing_skipped():
    decision = evaluate_macro_filter(PASSING, config())

    assert decision.allowed is True
    assert decision.evaluated_fully is True
    assert decision.skipped == ()


def test_missing_key_is_treated_like_null():
    """피처 이름 자체가 없는 경우도 결손이다 (조용한 통과가 최악)."""
    decision = evaluate_macro_filter({"float_ratio": 30.0}, config())

    assert decision.allowed is False


def test_zero_is_a_value_not_a_missing():
    """0.0 은 결손이 아니다 — 시총 0 은 '모른다'가 아니라 '조건 미달'이다."""
    decision = evaluate_macro_filter({**PASSING, "mkt_cap": 0.0}, config())

    assert decision.allowed is False
    assert any("mkt_cap=0" in r for r in decision.reasons)
    assert decision.skipped == ()


def test_thresholds_reject_out_of_range_values():
    too_big = evaluate_macro_filter({**PASSING, "mkt_cap": 50000.0}, config())
    too_illiquid = evaluate_macro_filter({**PASSING, "ytd_tradamt_20": 10.0}, config())

    assert too_big.allowed is False
    assert too_illiquid.allowed is False


def test_disabled_filter_passes_without_marking_anything_skipped():
    """'적용할 필터가 없는 것'과 '적용하지 못한 것'은 다르다."""
    decision = evaluate_macro_filter({}, config(enabled=False))

    assert decision.allowed is True
    assert decision.evaluated_fully is True


def test_unknown_policy_is_rejected_at_config_time():
    """오타가 조용히 기본값으로 흡수되면 정책을 명시한 의미가 없다."""
    with pytest.raises(ValueError, match="on_missing"):
        config(on_missing="ignore")


def test_default_policy_is_reject():
    assert MacroFilterConfig.from_params({"enabled": True}).on_missing == ON_MISSING_REJECT


def test_tick_engine_params_carry_the_policy_into_the_manifest():
    """정책이 param_hash 를 통해 런 매니페스트에 기록되어야 한다 (§3.6.1)."""
    from engine.nxt_tick_engine import PARAMS

    assert PARAMS["macro"]["on_missing"] in (ON_MISSING_REJECT, ON_MISSING_SKIP_FILTER)
    MacroFilterConfig.from_params(PARAMS["macro"])      # 검증을 통과해야 한다


# ---------------------------------------------------------------------------
# 커버리지 게이트
# ---------------------------------------------------------------------------

def make_day(n_codes: int, codes_with_macro: int, rows: int = 10) -> pd.DataFrame:
    frames = []
    for i in range(n_codes):
        value = 1000.0 if i < codes_with_macro else np.nan
        frames.append(pd.DataFrame({
            "code": [f"{i:06d}"] * rows,
            "time": [f"0900{j:02d}" for j in range(rows)],
            "cbv_5": np.arange(rows, dtype=float),
            "mkt_cap": [value] * rows,
        }))
    return pd.concat(frames, ignore_index=True)


def test_coverage_measures_codes_not_just_rows():
    """거시 피처는 종목당 값이 하나다. 판단 기준은 종목 커버리지다."""
    features = [RollingBuyVolMean(5), MarketCap()]
    coverage = measure_coverage(make_day(200, 3), features)

    assert coverage["total_codes"] == 200
    assert coverage["macro_features"] == ["mkt_cap"]
    assert coverage["per_feature"]["mkt_cap"]["codes_with_value"] == 3
    assert coverage["per_feature"]["mkt_cap"]["code_coverage_pct"] == pytest.approx(1.5)
    assert coverage["min_code_coverage_pct"] == pytest.approx(1.5)


def test_coverage_ignores_micro_features():
    """cbv_5 는 거시 피처가 아니므로 게이트 대상이 아니다."""
    coverage = measure_coverage(make_day(10, 10), [RollingBuyVolMean(5)])

    assert coverage["macro_features"] == []
    assert coverage["per_feature"] == {}


def test_report_fails_below_threshold_and_passes_above(capsys):
    features = [MarketCap()]
    low = measure_coverage(make_day(200, 3), features)
    full = measure_coverage(make_day(200, 200), features)

    assert report_coverage("20220425", low, 95.0, strict=False) is False
    assert report_coverage("20220425", full, 95.0, strict=False) is True
    assert "1.50%" in capsys.readouterr().out


def test_coverage_is_recorded_in_the_manifest(tmp_path: Path):
    """
    '그때 그 파일의 커버리지가 얼마였나'를 나중에 답할 수 있어야 한다.
    지금은 파일 어디에도 그 사실이 적혀 있지 않다는 것이 §3.6.1 의 지적이었다.
    """
    store = FeatureStore(version="fs_v1", root=tmp_path)
    frame = make_day(4, 1, rows=3)
    store.write("20220425", frame, FeatureSet(version="fs_v1", features=(MarketCap(),)))

    coverage = measure_coverage(frame, [MarketCap()])
    store.record_coverage("20220425", coverage)

    saved = store.read_manifest()["coverage"]["20220425"]
    assert saved["per_feature"]["mkt_cap"]["codes_with_value"] == 1
    assert saved["min_code_coverage_pct"] == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# 해상도 표기 (§3.6.2)
# ---------------------------------------------------------------------------

def test_manifest_resolution_matches_what_is_actually_stored():
    """
    obi_top3 / buy_ratio_15t 는 틱 지표지만 fs_v1 에는 1초봉 행으로 저장된다.
    매니페스트가 "tick" 이라고 적어 두면 문서가 거짓말을 하는 상태가 된다 (§3.6.2).
    """
    fs = FeatureSet(version="fs_v1", features=(OrderBookImbalance(), RecentBuyRatio(15), MarketCap()))
    entry = {f["name"]: f for f in fs.manifest()["features"]}

    assert entry["obi_top3"]["resolution"] == "bar_1s"
    assert entry["obi_top3"]["native_resolution"] == "tick"
    assert entry["buy_ratio_15t"]["resolution"] == "bar_1s"
    assert entry["mkt_cap"]["resolution"] == "bar_1s"
    assert entry["mkt_cap"]["native_resolution"] == "daily"


def test_every_stored_feature_declares_bar_1s():
    """fs_v1 은 1초봉 프레임 하나에 전부 붙인다 — 예외가 있으면 저장이 거짓말이다."""
    registry.reset()
    features = registry.bootstrap().values()
    try:
        assert {f.resolution for f in features} == {"bar_1s"}
    finally:
        registry.reset()
