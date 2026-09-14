"""
tests/test_runstore.py — L5 런 스토어 검증 (Phase A)

이 테스트가 지키는 두 가지 약속:

  1) **같은 입력이면 같은 run_id** — 재현성의 정의다. run_id 가 같은데 결과가
     다르면 어딘가에 비결정성이 있다는 뜻이고, 그건 그 자체로 버그다.
  2) **지표는 한 곳에서만 계산된다** — 매니페스트에 적힌 승률과 대시보드가
     보여주는 승률이 다르면 둘 중 하나는 거짓말이다.

실행:
    uv run pytest tests/test_runstore.py -v

참고: ARCHITECTURE_V2.md §6
"""

from __future__ import annotations

import json
import sys
import warnings
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.contracts import TRADE_COLUMNS, Trade                      # noqa: E402
from core.runstore import (                                          # noqa: E402
    RunManifest,
    RunStore,
    hash_params,
    make_run_id,
    summarize,
    trades_to_frame,
)
from dashboard.metrics import summary_metrics as dashboard_summary   # noqa: E402


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------

def _trade(
    *,
    run_id: str = "run0001",
    exit_rule: str = "fixed",
    net: float = 0.5,
    mae: float = -0.3,
    mfe: float = 1.2,
    holding: int = 30,
    day: int = 11,
    meta: dict | None = None,
) -> Trade:
    return Trade(
        run_id=run_id,
        strategy_id="nxt_breakout",
        strategy_version="1.2.0",
        exit_rule=exit_rule,
        code="053260",
        name="기아",
        date=date(2026, 9, day),
        entry_time="123137",
        entry_price=9030.0,
        exit_time="123147",
        exit_price=9080.0,
        qty=0,
        gross_pnl_pct=net + 0.2,
        fee_pct=0.2,
        net_pnl_pct=net,
        mae_pct=mae,
        mfe_pct=mfe,
        holding_sec=holding,
        exit_reason="익절 (+0.8%)",
        signal_meta=meta if meta is not None else {"obi_top3": 1.73, "비고": "한글 키"},
    )


@pytest.fixture
def sample_trades() -> list[Trade]:
    """승/패가 섞이고 청산 규칙이 둘, 거래일이 이틀인 표본."""
    return [
        _trade(exit_rule="fixed", net=1.2, mae=-0.4, mfe=1.6, holding=40, day=11),
        _trade(exit_rule="fixed", net=-0.8, mae=-1.1, mfe=0.3, holding=20, day=11),
        _trade(exit_rule="tick_trail", net=0.4, mae=-0.2, mfe=0.9, holding=15, day=12),
        _trade(exit_rule="tick_trail", net=-0.3, mae=-0.7, mfe=0.1, holding=5, day=12),
    ]


@pytest.fixture
def manifest() -> RunManifest:
    return RunManifest(
        run_id="a1b2c3d4",
        strategy_id="nxt_breakout",
        strategy_version="1.2.0",
        param_variant="default",
        param_hash="7b21aaaa",
        feature_set_version="none",
        engine="tick",
        mode="backtest",
        date_range=("20260911", "20260912"),
    )


@pytest.fixture
def store(tmp_path) -> RunStore:
    return RunStore(tmp_path / "runs")


ID_KWARGS = dict(
    strategy_id="nxt_breakout",
    strategy_version="1.2.0",
    param_hash="7b21aaaa",
    feature_set_version="fs_v1",
    date_range=("20260901", "20260911"),
    git_sha="4e1a0b9",
)


# ---------------------------------------------------------------------------
# 1. 재현성 — 같은 입력이면 같은 run_id
# ---------------------------------------------------------------------------

def test_run_id_is_deterministic():
    assert make_run_id(**ID_KWARGS) == make_run_id(**ID_KWARGS)


