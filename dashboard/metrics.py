from __future__ import annotations

import numpy as np
import pandas as pd

from core.metrics import summary_metrics as _core_summary_metrics

# ---------------------------------------------------------------------------
# 지표 계산식은 core/metrics.py 에 있다 (Phase A).
#
# 이유: 런 스토어(core/runstore.py)가 RunManifest.metrics 에 같은 지표를 기록한다.
#       계산이 두 곳에 있으면 "대시보드가 보여주는 승률"과 "매니페스트에 적힌
#       승률"이 언젠가 반드시 어긋난다.
#
# 컬럼 이름은 표준 Trade 계약을 그대로 쓴다 (§7.5).
#       pnl -> net_pnl_pct,  mdd -> mae_pct,  mdu -> mfe_pct,  msg -> exit_reason
# 화면에 찍는 한글 라벨("평균 MDD" 등)은 기존 표기를 유지한다.
#
# 참고: ARCHITECTURE_V2.md §6.1, §7.5
# ---------------------------------------------------------------------------

PNL = "net_pnl_pct"
MAE = "mae_pct"
MFE = "mfe_pct"
REASON = "exit_reason"


def summary_metrics(df: pd.DataFrame) -> dict[str, float]:
    """표준 Trade 컬럼 기준 요약 지표 (계산은 core.metrics 가 담당)."""
    return _core_summary_metrics(df)


def daily_performance(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["date", "trade_count", "avg_pnl", "total_pnl", "win_rate", "avg_mae_pct"])
    daily = (
        df.groupby("date", dropna=False)
        .agg(
            trade_count=(PNL, "size"),
            avg_pnl=(PNL, "mean"),
            total_pnl=(PNL, "sum"),
            avg_mae_pct=(MAE, "mean"),
        )
        .reset_index()
    )
    wins = df.assign(win=(df[PNL] > 0).astype(int)).groupby("date")["win"].mean().mul(100).reset_index(name="win_rate")
    daily = daily.merge(wins, on="date", how="left")
    daily["cumulative_pnl"] = daily["total_pnl"].cumsum()
    return daily.sort_values("date")


def daily_performance_by_run(df: pd.DataFrame, by: str = "run_label") -> pd.DataFrame:
    """
    런별 일자 성과 — 누적 PnL 곡선을 런끼리 겹쳐 그리기 위한 집계.

    '파일 뷰어'에는 없던 축이다. 같은 기간을 서로 다른 전략/청산 규칙이 어떻게
    통과했는지 한 화면에서 비교하는 것이 런 스토어를 도입한 이유다 (§6.1).
    """
    if df.empty or by not in df.columns:
        return pd.DataFrame(columns=[by, "date", "trade_count", "avg_pnl", "total_pnl", "cumulative_pnl"])

    grouped = (
        df.groupby([by, "date"], dropna=False)
        .agg(trade_count=(PNL, "size"), avg_pnl=(PNL, "mean"), total_pnl=(PNL, "sum"))
        .reset_index()
        .sort_values([by, "date"])
    )
    grouped["cumulative_pnl"] = grouped.groupby(by)["total_pnl"].cumsum()
    return grouped


def exit_reason_performance(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=[REASON, "trade_count", "avg_pnl", "win_rate"])
    grouped = (
        df.assign(win=(df[PNL] > 0).astype(int))
        .groupby(REASON, dropna=False)
        .agg(trade_count=(PNL, "size"), avg_pnl=(PNL, "mean"), win_rate=("win", "mean"))
        .reset_index()
    )
    grouped["win_rate"] *= 100
    return grouped.sort_values("trade_count", ascending=False)


def exit_rule_performance(df: pd.DataFrame) -> pd.DataFrame:
    """
    청산 규칙(exit_rule)별 성과 — 3대 트레일링 컷 비교의 화면판.

    nxt 틱 엔진은 한 번의 실행에서 fixed / tick_trail / step_trail 을 동시에
    돌린다. 지금까지 그 비교는 stdout 성적표에만 있었다 (§4.1).
    """
    if df.empty or "exit_rule" not in df.columns:
        return pd.DataFrame(columns=["exit_rule", "trade_count", "avg_pnl", "total_pnl", "win_rate", "avg_mae_pct"])
    grouped = (
        df.assign(win=(df[PNL] > 0).astype(int))
        .groupby("exit_rule", dropna=False)
        .agg(
            trade_count=(PNL, "size"),
            avg_pnl=(PNL, "mean"),
            total_pnl=(PNL, "sum"),
            win_rate=("win", "mean"),
            avg_mae_pct=(MAE, "mean"),
        )
        .reset_index()
    )
    grouped["win_rate"] *= 100
    return grouped.sort_values("total_pnl", ascending=False)


def hour_performance(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["entry_hour", "trade_count", "avg_pnl", "win_rate", "avg_mae_pct"])
    grouped = (
        df.assign(win=(df[PNL] > 0).astype(int))
        .groupby("entry_hour", dropna=False)
        .agg(
            trade_count=(PNL, "size"),
            avg_pnl=(PNL, "mean"),
            win_rate=("win", "mean"),
            avg_mae_pct=(MAE, "mean"),
        )
        .reset_index()
    )
    grouped["win_rate"] *= 100
    return grouped.sort_values("entry_hour")
