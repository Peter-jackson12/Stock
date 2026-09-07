from __future__ import annotations

import numpy as np
import pandas as pd

from dashboard.metrics import summary_metrics, exit_reason_performance, hour_performance


def available_analysis_columns(df: pd.DataFrame) -> list[str]:
    candidates = ["entry_hour", "holding_seconds", "cbv_1", "ctotal", "cum_amt", "mdd", "mdu", "entry_price"]
    output = []
    for col in candidates:
        if col in df.columns and df[col].nunique(dropna=True) > 1:
            output.append(col)
    return output


def analyze_numeric_bins(df: pd.DataFrame, column: str, bins: int = 6) -> tuple[pd.DataFrame, str | None]:
    """수치 컬럼을 구간화하여 거래 수, 평균 PnL, 승률을 계산합니다."""
    if column not in df.columns:
        return pd.DataFrame(), f"{column} 컬럼이 없습니다."

    clean = df[[column, "pnl", "mdd"]].copy()
    clean[column] = pd.to_numeric(clean[column], errors="coerce")
    clean = clean.dropna(subset=[column, "pnl"])
    if clean[column].nunique() <= 1:
        return pd.DataFrame(), f"{column} 값이 거의 고정되어 있어 구간별 비교가 어렵습니다."

    try:
        clean["group"] = pd.qcut(clean[column], q=min(bins, clean[column].nunique()), duplicates="drop")
    except ValueError:
        clean["group"] = pd.cut(clean[column], bins=bins, duplicates="drop")

    result = (
        clean.assign(win=(clean["pnl"] > 0).astype(int))
        .groupby("group", observed=True)
        .agg(
            trade_count=("pnl", "size"),
            avg_pnl=("pnl", "mean"),
            win_rate=("win", "mean"),
            avg_mdd=("mdd", "mean"),
            min_value=(column, "min"),
            max_value=(column, "max"),
        )
        .reset_index()
    )
    result["win_rate"] *= 100
    result["group"] = result["group"].astype(str)
    return result, None


def analyze_category(df: pd.DataFrame, column: str) -> tuple[pd.DataFrame, str | None]:
    if column not in df.columns:
        return pd.DataFrame(), f"{column} 컬럼이 없습니다."
    if df[column].nunique(dropna=True) <= 1:
        return pd.DataFrame(), f"{column} 값이 거의 고정되어 있어 비교가 어렵습니다."
    result = (
        df.assign(win=(df["pnl"] > 0).astype(int))
        .groupby(column, dropna=False)
        .agg(trade_count=("pnl", "size"), avg_pnl=("pnl", "mean"), win_rate=("win", "mean"), avg_mdd=("mdd", "mean"))
        .reset_index()
    )
    result["win_rate"] *= 100
    return result, None


def generate_insights(df: pd.DataFrame) -> list[dict[str, str]]:
    """LLM 없이도 재현 가능한 규칙 기반 자동 해석을 생성합니다."""
    if df.empty:
        return [{"level": "info", "title": "분석할 거래가 없습니다", "text": "필터 범위를 넓혀 거래 데이터를 선택해 주세요."}]

    m = summary_metrics(df)
    insights: list[dict[str, str]] = []

    if m["avg_pnl"] < 0:
        insights.append({"level": "danger", "title": "평균 손익이 음수입니다", "text": f"선택된 거래의 평균 PnL은 {m['avg_pnl']:.3f}%입니다. 진입·청산 조건을 우선 재검토할 필요가 있습니다."})
    else:
        insights.append({"level": "success", "title": "평균 손익이 양수입니다", "text": f"선택된 거래의 평균 PnL은 {m['avg_pnl']:.3f}%입니다. 동일 성과가 기간별로 유지되는지 추가 확인하세요."})

    if m["win_rate"] < 30:
        insights.append({"level": "warning", "title": "승률이 낮습니다", "text": f"승률이 {m['win_rate']:.2f}%입니다. 진입 필터를 강화하거나 손익비 구조를 함께 확인하는 것이 좋습니다."})

    exit_perf = exit_reason_performance(df)
    if not exit_perf.empty:
        top = exit_perf.iloc[0]
        share = top["trade_count"] / len(df) * 100
        if share >= 70:
            insights.append({"level": "warning", "title": "특정 청산 사유가 과도하게 집중됩니다", "text": f"'{top['msg']}' 청산이 전체의 {share:.1f}%를 차지합니다. 해당 Risk Rule이 지나치게 민감한지 검토할 가치가 있습니다."})

    hour = hour_performance(df).dropna(subset=["entry_hour"])
    if len(hour) >= 2:
        best = hour.sort_values("avg_pnl", ascending=False).iloc[0]
        worst = hour.sort_values("avg_pnl", ascending=True).iloc[0]
        if best["entry_hour"] != worst["entry_hour"]:
            insights.append({"level": "info", "title": "진입 시간대별 성과 차이가 있습니다", "text": f"평균 PnL 기준 {best['entry_hour']}시가 상대적으로 가장 높고, {worst['entry_hour']}시가 가장 낮습니다. 시간 필터를 전략 조건 후보로 검토할 수 있습니다."})

    if "holding_seconds" in df.columns:
        holding = pd.to_numeric(df["holding_seconds"], errors="coerce")
        rapid = (holding <= 10).mean() * 100
        if rapid >= 40:
            insights.append({"level": "info", "title": "진입 직후 청산 비중이 높습니다", "text": f"보유시간 10초 이하 거래가 약 {rapid:.1f}%입니다. 진입 직후 Noise에 의해 청산되는지 확인해 볼 필요가 있습니다."})

    return insights