@pytest.mark.parametrize("field,changed", [
    ("strategy_id", "other_strategy"),
    ("strategy_version", "1.3.0"),
    ("param_hash", "deadbeef"),
    ("feature_set_version", "fs_v2"),
    ("date_range", ("20260901", "20260912")),
    ("git_sha", "0000000"),
])
def test_run_id_changes_when_any_input_changes(field, changed):
    """다섯 입력 중 무엇이 바뀌어도 다른 런이어야 한다. 안 바뀌면 런이 뒤섞인다."""
    other = {**ID_KWARGS, field: changed}
    assert make_run_id(**ID_KWARGS) != make_run_id(**other)


def test_hash_params_ignores_dict_order():
    """딕셔너리 순서가 run_id 를 바꾸면 재현성 검증 자체가 성립하지 않는다."""
    assert hash_params({"a": 1, "b": {"x": 1, "y": 2}}) == hash_params({"b": {"y": 2, "x": 1}, "a": 1})


def test_hash_params_detects_value_change():
    assert hash_params({"stop_loss_pct": -0.005}) != hash_params({"stop_loss_pct": -0.006})


# ---------------------------------------------------------------------------
# 2. 저장 / 조회
# ---------------------------------------------------------------------------

def test_save_writes_manifest_and_parquet(store, manifest, sample_trades):
    key = store.save(manifest, sample_trades)
    run_dir = store.run_dir(key)

    assert key == manifest.run_id
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "trades.parquet").exists()

    saved = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert saved["run_id"] == manifest.run_id
    assert saved["storage_key"] == key
    assert saved["metrics"]["trades"] == len(sample_trades)      # 비어 있으면 자동으로 채워진다


def test_saved_columns_follow_the_contract(store, manifest, sample_trades):
    store.save(manifest, sample_trades)
    df = pd.read_parquet(store.run_dir(manifest.run_id) / "trades.parquet")
    assert list(df.columns) == list(TRADE_COLUMNS)


def test_save_does_not_overwrite_existing_run(store, manifest, sample_trades):
    """
    같은 run_id 를 다시 저장해도 기존 결과를 덮지 않는다.
    덮어쓰면 '같은 입력이 같은 결과를 냈는가'를 확인할 기회가 사라진다.
    """
    first = store.save(manifest, sample_trades)
    with pytest.warns(UserWarning):
        second = store.save(manifest, sample_trades[:1])

    assert second != first and second.startswith(first)
    assert len(pd.read_parquet(store.run_dir(first) / "trades.parquet")) == len(sample_trades)
    assert len(pd.read_parquet(store.run_dir(second) / "trades.parquet")) == 1


def test_save_on_exists_skip_and_error(store, manifest, sample_trades):
    store.save(manifest, sample_trades)

    with pytest.warns(UserWarning):
        assert store.save(manifest, [], on_exists="skip") == manifest.run_id
    assert len(pd.read_parquet(store.run_dir(manifest.run_id) / "trades.parquet")) == len(sample_trades)

    with pytest.raises(FileExistsError):
        store.save(manifest, [], on_exists="error")


def test_load_trades_does_not_double_count_reruns(store, manifest, sample_trades):
    """
    같은 입력을 두 번 돌리면 __2 가 생긴다. 그때 run_id 로 거래를 읽었더니 두 실행분이
    합쳐져 건수와 손익이 두 배가 되는 사고가 있었다. 기본값은 한 실행분만 읽는다.
    """
    store.save(manifest, sample_trades)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        store.save(manifest, sample_trades)

    df = store.load_trades(manifest.run_id)
    assert len(df) == len(sample_trades)
    assert set(df["storage_key"]) == {manifest.run_id}


def test_load_trades_can_include_reruns_on_request(store, manifest, sample_trades):
    """재현성 비교는 의도를 밝혔을 때만 — 두 실행분을 나란히 읽는다."""
    store.save(manifest, sample_trades)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        store.save(manifest, sample_trades)

    assert store.rerun_keys(manifest.run_id) == [manifest.run_id, manifest.run_id + "__2"]

    df = store.load_trades(manifest.run_id, include_reruns=True)
    assert len(df) == 2 * len(sample_trades)
    assert set(df["storage_key"]) == {manifest.run_id, manifest.run_id + "__2"}

    # 두 실행분이 같은 결과를 냈는지 = 비결정성 검사
    one, two = (g.drop(columns="storage_key").reset_index(drop=True)
                for _, g in df.groupby("storage_key"))
    assert one.equals(two)


