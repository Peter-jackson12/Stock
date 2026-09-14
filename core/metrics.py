"""
core/metrics.py — 성과 지표의 단일 출처 (Phase A)

**지표 계산이 두 곳에 있으면 반드시 어긋난다.**

이 모듈이 생기기 전, 성과 지표는 dashboard/metrics.py 에만 있었다. 런 스토어가
매니페스트에 요약 지표를 박아 넣으려면 같은 계산이 필요한데, 거기서 새로 짜는
순간 "대시보드가 보여주는 승률"과 "매니페스트에 기록된 승률"이 조용히 달라진다.
그래서 계산식을 여기로 옮기고, dashboard/metrics.py 는 이 모듈을 호출하는
얇은 어댑터가 되었다.

컬럼 이름은 계층마다 다르다.

    레거시 CSV (engine.py)      : pnl,          mdd,      mdu,      holding_seconds
    표준 Trade (core.contracts) : net_pnl_pct,  mae_pct,  mfe_pct,  holding_sec

이름이 다르다고 계산을 두 번 짜면 안 된다. 계산식은 하나로 두고 **컬럼 이름만
인자로 받는다.**

참고: ARCHITECTURE_V2.md §6.1, §6.2
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "summary_metrics",
    "max_drawdown_pct",
    "SUMMARY_KEYS",
]


#: summary_metrics() 가 항상 돌려주는 키 (거래가 0건이어도 동일)
SUMMARY_KEYS: tuple[str, ...] = (
    "trades", "avg_pnl", "win_rate", "total_pnl",
    "avg_mae_pct", "avg_mfe_pct", "profit_factor", "avg_holding_seconds", "tpi",
)


def _poc_tpi(avg_pnl: float, avg_mae: float) -> float:
    """POC용 TPI 대체 지표.

    원 기획안에 TPI 산식이 명시되어 있지 않아, 대시보드 비교용으로
    평균 PnL / |평균 MDD|를 사용합니다. 실제 프로젝트에서는 사용자가
    정의한 공식으로 교체해야 합니다.

    (dashboard/metrics.py 에서 그대로 옮겨옴 — 산식 변경 없음)
    """
    risk = abs(avg_mae)
    if risk <= 1e-12:
        return 0.0
    return float(avg_pnl / risk)


def _numeric(df: pd.DataFrame, column: Optional[str]) -> pd.Series:
    """없는 컬럼은 NaN 시리즈로 대체 — 엔진마다 누락 컬럼이 다를 수 있다."""
    if column is None or column not in df.columns:
        return pd.Series([np.nan] * len(df), index=df.index, dtype=float)
    return pd.to_numeric(df[column], errors="coerce")


def summary_metrics(
    df: pd.DataFrame,
    *,
    pnl_col: str = "net_pnl_pct",
    mae_col: str = "mae_pct",
    mfe_col: str = "mfe_pct",
    holding_col: str = "holding_sec",
) -> dict[str, float]:
    """
    거래 목록 -> 요약 지표.

    dashboard/metrics.py::summary_metrics 의 계산식을 그대로 옮긴 것이다.
    반환 키는 표준 Trade 이름을 따른다(avg_mae_pct / avg_mfe_pct). 화면에 찍는
    한글 라벨("평균 MDD")은 그대로 두고, 코드가 쓰는 이름만 정리한 것이다.
    """
    if df.empty:
        return {
            "trades": 0, "avg_pnl": 0.0, "win_rate": 0.0, "total_pnl": 0.0,
            "avg_mae_pct": 0.0, "avg_mfe_pct": 0.0, "profit_factor": 0.0,
            "avg_holding_seconds": 0.0, "tpi": 0.0,
        }

    pnl = _numeric(df, pnl_col).fillna(0)
    mae = _numeric(df, mae_col).fillna(0)
    wins = pnl[pnl > 0].sum()
    losses = abs(pnl[pnl < 0].sum())
    pf = wins / losses if losses > 0 else np.inf if wins > 0 else 0.0
    avg_pnl = float(pnl.mean())
    avg_mae = float(mae.mean())

    return {
        "trades": int(len(df)),
        "avg_pnl": avg_pnl,
        "win_rate": float((pnl > 0).mean() * 100),
        "total_pnl": float(pnl.sum()),
        "avg_mae_pct": avg_mae,
        "avg_mfe_pct": float(_numeric(df, mfe_col).mean()),
        "profit_factor": float(pf),
        "avg_holding_seconds": float(_numeric(df, holding_col).mean()),
        "tpi": _poc_tpi(avg_pnl, avg_mae),
    }


def max_drawdown_pct(pnl_pct: Sequence[float] | pd.Series) -> float:
    """
    거래별 손익(%)을 누적한 곡선의 최대 낙폭. 0 또는 음수를 반환한다.

    거래별 MAE(mae_pct)와는 다른 지표다.
      - mae_pct   : 한 거래가 보유 중 겪은 최대 역행폭
      - mdd_pct   : 거래를 시간순으로 누적했을 때 자본 곡선의 최대 낙폭
    RunManifest.metrics 의 "mdd_pct" 가 이것이다 (ARCHITECTURE_V2.md §6.3).

    거래별 손익을 단순 합산(비복리)한 곡선 기준이다. 지금 두 엔진 모두
    손익을 % 로만 기록하고 자본 배분(qty)을 모델링하지 않기 때문이다.
    Phase E 에서 StrategyAccount 가 붙으면 금액 기준 곡선으로 교체한다.
    """
    series = pd.to_numeric(pd.Series(list(pnl_pct), dtype="float64"), errors="coerce").fillna(0)
    if series.empty:
        return 0.0
    equity = series.cumsum()
    drawdown = equity - equity.cummax()
    return float(min(0.0, drawdown.min()))
