import streamlit as st

from dashboard.data_service import load_results, filter_results
from dashboard.metrics import summary_metrics, daily_performance, exit_reason_performance, hour_performance
from dashboard.charts import cumulative_pnl_chart, pnl_distribution_chart, exit_reason_chart

st.set_page_config(page_title="Performance", page_icon="🔎", layout="wide")
st.title("🔎 Performance")
st.caption("Excel Pivot Table 대신 기간·종목·청산 조건을 선택하면 집계 결과가 자동 갱신됩니다.")

df = load_results()

with st.sidebar:
    st.header("필터")
    min_date, max_date = df["date"].min().date(), df["date"].max().date()
    date_range = st.date_input("기간", value=(min_date, max_date), min_value=min_date, max_value=max_date)
    selected_names = st.multiselect("종목", sorted(df["name"].astype(str).unique().tolist()))
    selected_reasons = st.multiselect("청산 사유", sorted(df["msg"].dropna().astype(str).unique().tolist()))
    min_pnl, max_pnl = float(df["pnl"].min()), float(df["pnl"].max())
    pnl_range = st.slider("PnL 범위 (%)", min_value=min_pnl, max_value=max_pnl, value=(min_pnl, max_pnl))

if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date = end_date = date_range

filtered = filter_results(df, start_date, end_date, selected_names, selected_reasons, pnl_range)
m = summary_metrics(filtered)

cols = st.columns(5)
cols[0].metric("선택 거래", f"{m['trades']:,}")
cols[1].metric("평균 PnL", f"{m['avg_pnl']:.3f}%")
cols[2].metric("승률", f"{m['win_rate']:.2f}%")
cols[3].metric("Profit Factor", "∞" if m['profit_factor'] == float('inf') else f"{m['profit_factor']:.3f}")
cols[4].metric("평균 보유시간", f"{m['avg_holding_seconds']:.1f}초")

if filtered.empty:
    st.warning("현재 필터 조건에 해당하는 거래가 없습니다.")
    st.stop()

daily = daily_performance(filtered)
left, right = st.columns(2)
with left:
    st.plotly_chart(cumulative_pnl_chart(daily), use_container_width=True)
with right:
    st.plotly_chart(pnl_distribution_chart(filtered), use_container_width=True)

left, right = st.columns(2)
with left:
    st.markdown("### 청산 사유별 성과")
    exit_df = exit_reason_performance(filtered)
    st.plotly_chart(exit_reason_chart(exit_df), use_container_width=True)
    st.dataframe(exit_df, use_container_width=True, hide_index=True)
with right:
    st.markdown("### 진입 시간대별 성과")
    st.dataframe(hour_performance(filtered), use_container_width=True, hide_index=True)

st.markdown("### 거래 상세")
cols = [c for c in ["date", "name", "entry_time_text", "exit_time_text", "holding_seconds", "entry_price", "exit_price", "pnl", "mdd", "mdu", "msg", "cbv_1", "ctotal", "cum_amt"] if c in filtered.columns]
filtered_table = filtered[cols].copy()
st.dataframe(filtered_table, use_container_width=True, hide_index=True, height=450)

st.download_button(
    label="📥 현재 필터 결과 CSV 저장",
    data=filtered_table.to_csv(index=False).encode("utf-8-sig"),
    file_name=f"performance_filtered_{start_date}_{end_date}.csv",
    mime="text/csv",
    use_container_width=True,
    help="현재 선택한 기간·종목·청산사유·PnL 필터가 적용된 거래 상세 테이블을 그대로 저장합니다.",
)

from dashboard.ollama_client import render_ai_chat, table_to_context

performance_context = f"""
[현재 필터]
- 기간: {start_date} ~ {end_date}
- 선택 종목: {selected_names if selected_names else '전체'}
- 선택 청산 사유: {selected_reasons if selected_reasons else '전체'}
- PnL 범위: {pnl_range[0]:.4f}% ~ {pnl_range[1]:.4f}%

[필터 적용 KPI]
- 거래 수: {m['trades']:,}건
- 평균 PnL: {m['avg_pnl']:.4f}%
- 승률: {m['win_rate']:.2f}%
- Profit Factor: {m['profit_factor']:.4f}
- 평균 MDD: {m['avg_mdd']:.4f}%
- 평균 MDU: {m['avg_mdu']:.4f}%
- 평균 보유시간: {m['avg_holding_seconds']:.2f}초

[청산 사유별 성과]
{table_to_context(exit_reason_performance(filtered))}

[진입 시간대별 성과]
{table_to_context(hour_performance(filtered))}

[일별 성과]
{table_to_context(daily.tail(20))}
"""

render_ai_chat(
    "Performance",
    performance_context,
    [
        "현재 필터에서 왜 성과가 안 좋아?",
        "시간대별로 어디가 가장 나아?",
        "청산 사유별 문제를 분석해줘",
    ],
)