def test_load_trades_concats_multiple_runs(store, manifest, sample_trades):
    store.save(manifest, sample_trades)

    other = replace(manifest, run_id="ffff0000", strategy_id="cross_Today", metrics={})
    store.save(other, [_trade(run_id="ffff0000", exit_rule="risk_manager")])

    df = store.load_trades([manifest.run_id, "ffff0000"])
    assert len(df) == len(sample_trades) + 1
    # 런이 섞여도 어느 런에서 왔는지 구분된다 — 전략 N개 비교의 출발점
    assert set(df["storage_key"]) == {manifest.run_id, "ffff0000"}
    assert set(df["exit_rule"]) == {"fixed", "tick_trail", "risk_manager"}


def test_signal_meta_roundtrips_through_parquet(store, manifest):
    meta = {"obi_top3": 1.73, "vol_15t": 120, "비고": "한글 키", "nested": {"a": [1, 2]}}
    store.save(manifest, [_trade(meta=meta)])

    raw = store.load_trades(manifest.run_id)
    assert isinstance(raw["signal_meta"].iloc[0], str)          # parquet 에는 문자열 1컬럼

    parsed = store.load_trades(manifest.run_id, parse_signal_meta=True)
    assert parsed["signal_meta"].iloc[0] == meta
    assert parsed["name"].iloc[0] == "기아"                      # 한글이 깨지지 않는다


def test_date_column_keeps_date_type(store, manifest, sample_trades):
    store.save(manifest, sample_trades)
    df = store.load_trades(manifest.run_id)
    assert df["date"].iloc[0] == date(2026, 9, 11)


def test_query_filters(store, manifest, sample_trades):
    store.save(manifest, sample_trades)
    other = replace(
        manifest, run_id="ffff0000", strategy_id="cross_Today",
        mode="paper", date_range=("20260801", "20260805"), metrics={},
    )
    store.save(other, sample_trades[:1])

    assert len(store.query()) == 2
    assert [m.run_id for m in store.query(strategy_id="nxt_breakout")] == [manifest.run_id]
    assert [m.run_id for m in store.query(mode="paper")] == ["ffff0000"]
    # since/until 은 '실행 시각'이 아니라 '다룬 날짜 범위'와 겹치는지로 판정한다
    assert [m.run_id for m in store.query(since="20260901")] == [manifest.run_id]
    assert [m.run_id for m in store.query(until="20260810")] == ["ffff0000"]


def test_query_on_empty_store(store):
    assert store.query() == []


def test_load_trades_warns_on_unknown_run(store):
    with pytest.warns(UserWarning):
        df = store.load_trades("nope0000")
    assert df.empty
    assert list(df.columns) == ["storage_key", *TRADE_COLUMNS]


# ---------------------------------------------------------------------------
# 3. 요약 지표 — 대시보드와 같은 숫자여야 한다
# ---------------------------------------------------------------------------

