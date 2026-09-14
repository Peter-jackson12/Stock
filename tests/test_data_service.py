"""
tests/test_data_service.py — 대시보드 조회 계층 검증 (ARCHITECTURE_V2.md §7.5)

대시보드가 "파일 하나를 보는 뷰어"에서 "런 비교기"로 바뀌었다. 그 전환에서
깨지기 쉬운 지점을 고정한다.

  1) 런 질의   전략/variant/날짜로 런을 고를 수 있는가
  2) 다중 런   두 런의 거래가 한 DataFrame 에 섞여도 출처가 구분되는가
  3) 컬럼 계약 화면이 기대하는 표준 이름(net_pnl_pct / mae_pct / mfe_pct /
               exit_reason)과 파생 컬럼이 나오는가
  4) signal_meta  진입 시점 스냅샷이 분석 가능한 컬럼으로 펼쳐지는가

실행:
    uv run pytest tests/test_data_service.py -v

여기서는 streamlit 을 import 하지 않는다. 조회 계층이 화면과 분리되어 있다는
것 자체가 §7.2 의 대시보드 형태(A~D)를 아직 열어둘 수 있는 근거다.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.contracts import Trade                                     # noqa: E402
from core.runstore import RunManifest, RunStore                      # noqa: E402
from dashboard.data_service import (                                 # noqa: E402
    compare_runs,
    exit_rules_of,
    filter_results,
    list_runs,
    load_results,
    load_runs,
    run_label,
    runs_to_frame,
)
from dashboard.metrics import (                                      # noqa: E402
    daily_performance,
    daily_performance_by_run,
    exit_reason_performance,
    exit_rule_performance,
    hour_performance,
    summary_metrics,
)


# ---------------------------------------------------------------------------
# 픽스처 — 두 전략의 런이 들어 있는 임시 스토어
# ---------------------------------------------------------------------------

def _trade(run_id, *, exit_rule, net, code="000270", name="기아", day=11,
           entry="090115", exit_="090130", mae=-0.3, mfe=0.5, meta=None) -> Trade:
    return Trade(
        run_id=run_id,
        strategy_id="nxt_breakout" if exit_rule != "risk_manager" else "cross_Today",
        strategy_version="1.2.0",
        exit_rule=exit_rule,
        code=code,
        name=name,
        date=date(2026, 9, day),
        entry_time=entry,
        entry_price=1000.0,
        exit_time=exit_,
        exit_price=1000.0 * (1 + net / 100),
        qty=0,
        gross_pnl_pct=net + 0.2,
        fee_pct=0.2,
        net_pnl_pct=net,
        mae_pct=mae,
        mfe_pct=mfe,
        holding_sec=15,
        exit_reason="익절" if net > 0 else "손절",
        signal_meta=meta if meta is not None else {"cbv_1": 12.5, "ctotal": 3.2},
    )


def _manifest(run_id, **kw) -> RunManifest:
    base = dict(
        run_id=run_id,
        strategy_id="nxt_breakout",
        strategy_version="1.2.0",
        param_variant="default",
        param_hash="p" + run_id,
        feature_set_version="none",
        engine="tick",
        mode="backtest",
        date_range=("20260911", "20260911"),
    )
    base.update(kw)
    return RunManifest(**base)


@pytest.fixture
def store_root(tmp_path) -> Path:
    """
    런 3개:
      tickrun  nxt_breakout / default  / 20260911 / 4건 (fixed 2 + tick_trail 2)
      barrun   cross_Today  / default  / 20220420~20220826 / 2건 (risk_manager)
      aggrrun  nxt_breakout / aggressive / 20260911 / 0건
    """
    root = tmp_path / "runs"
    store = RunStore(root)

    store.save(_manifest("tickrun"), [
        _trade("tickrun", exit_rule="fixed", net=1.0, code="053260", name="", day=11),
        _trade("tickrun", exit_rule="fixed", net=-0.5, code="053260", name="", day=12),
        _trade("tickrun", exit_rule="tick_trail", net=0.3, code="053260", name="", day=11,
               meta={"obi_top3": 1.7, "buy_ratio_15t": 0.86, "placeholders": {"mkt_float": 1000}}),
        _trade("tickrun", exit_rule="tick_trail", net=-0.2, code="053260", name="", day=12),
    ])
    store.save(_manifest("barrun", strategy_id="cross_Today", engine="bar",
                         date_range=("20220420", "20220826")), [
        _trade("barrun", exit_rule="risk_manager", net=-0.23, day=11, entry="093000", exit_="093010"),
        _trade("barrun", exit_rule="risk_manager", net=-0.23, day=12, entry="100000", exit_="100005"),
    ])
    store.save(_manifest("aggrrun", param_variant="aggressive"), [])
    return root


# ---------------------------------------------------------------------------
# 1. 런 질의
# ---------------------------------------------------------------------------

def test_list_runs_returns_newest_first(store_root):
    runs = list_runs(root=store_root)
    assert [r.run_id for r in runs] == ["aggrrun", "barrun", "tickrun"]


def test_list_runs_filters_by_strategy(store_root):
    runs = list_runs(strategy_id="cross_Today", root=store_root)
    assert [r.run_id for r in runs] == ["barrun"]


def test_list_runs_filters_by_variant(store_root):
    runs = list_runs(param_variant="aggressive", root=store_root)
    assert [r.run_id for r in runs] == ["aggrrun"]


def test_list_runs_filters_by_date_overlap(store_root):
    """런이 '다룬 날짜'와 겹치는지로 거른다. 실행 시각이 아니다."""
    assert {r.run_id for r in list_runs(since="20260101", root=store_root)} == {"tickrun", "aggrrun"}
    assert {r.run_id for r in list_runs(until="20221231", root=store_root)} == {"barrun"}


def test_runs_to_frame_uses_manifest_metrics(store_root):
    """런 목록 표는 매니페스트에 적힌 지표를 그대로 쓴다 — 화면에서 재계산하지 않는다."""
    frame = runs_to_frame(list_runs(root=store_root))
    row = frame.set_index("run_id").loc["tickrun"]
    assert row["trades"] == 4
    assert row["win_rate"] == pytest.approx(50.0)
    assert row["net_pnl_sum"] == pytest.approx(0.6)


def test_run_label_is_readable(store_root):
    label = run_label(list_runs(strategy_id="cross_Today", root=store_root)[0])
    assert "cross_Today" in label and "default" in label and "barrun" in label


def test_exit_rules_come_from_manifest(store_root):
    """사이드바 필터 후보는 parquet 을 열지 않고 매니페스트에서 나온다."""
    runs = [r for r in list_runs(root=store_root) if r.run_id == "tickrun"]
    assert exit_rules_of(runs) == ["fixed", "tick_trail"]


# ---------------------------------------------------------------------------
# 2. 거래 로딩 — 다중 런
# ---------------------------------------------------------------------------

def test_load_results_defaults_to_latest_run_with_no_keys(store_root):
    df = load_results(root=store_root)
    assert df.empty                      # 최신 런(aggrrun)은 거래 0건


def test_load_results_raises_when_store_is_empty(tmp_path):
    with pytest.raises(FileNotFoundError, match="저장된 런이 없습니다"):
        load_results(root=tmp_path / "empty")


def test_load_runs_concats_and_keeps_origin(store_root):
    """두 엔진의 결과가 한 DataFrame 에 들어가되 출처가 구분된다 — 비교의 전제."""
    df = load_runs(["tickrun", "barrun"], root=store_root)
    assert len(df) == 6
    assert set(df["storage_key"]) == {"tickrun", "barrun"}
    assert set(df["strategy_id"]) == {"nxt_breakout", "cross_Today"}
    assert df["run_label"].nunique() == 2


def test_loaded_columns_follow_standard_names(store_root):
    df = load_runs(["tickrun"], root=store_root)
    for col in ("net_pnl_pct", "mae_pct", "mfe_pct", "exit_reason", "exit_rule", "holding_sec"):
        assert col in df.columns
    # 레거시 이름은 더 이상 없다
    for col in ("pnl", "mdd", "mdu", "msg", "holding_seconds", "today"):
        assert col not in df.columns


def test_derived_columns_for_screens(store_root):
    df = load_runs(["barrun"], root=store_root)
    assert str(df["date"].dtype).startswith("datetime64")
    assert df["entry_time_text"].iloc[0] == "09:30:00"
    assert df["entry_hour"].iloc[0] == "09"


def test_load_results_filters_by_exit_rule(store_root):
    df = load_results(["tickrun"], exit_rules=["tick_trail"], root=store_root)
    assert len(df) == 2
    assert set(df["exit_rule"]) == {"tick_trail"}


# ---------------------------------------------------------------------------
# 3. signal_meta 전개
# ---------------------------------------------------------------------------

def test_signal_meta_is_expanded_into_columns(store_root):
    """진입 시점 스냅샷이 그대로 분석 변수가 된다 (§6.2)."""
    df = load_runs(["tickrun"], root=store_root)
    assert "cbv_1" in df.columns and "ctotal" in df.columns
    assert "obi_top3" in df.columns and "buy_ratio_15t" in df.columns
    assert df.loc[df["obi_top3"].notna(), "obi_top3"].iloc[0] == pytest.approx(1.7)


def test_nested_signal_meta_is_flattened_one_level(store_root):
    df = load_runs(["tickrun"], root=store_root)
    assert "placeholders.mkt_float" in df.columns


def test_signal_meta_does_not_overwrite_standard_columns(tmp_path):
    """메타 키가 표준 컬럼과 겹치면 meta_ 접두사로 피한다."""
    root = tmp_path / "runs"
    RunStore(root).save(
        _manifest("collide"),
        [_trade("collide", exit_rule="fixed", net=1.0, meta={"code": "다른값", "cbv_1": 1.0})],
    )
    df = load_runs(["collide"], root=root)
    assert df["code"].iloc[0] == "000270"        # 원본 유지
    assert df["meta_code"].iloc[0] == "다른값"    # 메타는 접두사로


# ---------------------------------------------------------------------------
# 4. 필터
# ---------------------------------------------------------------------------

def test_filter_results_by_each_condition(store_root):
    df = load_runs(["tickrun", "barrun"], root=store_root)

    assert len(filter_results(df, exit_rules=["fixed"])) == 2
    assert len(filter_results(df, run_keys=["barrun"])) == 2
    assert len(filter_results(df, exit_reasons=["익절"])) == 2
    assert len(filter_results(df, names=["기아"])) == 2
    assert len(filter_results(df, pnl_range=(0.0, 5.0))) == 2
    assert len(filter_results(df, start_date=date(2026, 9, 12))) == 3


def test_filter_results_on_empty_frame(store_root):
    empty = load_runs(["aggrrun"], root=store_root)
    assert filter_results(empty, exit_rules=["fixed"]).empty


# ---------------------------------------------------------------------------
# 5. 비교 집계
# ---------------------------------------------------------------------------

def test_compare_runs_groups_by_run(store_root):
    """run_id 로 묶어 성과를 나란히 — 이 함수가 '뷰어'와 '비교기'를 가른다."""
    df = load_runs(["tickrun", "barrun"], root=store_root)
    comparison = compare_runs(df)

    assert len(comparison) == 2
    assert set(comparison.columns) >= {"run_label", "trades", "total_pnl", "win_rate", "avg_mae_pct"}
    by_label = comparison.set_index("run_label")
    tick = by_label[by_label.index.str.contains("tickrun")].iloc[0]
    assert tick["trades"] == 4
    assert tick["total_pnl"] == pytest.approx(0.6)


def test_compare_runs_can_group_by_exit_rule(store_root):
    df = load_runs(["tickrun"], root=store_root)
    comparison = compare_runs(df, by="exit_rule")
    assert set(comparison["exit_rule"]) == {"fixed", "tick_trail"}


def test_exit_rule_performance_compares_three_cuts(store_root):
    df = load_runs(["tickrun"], root=store_root)
    perf = exit_rule_performance(df)
    assert set(perf["exit_rule"]) == {"fixed", "tick_trail"}
    assert perf["total_pnl"].sum() == pytest.approx(0.6)


def test_daily_performance_by_run_keeps_curves_separate(store_root):
    """런별 누적 곡선이 섞이지 않아야 겹쳐 그릴 수 있다."""
    df = load_runs(["tickrun", "barrun"], root=store_root)
    daily = daily_performance_by_run(df)
    assert set(daily["run_label"].unique()) == set(df["run_label"].unique())
    for _, group in daily.groupby("run_label"):
        assert group["cumulative_pnl"].iloc[-1] == pytest.approx(group["total_pnl"].sum())


# ---------------------------------------------------------------------------
# 6. 화면 집계 함수가 표준 컬럼으로 동작하는가
# ---------------------------------------------------------------------------

def test_screen_aggregations_use_standard_columns(store_root):
    df = load_runs(["tickrun", "barrun"], root=store_root)

    assert "avg_mae_pct" in daily_performance(df).columns
    assert "exit_reason" in exit_reason_performance(df).columns
    assert "avg_mae_pct" in hour_performance(df).columns

    m = summary_metrics(df)
    assert m["trades"] == 6
    assert m["avg_mae_pct"] == pytest.approx(df["mae_pct"].mean())
    assert m["total_pnl"] == pytest.approx(df["net_pnl_pct"].sum())


def test_summary_metrics_matches_manifest(store_root):
    """화면에서 계산한 값과 실행 시 매니페스트에 기록된 값이 같아야 한다."""
    manifest = RunStore(store_root).load_manifest("tickrun")
    m = summary_metrics(load_runs(["tickrun"], root=store_root))

    assert m["trades"] == manifest.metrics["trades"]
    assert m["win_rate"] == pytest.approx(manifest.metrics["win_rate"])
    assert m["total_pnl"] == pytest.approx(manifest.metrics["net_pnl_sum"])
    assert m["avg_mae_pct"] == pytest.approx(manifest.metrics["avg_mae_pct"])
