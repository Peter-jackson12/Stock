from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="Stock Control Tower", page_icon="📈", layout="wide")
from dashboard.access import require_access
require_access()

view = st.sidebar.radio("화면", ["운영 관리", "백테스트 분석"], key="main_view")
if view == "운영 관리":
    from dashboard.operations import render_control_tower
    render_control_tower()
    st.stop()

from dashboard.charts import cumulative_pnl_by_run_chart, exit_rule_chart, run_comparison_chart
from dashboard.data_service import compare_runs
from dashboard.metrics import daily_performance_by_run, exit_rule_performance, summary_metrics
from dashboard.run_selector import get_selected_trades

st.title("📈 Stock Strategy Analytics POC")
st.caption("결과 파일 하나를 열던 화면을 런(run) 비교기로 바꿨습니다. 왼쪽에서 런을 고르면 됩니다.")

df, selection = get_selected_trades()

# ---------------------------------------------------------------------------
# 선택된 런 목록 — 매니페스트가 곧 재현 정보다 (§6.3)
# ---------------------------------------------------------------------------
st.markdown("### 선택된 런")
runs_table = selection.frame()
st.dataframe(
    runs_table[[
        "storage_key", "strategy_id", "version", "variant", "engine", "mode",
        "date_from", "date_to", "trades", "win_rate", "net_pnl_sum", "mdd_pct", "git_sha",
    ]],
    use_container_width=True,
    hide_index=True,
)
st.caption("각 런의 지표는 실행 시점에 매니페스트로 기록된 값입니다. 화면에서 다시 계산하지 않습니다.")

if df.empty:
    st.stop()

# ---------------------------------------------------------------------------
# 선택 전체 KPI
# ---------------------------------------------------------------------------
m = summary_metrics(df)
c1, c2, c3, c4 = st.columns(4)
c1.metric("총 거래", f"{m['trades']:,}건")
c2.metric("평균 PnL", f"{m['avg_pnl']:.3f}%")
c3.metric("승률", f"{m['win_rate']:.2f}%")
c4.metric("평균 MDD", f"{m['avg_mae_pct']:.3f}%")

# ---------------------------------------------------------------------------
# 런 비교 — 이 화면이 존재하는 이유
# ---------------------------------------------------------------------------
if selection.is_multi:
    st.markdown("### 런 비교")
    comparison = compare_runs(df)
    st.plotly_chart(run_comparison_chart(comparison), use_container_width=True)
    st.dataframe(comparison, use_container_width=True, hide_index=True)
    st.plotly_chart(
        cumulative_pnl_by_run_chart(daily_performance_by_run(df)),
        use_container_width=True,
    )
else:
    st.info("왼쪽에서 런을 두 개 이상 선택하면 이 자리에 비교 화면이 나타납니다.")

# ---------------------------------------------------------------------------
# 청산 규칙 비교 — 틱 엔진의 3대 컷이 한 런 안에 들어 있다 (§4.1)
# ---------------------------------------------------------------------------
rule_perf = exit_rule_performance(df)
if len(rule_perf) > 1:
    st.markdown("### 청산 규칙 비교")
    st.plotly_chart(exit_rule_chart(rule_perf), use_container_width=True)
    st.dataframe(rule_perf, use_container_width=True, hide_index=True)

st.markdown("### 이 POC에서 해결하는 문제")
st.markdown(
    """
- **Overview**: 전체 전략 상태를 KPI와 시계열로 즉시 확인
- **Performance**: 날짜·종목·청산사유 필터와 피벗 대체 집계
- **Strategy Analysis**: 조건/변수 구간별 성과를 자동 비교
- **Insight**: 사람이 피벗 결과를 읽던 해석 단계를 규칙 기반으로 자동화
    """
)

st.info("왼쪽 사이드바의 Pages에서 분석 화면을 선택해 주세요.")

with st.expander("현재 데이터셋 정보"):
    st.write(f"거래 건수: {len(df):,}건")
    st.write(f"기간: {df['date'].min().date()} ~ {df['date'].max().date()}")
    st.write(f"컬럼 수: {len(df.columns)}개 (signal_meta 전개 포함)")
    st.dataframe(df.head(20), use_container_width=True, hide_index=True)