def test_summarize_matches_dashboard_metrics(sample_trades):
    """
    같은 거래 목록을 (a) 런 스토어가 summarize 한 결과와
    (b) 대시보드가 계산한 결과가 일치해야 한다.

    이게 어긋나면 "매니페스트에 적힌 승률"과 "대시보드가 보여주는 승률"이 달라진다.
    지표 계산을 core/metrics.py 한 곳에만 두는 이유다.

    §7.5 이후 대시보드도 표준 Trade 컬럼을 그대로 받는다. 예전 CSV 컬럼
    (pnl/mdd/mdu)로 옮겨 담는 과정이 사라져, 어긋날 여지가 한 겹 더 줄었다.
    """
    got = summarize(sample_trades)

    as_screen_sees_it = pd.DataFrame({
        "net_pnl_pct": [t.net_pnl_pct for t in sample_trades],
        "mae_pct": [t.mae_pct for t in sample_trades],
        "mfe_pct": [t.mfe_pct for t in sample_trades],
        "holding_sec": [t.holding_sec for t in sample_trades],
    })
    expected = dashboard_summary(as_screen_sees_it)

    assert got["trades"] == expected["trades"]
    assert got["win_rate"] == pytest.approx(expected["win_rate"])
    assert got["avg_pnl"] == pytest.approx(expected["avg_pnl"])
    assert got["net_pnl_sum"] == pytest.approx(expected["total_pnl"])
    assert got["profit_factor"] == pytest.approx(expected["profit_factor"])
    assert got["avg_mae_pct"] == pytest.approx(expected["avg_mae_pct"])
    assert got["avg_mfe_pct"] == pytest.approx(expected["avg_mfe_pct"])
    assert got["avg_holding_sec"] == pytest.approx(expected["avg_holding_seconds"])
    assert got["tpi"] == pytest.approx(expected["tpi"])


def test_summarize_counts_days_and_rate(sample_trades):
    got = summarize(sample_trades)
    assert got["days"] == 2                     # 20260911, 20260912
    assert got["trades_per_day"] == pytest.approx(2.0)


def test_summarize_splits_by_exit_rule(sample_trades):
    """한 런 안의 3대 청산 컷을 나란히 비교하는 축."""
    by_rule = summarize(sample_trades)["by_exit_rule"]
    assert set(by_rule) == {"fixed", "tick_trail"}
    assert by_rule["fixed"]["net_pnl_sum"] == pytest.approx(1.2 - 0.8)
    assert by_rule["tick_trail"]["net_pnl_sum"] == pytest.approx(0.4 - 0.3)
    assert by_rule["fixed"]["win_rate"] == pytest.approx(50.0)


def test_summarize_mdd_is_equity_curve_drawdown():
    """거래별 MAE 가 아니라 누적 손익 곡선의 낙폭이다."""
    trades = [
        _trade(net=2.0, mae=-0.1),
        _trade(net=-3.0, mae=-0.2),
        _trade(net=1.0, mae=-0.3),
    ]
    got = summarize(trades)
    assert got["mdd_pct"] == pytest.approx(-3.0)      # +2.0 -> -1.0 구간
    assert got["avg_mae_pct"] == pytest.approx(-0.2)  # 거래별 MAE 평균과는 별개


def test_summarize_on_empty_trades():
    got = summarize([])
    assert got["trades"] == 0
    assert got["net_pnl_sum"] == 0.0
    assert got["by_exit_rule"] == {}
    assert got["days"] == 0


def test_summarize_is_json_safe():
    """전승(손실 0)이면 profit factor 가 inf 다. JSON 에 inf 를 넣을 수 없다."""
    got = summarize([_trade(net=1.0), _trade(net=2.0)])
    assert got["profit_factor"] is None
    json.dumps(got)                                   # 예외가 나지 않아야 한다


# ---------------------------------------------------------------------------
# 4. 직렬화 보조
# ---------------------------------------------------------------------------

def test_trades_to_frame_keeps_contract_column_order(sample_trades):
    assert list(trades_to_frame(sample_trades).columns) == list(TRADE_COLUMNS)


def test_trades_to_frame_on_empty_list():
    frame = trades_to_frame([])
    assert frame.empty
    assert list(frame.columns) == list(TRADE_COLUMNS)


def test_manifest_json_roundtrip(manifest):
    manifest.metrics = {"trades": 3, "win_rate": 33.3}
    restored = RunManifest.from_json(manifest.to_json())
    assert restored == manifest


def test_manifest_ignores_unknown_keys(manifest):
    raw = json.loads(manifest.to_json())
    raw["future_field"] = "Phase Z"
    restored = RunManifest.from_json(json.dumps(raw))
    assert restored.run_id == manifest.run_id
