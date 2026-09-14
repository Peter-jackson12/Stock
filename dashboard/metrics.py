from __future__ import annotations

import numpy as np
import pandas as pd

from core.metrics import summary_metrics as _core_summary_metrics

# ---------------------------------------------------------------------------
# ⚠️ summary_metrics 의 계산식은 core/metrics.py 로 옮겼다 (Phase A).
#
# 이유: 런 스토어(core/runstore.py)가 RunManifest.metrics 에 같은 지표를 기록한다.
#       계산이 두 곳에 있으면 "대시보드가 보여주는 승률"과 "매니페스트에 적힌
#       승률"이 언젠가 반드시 어긋난다. 여기서는 레거시 CSV 컬럼 이름
#       (pnl / mdd / mdu / holding_seconds)을 표준 이름에 매핑만 한다.
#
# 참고: ARCHITECTURE_V2.md §6.1
# ---------------------------------------------------------------------------


def summary_metrics(df: pd.DataFrame) -> dict[str, float]:
    """레거시 결과 CSV 컬럼 기준 요약 지표 (계산은 core.metrics 가 담당)."""
    return _core_summary_metrics(
        df,
        pnl_col="pnl",
        mae_col="mdd",
        mfe_col="mdu",
        holding_col="holding_seconds",
    )


def daily_performance(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["date", "trade_count", "avg_pnl", "total_pnl", "win_rate", "avg_mdd"])
    daily = (
        df.groupby("date", dropna=False)
        .agg(
            trade_count=("pnl", "size"),
            avg_pnl=("pnl", "mean"),
            total_pnl=("pnl", "sum"),
            avg_mdd=("mdd", "mean"),
        )
        .reset_index()
    )
    wins = df.assign(win=(df["pnl"] > 0).astype(int)).groupby("date")["win"].mean().mul(100).reset_index(name="win_rate")
    daily = daily.merge(wins, on="date", how="left")
    daily["cumulative_pnl"] = daily["total_pnl"].cumsum()
    return daily.sort_values("date")


def exit_reason_performance(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["msg", "trade_count", "avg_pnl", "win_rate"])
    grouped = (
        df.assign(win=(df["pnl"] > 0).astype(int))
        .groupby("msg", dropna=False)
        .agg(trade_count=("pnl", "size"), avg_pnl=("pnl", "mean"), win_rate=("win", "mean"))
        .reset_index()
    )
    grouped["win_rate"] *= 100
    return grouped.sort_values("trade_count", ascending=False)


def hour_performance(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["entry_hour", "trade_count", "avg_pnl", "win_rate", "avg_mdd"])
    grouped = (
        df.assign(win=(df["pnl"] > 0).astype(int))
        .groupby("entry_hour", dropna=False)
        .agg(
            trade_count=("pnl", "size"),
            avg_pnl=("pnl", "mean"),
            win_rate=("win", "mean"),
            avg_mdd=("mdd", "mean"),
        )
        .reset_index()
    )
    grouped["win_rate"] *= 100
    return grouped.sort_values("entry_hour")
