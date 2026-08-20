from __future__ import annotations

import numpy as np
import pandas as pd


def _poc_tpi(avg_pnl: float, avg_mdd: float) -> float:
    """POC용 TPI 대체 지표.

    원 기획안에 TPI 산식이 명시되어 있지 않아, 대시보드 비교용으로
    평균 PnL / |평균 MDD|를 사용합니다. 실제 프로젝트에서는 사용자가
    정의한 공식으로 교체해야 합니다.
    """
    risk = abs(avg_mdd)
    if risk <= 1e-12:
        return 0.0
    return float(avg_pnl / risk)


def summary_metrics(df: pd.DataFrame) -> dict[str, float]:
    if df.empty:
        return {
            "trades": 0, "avg_pnl": 0.0, "win_rate": 0.0, "total_pnl": 0.0,
            "avg_mdd": 0.0, "avg_mdu": 0.0, "profit_factor": 0.0,
            "avg_holding_seconds": 0.0, "tpi": 0.0,
        }

    pnl = pd.to_numeric(df["pnl"], errors="coerce").fillna(0)
    mdd = pd.to_numeric(df["mdd"], errors="coerce").fillna(0)
    wins = pnl[pnl > 0].sum()
    losses = abs(pnl[pnl < 0].sum())
    pf = wins / losses if losses > 0 else np.inf if wins > 0 else 0.0
    avg_pnl = float(pnl.mean())
    avg_mdd = float(mdd.mean())

    return {
        "trades": int(len(df)),
        "avg_pnl": avg_pnl,
        "win_rate": float((pnl > 0).mean() * 100),
        "total_pnl": float(pnl.sum()),
        "avg_mdd": avg_mdd,
        "avg_mdu": float(pd.to_numeric(df["mdu"], errors="coerce").mean()),
        "profit_factor": float(pf),
        "avg_holding_seconds": float(pd.to_numeric(df["holding_seconds"], errors="coerce").mean()),
        "tpi": _poc_tpi(avg_pnl, avg_mdd),
    }


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
