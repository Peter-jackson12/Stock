from __future__ import annotations

from pathlib import Path
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULT = PROJECT_ROOT / "results" / "cross_Today_1.csv"


def _time_to_text(value) -> str:
    """90045 -> 09:00:45 형식으로 변환합니다."""
    if pd.isna(value):
        return ""
    try:
        raw = str(int(float(value))).zfill(6)
        return f"{raw[:2]}:{raw[2:4]}:{raw[4:6]}"
    except (TypeError, ValueError):
        return str(value)


def _time_to_seconds(value) -> float | None:
    try:
        raw = str(int(float(value))).zfill(6)
        h, m, s = int(raw[:2]), int(raw[2:4]), int(raw[4:6])
        return h * 3600 + m * 60 + s
    except (TypeError, ValueError):
        return None


def load_results(path: str | Path | None = None) -> pd.DataFrame:
    """백테스트 결과 CSV를 읽고 대시보드용 파생 컬럼을 생성합니다."""
    csv_path = Path(path) if path else DEFAULT_RESULT
    if not csv_path.exists():
        raise FileNotFoundError(f"결과 파일을 찾을 수 없습니다: {csv_path}")

    df = pd.read_csv(csv_path, low_memory=False)
    required = {"today", "name", "entry_time", "exit_time", "pnl", "mdd", "mdu", "msg"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"대시보드 필수 컬럼이 없습니다: {', '.join(sorted(missing))}")

    df = df.copy()
    df["date"] = pd.to_datetime(df["today"].astype(str), format="%Y%m%d", errors="coerce")
    df["entry_time_text"] = df["entry_time"].map(_time_to_text)
    df["exit_time_text"] = df["exit_time"].map(_time_to_text)
    df["entry_hour"] = df["entry_time_text"].str[:2].replace("", pd.NA)

    entry_sec = df["entry_time"].map(_time_to_seconds)
    exit_sec = df["exit_time"].map(_time_to_seconds)
    df["holding_seconds"] = [
        max(0, e - s) if s is not None and e is not None else pd.NA
        for s, e in zip(entry_sec, exit_sec)
    ]

    for col in ["pnl", "mdd", "mdu", "entry_price", "exit_price", "cbv_1", "ctotal", "cum_amt"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df.sort_values(["date", "entry_time"], na_position="last").reset_index(drop=True)


def filter_results(
    df: pd.DataFrame,
    start_date=None,
    end_date=None,
    names: list[str] | None = None,
    exit_reasons: list[str] | None = None,
    pnl_range: tuple[float, float] | None = None,
) -> pd.DataFrame:
    """공통 필터 조건을 적용합니다."""
    result = df.copy()
    if start_date is not None:
        result = result[result["date"].dt.date >= start_date]
    if end_date is not None:
        result = result[result["date"].dt.date <= end_date]
    if names:
        result = result[result["name"].astype(str).isin([str(x) for x in names])]
    if exit_reasons:
        result = result[result["msg"].astype(str).isin(exit_reasons)]
    if pnl_range is not None:
        result = result[result["pnl"].between(pnl_range[0], pnl_range[1], inclusive="both")]
    return result.reset_index(drop=True)
