"""
dashboard/data_service.py — 런 스토어 조회 어댑터 (ARCHITECTURE_V2.md §7.5)

전:  DEFAULT_RESULT = PROJECT_ROOT / "results" / "cross_Today_1.csv"
     -> 파일 하나를 보는 뷰어. 비교 대상이 없으니 "결과 비교"라는 대시보드의
        핵심 가치가 발동하지 않았다.

후:  runs = run_store.query(strategy_id=..., since=...)
     df   = run_store.load_trades([r.storage_key for r in runs])
     -> 런 비교기. run_id 로 groupby 하면 전략 N개, 청산 규칙 N개가 한 화면에 선다.

이 모듈은 **streamlit 을 import 하지 않는다.** 조회·가공은 화면과 무관해야
테스트도 되고, §7.2 에서 대시보드 형태(A~D)가 어떻게 결정되든 그대로 재사용된다.
UI 는 dashboard/run_selector.py 가 담당한다.

컬럼 이름은 core.contracts.Trade 의 표준 이름을 그대로 쓴다.
    pnl -> net_pnl_pct,  mdd -> mae_pct,  mdu -> mfe_pct,  msg -> exit_reason
화면에 찍히는 한글 라벨("평균 MDD" 등)은 그대로 유지한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import pandas as pd

from core.runstore import RunManifest, RunStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 거래 1건을 설명하는 표준 컬럼 (core.contracts.TRADE_COLUMNS 와 같은 이름)
NUMERIC_COLUMNS = (
    "entry_price", "exit_price", "qty",
    "gross_pnl_pct", "fee_pct", "net_pnl_pct",
    "mae_pct", "mfe_pct", "holding_sec",
)


# ---------------------------------------------------------------------------
# 시간 포맷 (기존 동작 유지)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 런 조회
# ---------------------------------------------------------------------------

def get_store(root: Path | str | None = None) -> RunStore:
    return RunStore(root)


def list_runs(
    *,
    strategy_id: Optional[str] = None,
    param_variant: Optional[str] = None,
    mode: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    root: Path | str | None = None,
) -> list[RunManifest]:
    """
    조건에 맞는 런 목록 (최신 실행이 앞).

    since/until 은 런이 '다룬 날짜 범위'와 겹치는지로 판정한다 (RunStore.query).
    param_variant 는 매니페스트 필드라 스토어 질의 뒤에 거른다.
    """
    runs = get_store(root).query(strategy_id=strategy_id, mode=mode, since=since, until=until)
    if param_variant:
        runs = [r for r in runs if r.param_variant == param_variant]
    return list(reversed(runs))


def run_label(manifest: RunManifest) -> str:
    """사람이 읽는 런 이름. 셀렉트박스와 차트 범례에 쓴다."""
    start, end = (list(manifest.date_range) + ["", ""])[:2]
    period = start if start == end else f"{start}~{end}"
    trades = manifest.metrics.get("trades", "?") if manifest.metrics else "?"
    return (
        f"{manifest.strategy_id} · {manifest.param_variant} · {period} · "
        f"{trades}건 · {manifest.storage_key or manifest.run_id}"
    )


def runs_to_frame(manifests: Sequence[RunManifest]) -> pd.DataFrame:
    """런 목록을 표로. 매니페스트에 이미 요약 지표가 들어 있어 재계산하지 않는다."""
    rows = []
    for m in manifests:
        metrics = m.metrics or {}
        start, end = (list(m.date_range) + ["", ""])[:2]
        rows.append({
            "storage_key": m.storage_key or m.run_id,
            "run_id": m.run_id,
            "strategy_id": m.strategy_id,
            "version": m.strategy_version,
            "variant": m.param_variant,
            "engine": m.engine,
            "mode": m.mode,
            "date_from": start,
            "date_to": end,
            "trades": metrics.get("trades", 0),
            "win_rate": metrics.get("win_rate"),
            "net_pnl_sum": metrics.get("net_pnl_sum"),
            "mdd_pct": metrics.get("mdd_pct"),
            "created_at": m.created_at,
            "git_sha": m.git_sha,
        })
    return pd.DataFrame(rows)


def exit_rules_of(manifests: Sequence[RunManifest]) -> list[str]:
    """
    선택된 런들에 들어 있는 청산 규칙 목록.

    parquet 을 열지 않고 매니페스트의 by_exit_rule 만 본다 — 사이드바가
    거래 파일을 읽지 않고도 필터 후보를 그릴 수 있다.
    """
    rules: set[str] = set()
    for m in manifests:
        rules.update((m.metrics or {}).get("by_exit_rule", {}).keys())
    return sorted(rules)


# ---------------------------------------------------------------------------
# 거래 로딩
# ---------------------------------------------------------------------------

def _expand_signal_meta(df: pd.DataFrame) -> pd.DataFrame:
    """
    signal_meta(JSON 문자열)를 컬럼으로 펼친다.

    진입 시점 피처 스냅샷(cbv_1, ctotal, obi_top3 ...)을 그대로 분석 변수로 쓸 수
    있게 하기 위함이다. "왜 이 거래가 졌는가"를 원천 DB 재조회 없이 답한다는
    signal_meta 의 목적(§6.2)이 여기서 실현된다.

    표준 컬럼과 이름이 겹치면 meta_ 접두사를 붙여 원본을 보호한다.
    중첩 딕셔너리는 점 표기로 한 단계만 편다 (placeholders.mkt_float).
    """
    if df.empty or "signal_meta" not in df.columns:
        return df

    def _flatten(raw: Any) -> dict[str, Any]:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {}
        if not isinstance(raw, dict):
            return {}
        flat: dict[str, Any] = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                for sub, sub_value in value.items():
                    if not isinstance(sub_value, (dict, list)):
                        flat[f"{key}.{sub}"] = sub_value
            elif not isinstance(value, list):
                flat[key] = value
        return flat

    expanded = pd.DataFrame([_flatten(v) for v in df["signal_meta"]], index=df.index)
    if expanded.empty:
        return df

    expanded = expanded.rename(columns={c: f"meta_{c}" for c in expanded.columns if c in df.columns})
    return pd.concat([df, expanded], axis=1)


def load_runs(
    run_keys: Sequence[str],
    *,
    root: Path | str | None = None,
    include_reruns: bool = False,
) -> pd.DataFrame:
    """
    선택한 런들의 거래를 하나의 DataFrame 으로. 대시보드용 파생 컬럼을 붙인다.

    run_id / storage_key 컬럼이 그대로 살아 있으므로 groupby 비교가 가능하다.
    """
    store = get_store(root)
    df = store.load_trades(list(run_keys), include_reruns=include_reruns)
    if df.empty:
        return _with_derived(df)

    labels = {}
    for key in df["storage_key"].unique():
        try:
            labels[key] = run_label(store.load_manifest(key))
        except Exception:
            labels[key] = key
    df = df.copy()
    df["run_label"] = df["storage_key"].map(labels)
    return _with_derived(df)


def _with_derived(df: pd.DataFrame) -> pd.DataFrame:
    """대시보드 화면이 기대하는 파생 컬럼 (date, 시각 텍스트, 진입 시간대)."""
    if df.empty:
        empty = df.copy()
        for col in ("date", "entry_time_text", "exit_time_text", "entry_hour", "run_label"):
            if col not in empty.columns:
                empty[col] = pd.Series(dtype="object")
        return empty

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["entry_time_text"] = df["entry_time"].map(_time_to_text)
    df["exit_time_text"] = df["exit_time"].map(_time_to_text)
    df["entry_hour"] = df["entry_time_text"].str[:2].replace("", pd.NA)

    for col in NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 저장된 보유시간이 없으면 진입/청산 시각에서 되살린다 (구 버전 런 대비)
    if "holding_sec" not in df.columns or df["holding_sec"].isna().all():
        entry_sec = df["entry_time"].map(_time_to_seconds)
        exit_sec = df["exit_time"].map(_time_to_seconds)
        df["holding_sec"] = [
            max(0, e - s) if s is not None and e is not None else pd.NA
            for s, e in zip(entry_sec, exit_sec)
        ]

    df = _expand_signal_meta(df)
    return df.sort_values(["date", "entry_time"], na_position="last").reset_index(drop=True)


def load_results(
    run_keys: Optional[Sequence[str]] = None,
    *,
    exit_rules: Optional[Iterable[str]] = None,
    root: Path | str | None = None,
) -> pd.DataFrame:
    """
    대시보드가 보는 거래 목록.

    run_keys 를 주지 않으면 **가장 최근 런 하나**를 연다. 예전처럼 특정 CSV 파일에
    고정되지 않으며, 어떤 런을 볼지는 호출자(사이드바)가 정한다.

    거래가 하나도 없는 런을 선택할 수 있고, 그때는 빈 DataFrame 이 나온다.
    "조건에 맞는 거래가 없다"는 것도 결과이므로 예외로 만들지 않는다.
    """
    if run_keys is None:
        runs = list_runs(root=root)
        if not runs:
            raise FileNotFoundError(
                "저장된 런이 없습니다. 백테스트를 먼저 실행하세요.\n"
                "  uv run python engine/main.py\n"
                "  uv run python engine/nxt_tick_engine.py 005930"
            )
        run_keys = [runs[0].storage_key or runs[0].run_id]

    df = load_runs(run_keys, root=root)
    if exit_rules:
        wanted = list(exit_rules)
        df = df[df["exit_rule"].isin(wanted)].reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# 필터
# ---------------------------------------------------------------------------

def filter_results(
    df: pd.DataFrame,
    start_date=None,
    end_date=None,
    names: list[str] | None = None,
    exit_reasons: list[str] | None = None,
    pnl_range: tuple[float, float] | None = None,
    exit_rules: list[str] | None = None,
    run_keys: list[str] | None = None,
) -> pd.DataFrame:
    """공통 필터 조건을 적용합니다."""
    result = df.copy()
    if result.empty:
        return result
    if start_date is not None:
        result = result[result["date"].dt.date >= start_date]
    if end_date is not None:
        result = result[result["date"].dt.date <= end_date]
    if names:
        result = result[result["name"].astype(str).isin([str(x) for x in names])]
    if exit_reasons:
        result = result[result["exit_reason"].astype(str).isin(exit_reasons)]
    if exit_rules:
        result = result[result["exit_rule"].astype(str).isin(exit_rules)]
    if run_keys:
        result = result[result["storage_key"].astype(str).isin(run_keys)]
    if pnl_range is not None:
        result = result[result["net_pnl_pct"].between(pnl_range[0], pnl_range[1], inclusive="both")]
    return result.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 런 비교
# ---------------------------------------------------------------------------

def compare_runs(df: pd.DataFrame, by: str = "run_label") -> pd.DataFrame:
    """
    런(또는 청산 규칙)별 성과 비교표.

    이 함수 하나가 '파일 뷰어'와 '런 비교기'를 가르는 지점이다. 지표 계산은
    core.metrics 가 하므로 매니페스트에 적힌 숫자와 어긋날 일이 없다.
    """
    from core.metrics import summary_metrics      # 순환 import 방지를 위해 지역 import

    if df.empty or by not in df.columns:
        return pd.DataFrame()

    rows = []
    for key, group in df.groupby(by, dropna=False):
        m = summary_metrics(group)
        rows.append({
            by: key,
            "trades": m["trades"],
            "avg_pnl": m["avg_pnl"],
            "total_pnl": m["total_pnl"],
            "win_rate": m["win_rate"],
            "avg_mae_pct": m["avg_mae_pct"],
            "profit_factor": m["profit_factor"],
            "avg_holding_seconds": m["avg_holding_seconds"],
            "tpi": m["tpi"],
        })
    return pd.DataFrame(rows).sort_values("total_pnl", ascending=False).reset_index(drop=True)