def timeframe_comparison(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """cbv_5/10/30/60 컬럼이 존재할 때 시간 단위별 성과를 비교합니다.

    현재 샘플 CSV에 없는 컬럼은 생성하지 않고 missing 목록으로 반환합니다.
    """
    frames = [5, 10, 30, 60]
    rows = []
    missing = []
    for sec in frames:
        col = f"cbv_{sec}"
        if col not in df.columns or pd.to_numeric(df[col], errors="coerce").nunique(dropna=True) <= 1:
            missing.append(col)
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        threshold = s.median()
        subset = df[s >= threshold]
        m = summary_metrics(subset)
        rows.append({
            "timeframe": f"{sec}초",
            "column": col,
            "threshold": threshold,
            "trade_count": m["trades"],
            "avg_pnl": m["avg_pnl"],
            "win_rate": m["win_rate"],
            "avg_mdd": m["avg_mdd"],
            "tpi": m["tpi"],
        })
    return pd.DataFrame(rows), missing


def compare_poc_strategies(df: pd.DataFrame, cbv_percentile: int = 50, latest_entry_hour: int = 10) -> pd.DataFrame:
    """현재 결과 CSV만으로 시연 가능한 단일/복합 조건 전략을 비교합니다.

    미래 정보를 쓰는 MDD/MDU/보유시간은 진입 조건에서 제외합니다.
    - Baseline: 전체 거래
    - CBV 조건: cbv_1 상위 percentile
    - 시간 조건: 지정 시각 이전 진입
    - 복합 조건: 위 두 조건 동시 만족
    """
    if df.empty:
        return pd.DataFrame()

    candidates: list[tuple[str, pd.DataFrame, str]] = [("Baseline", df, "전체 거래")]

    cbv_mask = None
    if "cbv_1" in df.columns:
        cbv = pd.to_numeric(df["cbv_1"], errors="coerce")
        if cbv.nunique(dropna=True) > 1:
            q = max(0, min(100, int(cbv_percentile))) / 100
            threshold = float(cbv.quantile(q))
            cbv_mask = cbv >= threshold
            candidates.append((f"CBV 단일", df[cbv_mask], f"cbv_1 ≥ {threshold:.3f} (상위 {100-cbv_percentile}% 기준)"))

    hour_num = pd.to_numeric(df.get("entry_hour"), errors="coerce")
    time_mask = hour_num <= latest_entry_hour
    if time_mask.notna().any():
        candidates.append(("시간 단일", df[time_mask.fillna(False)], f"진입시간 ≤ {latest_entry_hour:02d}:59"))

    if cbv_mask is not None and time_mask.notna().any():
        candidates.append(("복합 조건", df[cbv_mask & time_mask.fillna(False)], f"CBV 조건 + 진입시간 ≤ {latest_entry_hour:02d}:59"))

    rows = []
    for name, subset, condition in candidates:
        m = summary_metrics(subset)
        rows.append({
            "strategy": name,
            "condition": condition,
            "trade_count": m["trades"],
            "avg_pnl": m["avg_pnl"],
            "total_pnl": m["total_pnl"],
            "win_rate": m["win_rate"],
            "avg_mdd": m["avg_mdd"],
            "profit_factor": m["profit_factor"],
            "tpi": m["tpi"],
        })
    return pd.DataFrame(rows)


def recommend_strategy(comparison: pd.DataFrame) -> dict[str, object] | None:
    if comparison is None or comparison.empty:
        return None
    usable = comparison[comparison["trade_count"] > 0].copy()
    if usable.empty:
        return None
    # POC에서는 TPI 우선, 동률이면 평균 PnL과 거래 수 순으로 선정합니다.
    best = usable.sort_values(["tpi", "avg_pnl", "trade_count"], ascending=[False, False, False]).iloc[0]
    return best.to_dict()
