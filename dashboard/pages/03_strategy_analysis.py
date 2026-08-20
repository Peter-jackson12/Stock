import streamlit as st

from dashboard.data_service import load_results
from dashboard.analyzer import (
    available_analysis_columns,
    analyze_numeric_bins,
    analyze_category,
    timeframe_comparison,
    compare_poc_strategies,
    recommend_strategy,
)
from dashboard.charts import strategy_group_chart, strategy_comparison_chart
from dashboard.ollama_client import render_ai_chat, table_to_context

st.set_page_config(page_title="Strategy Analysis", page_icon="🧪", layout="wide")
st.title("🧪 Strategy Analysis")
st.caption("기획안의 핵심 질문인 변수 비교, 5/10/30/60초 비교, 단일·복합 조건 전략 비교를 한 페이지에서 확인합니다.")

df = load_results()

st.markdown("## 1. 전략 변수 구간별 분석")
available = available_analysis_columns(df)
labels = {
    "entry_hour": "진입 시간대",
    "holding_seconds": "보유시간(초)",
    "cbv_1": "CBV 1초",
    "ctotal": "CTOTAL",
    "cum_amt": "누적 거래대금",
    "mdd": "MDD",
    "mdu": "MDU",
    "entry_price": "진입가",
}

if available:
    selected = st.selectbox("분석 변수", available, format_func=lambda x: labels.get(x, x))
    if selected == "entry_hour":
        grouped, error = analyze_category(df, selected)
        if error:
            st.warning(error)
        else:
            st.dataframe(grouped, use_container_width=True, hide_index=True)
            best = grouped.sort_values("avg_pnl", ascending=False).iloc[0]
            worst = grouped.sort_values("avg_pnl").iloc[0]
            st.info(f"평균 PnL 기준 상대적으로 좋은 시간대는 {best[selected]}시, 낮은 시간대는 {worst[selected]}시입니다.")
    else:
        bins = st.slider("분석 구간 수", 3, 10, 6)
        grouped, error = analyze_numeric_bins(df, selected, bins=bins)
        if error:
            st.warning(error)
        else:
            st.plotly_chart(strategy_group_chart(grouped), use_container_width=True)
            st.dataframe(grouped[["group", "trade_count", "avg_pnl", "win_rate", "avg_mdd", "min_value", "max_value"]], use_container_width=True, hide_index=True)
            best = grouped.sort_values("avg_pnl", ascending=False).iloc[0]
            st.success(f"현재 데이터에서는 {labels.get(selected, selected)}의 '{best['group']}' 구간이 평균 PnL 기준 상대적으로 가장 높습니다.")
else:
    st.warning("현재 CSV에서 구간 비교가 가능한 전략 변수를 찾지 못했습니다.")
    selected, grouped = "없음", None

st.divider()
st.markdown("## 2. 5초 / 10초 / 30초 / 60초 비교")
st.caption("기획안 Q2에 대응합니다. 실제 결과 CSV에 각 시간대 CBV 컬럼이 들어오면 동일 화면에서 자동 비교됩니다.")
time_df, missing = timeframe_comparison(df)

if not time_df.empty:
    st.dataframe(time_df, use_container_width=True, hide_index=True)
    best_tf = time_df.sort_values("tpi", ascending=False).iloc[0]
    st.success(f"POC TPI 기준 가장 높은 시간 단위는 {best_tf['timeframe']}입니다.")
else:
    st.info("현재 샘플 CSV에는 `cbv_5`, `cbv_10`, `cbv_30`, `cbv_60`이 없어 실제 시간 단위 비교값을 만들지 않았습니다.")

status_rows = []
for sec in [5, 10, 30, 60]:
    col = f"cbv_{sec}"
    status_rows.append({"시간 단위": f"{sec}초", "필요 컬럼": col, "현재 상태": "연동 가능" if col not in missing else "CSV 컬럼 추가 필요"})
st.dataframe(status_rows, use_container_width=True, hide_index=True)

