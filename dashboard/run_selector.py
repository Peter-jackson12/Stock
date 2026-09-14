"""
dashboard/run_selector.py — 런 선택 사이드바 (ARCHITECTURE_V2.md §7.5)

대시보드가 "파일 하나를 보는 뷰어"에서 "런 비교기"로 바뀌면서 필요해진 단 하나의
새 화면 요소다. 어떤 결과를 볼지 고르는 곳.

    전략 -> 파라미터 variant -> 날짜 범위 -> 런(복수 선택) -> 청산 규칙

선택 결과는 st.session_state 에 남아 페이지를 옮겨도 유지된다.

⚠️ 대시보드 구조는 건드리지 않는다. §7.2 의 옵션 A~D 가 아직 미정이므로,
   여기서 하는 일은 "무엇을 로드할지 고르는 것"까지다. 페이지 구성·라우팅·
   실전 감시 분리는 Phase F 의 일이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import streamlit as st

from core.runstore import RunManifest
from dashboard.data_service import (
    exit_rules_of,
    list_runs,
    load_results,
    run_label,
    runs_to_frame,
)

SESSION_KEY = "selected_run_keys"
SESSION_RULES = "selected_exit_rules"


@dataclass
class RunSelection:
    """사이드바가 고른 것. 페이지는 이걸 들고 데이터를 읽는다."""

    keys: list[str] = field(default_factory=list)
    manifests: list[RunManifest] = field(default_factory=list)
    exit_rules: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.keys

    @property
    def is_multi(self) -> bool:
        return len(self.keys) > 1

    def summary_line(self) -> str:
        if self.is_empty:
            return "선택된 런이 없습니다."
        head = f"런 {len(self.keys)}개 선택"
        if self.exit_rules:
            head += f" · 청산 규칙 {', '.join(self.exit_rules)}"
        return head

    def frame(self) -> pd.DataFrame:
        return runs_to_frame(self.manifests)


@st.cache_data(show_spinner=False)
def _cached_trades(keys: tuple[str, ...], exit_rules: tuple[str, ...]) -> pd.DataFrame:
    """
    parquet 읽기는 페이지를 옮길 때마다 반복되므로 캐시한다.
    엔진을 다시 돌렸다면 사이드바의 '런 목록 새로고침'으로 비운다.
    """
    return load_results(list(keys), exit_rules=list(exit_rules) or None)


@st.cache_data(show_spinner=False)
def _cached_runs(strategy_id: Optional[str], variant: Optional[str], since: Optional[str], until: Optional[str]):
    return list_runs(strategy_id=strategy_id, param_variant=variant, since=since, until=until)


def _all_runs():
    return _cached_runs(None, None, None, None)


def render_run_sidebar(*, title: str = "런 선택") -> RunSelection:
    """
    사이드바에 런 선택 UI 를 그리고 선택 결과를 반환한다.

    저장된 런이 하나도 없으면 안내를 띄우고 페이지 실행을 멈춘다. 예전처럼
    특정 CSV 파일이 없다고 예외를 던지는 대신, 무엇을 하면 되는지 알려준다.
    """
    with st.sidebar:
        st.header(f"🗂️ {title}")

        if st.button("런 목록 새로고침", use_container_width=True,
                     help="엔진을 다시 돌린 뒤 새 런이 보이지 않으면 누르세요."):
            st.cache_data.clear()
            st.rerun()

        runs = _all_runs()
        if not runs:
            st.warning("저장된 런이 없습니다.")
            st.code(
                "uv run python engine/main.py\n"
                "uv run python engine/nxt_tick_engine.py 005930",
                language="bash",
            )
            st.caption("백테스트를 실행하면 runs/ 에 런이 쌓이고 여기에 나타납니다.")
            st.stop()

        # --- 전략 / variant -------------------------------------------------
        strategies = sorted({r.strategy_id for r in runs})
        strategy = st.selectbox("전략", ["(전체)"] + strategies, index=0)
        strategy_id = None if strategy == "(전체)" else strategy

        variants = sorted({r.param_variant for r in runs if strategy_id in (None, r.strategy_id)})
        variant_choice = st.selectbox("파라미터 variant", ["(전체)"] + variants, index=0)
        variant = None if variant_choice == "(전체)" else variant_choice

        # --- 날짜 범위 -------------------------------------------------------
        bounds = [d for r in runs for d in r.date_range if d]
        since = until = None
        if bounds:
            lo, hi = min(bounds), max(bounds)
            picked = st.date_input(
                "런 날짜 범위",
                value=(pd.to_datetime(lo).date(), pd.to_datetime(hi).date()),
                min_value=pd.to_datetime(lo).date(),
                max_value=pd.to_datetime(hi).date(),
                help="런이 '다룬 날짜'와 겹치는지로 거릅니다. 실행 시각이 아닙니다.",
            )
            if isinstance(picked, (tuple, list)) and len(picked) == 2:
                since = picked[0].strftime("%Y%m%d")
                until = picked[1].strftime("%Y%m%d")

        candidates = _cached_runs(strategy_id, variant, since, until)
        if not candidates:
            st.warning("조건에 맞는 런이 없습니다. 필터를 넓혀 주세요.")
            st.stop()

        # --- 런 복수 선택 ----------------------------------------------------
        options = [r.storage_key or r.run_id for r in candidates]
        labels = {(r.storage_key or r.run_id): run_label(r) for r in candidates}

        # 기본값: 거래가 있는 가장 최근 런. 거래 0건 런이 기본으로 잡히면
        # 처음 열었을 때 빈 화면을 보게 된다.
        with_trades = [
            (r.storage_key or r.run_id) for r in candidates
            if (r.metrics or {}).get("trades", 0) > 0
        ]
        previous = [k for k in st.session_state.get(SESSION_KEY, []) if k in options]
        default = previous or with_trades[:1] or options[:1]

        keys = st.multiselect(
            "런 (복수 선택 시 비교)",
            options,
            default=default,
            format_func=lambda k: labels.get(k, k),
            help="두 개 이상 고르면 run_id 별로 성과를 나란히 비교합니다.",
        )
        st.session_state[SESSION_KEY] = keys

        if not keys:
            st.warning("런을 하나 이상 선택하세요.")
            st.stop()

        selected = [r for r in candidates if (r.storage_key or r.run_id) in keys]

        # --- 청산 규칙 필터 ---------------------------------------------------
        # 매니페스트의 by_exit_rule 만 보므로 parquet 을 열지 않는다.
        rule_options = exit_rules_of(selected)
        exit_rules: list[str] = []
        if rule_options:
            kept = [r for r in st.session_state.get(SESSION_RULES, []) if r in rule_options]
            exit_rules = st.multiselect(
                "청산 규칙 (exit_rule)",
                rule_options,
                default=kept,
                help="비우면 전체. 3대 트레일링 컷을 하나씩 떼어 볼 때 씁니다.",
            )
            st.session_state[SESSION_RULES] = exit_rules

        st.caption(f"선택: {len(keys)}개 런 · 거래 "
                   f"{sum((m.metrics or {}).get('trades', 0) for m in selected):,}건")

    return RunSelection(keys=keys, manifests=selected, exit_rules=exit_rules)


def get_selected_trades(*, title: str = "런 선택") -> tuple[pd.DataFrame, RunSelection]:
    """
    페이지가 부르는 한 줄짜리 진입점.

        df, selection = get_selected_trades()

    예전의 `df = load_results()` 자리를 대신한다. 차이는 하나뿐이다 — 무엇을
    읽을지가 코드에 박힌 파일 경로가 아니라 사용자의 선택에서 온다.
    """
    selection = render_run_sidebar(title=title)
    df = _cached_trades(tuple(selection.keys), tuple(selection.exit_rules))

    if df.empty:
        st.info(
            "선택한 런에 거래가 없습니다. 다른 런을 고르거나 청산 규칙 필터를 비워 보세요.\n\n"
            f"({selection.summary_line()})"
        )
    return df, selection