st.divider()
st.markdown("## 3. 단일 조건 vs 복합 조건 전략 비교")
st.caption("기획안 Q3에 대응합니다. 현재 CSV에서 진입 시점에 알 수 있는 `cbv_1`과 진입시간을 이용해 POC 비교를 시연합니다.")

c1, c2 = st.columns(2)
with c1:
    cbv_percentile = st.slider("CBV 기준 백분위", 10, 90, 70, 10, help="예: 50이면 cbv_1 중앙값 이상 거래를 CBV 단일 조건으로 사용합니다.")
with c2:
    latest_hour = st.slider("최대 진입 시간(시)", 9, 15, 9, 1)

comparison = compare_poc_strategies(df, cbv_percentile=cbv_percentile, latest_entry_hour=latest_hour)
if comparison.empty:
    st.warning("전략 비교에 사용할 수 있는 거래가 없습니다.")
    best_strategy = None
else:
    st.plotly_chart(strategy_comparison_chart(comparison), use_container_width=True)
    comparison_table = comparison[["strategy", "condition", "trade_count", "avg_pnl", "win_rate", "avg_mdd", "profit_factor", "tpi"]].copy()
    st.dataframe(
        comparison_table,
        use_container_width=True,
        hide_index=True,
    )
    st.download_button(
        label="📥 전략 비교 결과 CSV 저장",
        data=comparison_table.to_csv(index=False).encode("utf-8-sig"),
        file_name="strategy_comparison_result.csv",
        mime="text/csv",
        use_container_width=True,
        help="현재 설정한 CBV 기준 백분위와 최대 진입 시간이 반영된 전략 비교 결과를 저장합니다.",
    )
    best_strategy = recommend_strategy(comparison)
    if best_strategy:
        st.success(
            f"🏆 현재 POC 추천 전략: **{best_strategy['strategy']}**  |  "
            f"POC TPI {best_strategy['tpi']:.3f}  |  승률 {best_strategy['win_rate']:.2f}%  |  "
            f"평균 MDD {best_strategy['avg_mdd']:.3f}%"
        )

st.warning(
    "**TPI 산식 주의:** 제공된 기획안에는 TPI의 정확한 공식이 적혀 있지 않아, 이번 POC에서는 전략 비교용 대체값으로 "
    "`평균 PnL ÷ |평균 MDD|`를 사용합니다. 실제 고도화 시 본인이 사용하는 TPI 공식으로 교체해야 합니다."
)

st.divider()
st.markdown("### 실제 고도화 시 결과 CSV에 추가할 핵심 컬럼")
st.code("cbv_5, cbv_10, cbv_30, cbv_60, tick_rate, buy_vol, sell_vol, bid_vol, offer_vol, max60buyratio, t_max1buyratio, amt_10s", language="text")

if grouped is not None and not getattr(grouped, "empty", True):
    variable_context = table_to_context(grouped, max_rows=20)
else:
    variable_context = "비교 가능한 변수 집계 없음"

strategy_context = f"""
[선택 변수]
- {selected}

[변수 구간별 성과]
{variable_context}

[시간 단위 비교]
{table_to_context(time_df, max_rows=10) if not time_df.empty else '현재 CSV에 cbv_5/10/30/60 컬럼이 없어 실측 비교 불가'}

[단일/복합 전략 비교]
{table_to_context(comparison, max_rows=10) if not comparison.empty else '비교 결과 없음'}

[POC TPI 정의]
- 평균 PnL / 절대값 평균 MDD
- 기획안에 실제 TPI 산식이 없어 임시 비교 지표로 사용

거래 수가 작은 전략을 단순히 최고라고 단정하지 말고 승률, 평균 PnL, MDD, Profit Factor를 함께 고려해서 답변하세요.
"""

render_ai_chat(
    "Strategy Analysis",
    strategy_context,
    [
        "단일 조건과 복합 조건 중 뭐가 더 나아?",
        "TPI와 MDD를 같이 보고 추천해줘",
        "실제 고도화 때 어떤 컬럼부터 추가할까?",
    ],
)
