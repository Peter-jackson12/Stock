import streamlit as st

from dashboard.data_service import load_results
from dashboard.metrics import summary_metrics, daily_performance, exit_reason_performance
from dashboard.charts import cumulative_pnl_chart, daily_avg_pnl_chart, exit_reason_chart
from dashboard.ollama_client import render_ai_chat, table_to_context

st.set_page_config(page_title="Overview", page_icon="📊", layout="wide")
st.title("📊 Overview")
st.caption("기획안의 핵심 성과 지표와 백테스트 상태를 피벗 없이 한 화면에서 확인합니다.")

df = load_results()
m = summary_metrics(df)
daily = daily_performance(df)
exit_perf = exit_reason_performance(df)

cols = st.columns(6)
cols[0].metric("총 거래", f"{m['trades']:,}")
cols[1].metric("총 PnL 합", f"{m['total_pnl']:.1f}%")
cols[2].metric("승률", f"{m['win_rate']:.2f}%")
cols[3].metric("POC TPI", f"{m['tpi']:.3f}")
cols[4].metric("평균 MDD", f"{m['avg_mdd']:.3f}%")
cols[5].metric("Profit Factor", f"{m['profit_factor']:.3f}")

st.caption("※ 기획안에 TPI 산식이 명시되어 있지 않아 이번 POC의 TPI는 `평균 PnL ÷ |평균 MDD|` 대체식입니다.")

st.plotly_chart(cumulative_pnl_chart(daily), use_container_width=True)
left, right = st.columns(2)
with left:
    st.plotly_chart(daily_avg_pnl_chart(daily), use_container_width=True)
with right:
    st.plotly_chart(exit_reason_chart(exit_perf), use_container_width=True)

st.markdown("### 최근 일별 성과")
show = daily[["date", "trade_count", "avg_pnl", "win_rate", "avg_mdd", "total_pnl"]].tail(30).copy()
show["date"] = show["date"].dt.date
st.dataframe(show.sort_values("date", ascending=False), use_container_width=True, hide_index=True)

overview_context = f"""
[전체 KPI]
- 총 거래: {m['trades']:,}건
- 평균 PnL: {m['avg_pnl']:.4f}%
- 총 PnL 합: {m['total_pnl']:.4f}%
- 승률: {m['win_rate']:.2f}%
- POC TPI: {m['tpi']:.4f}
- 평균 MDD: {m['avg_mdd']:.4f}%
- 평균 MDU: {m['avg_mdu']:.4f}%
- Profit Factor: {m['profit_factor']:.4f}
- 평균 보유시간: {m['avg_holding_seconds']:.2f}초

[POC TPI 정의]
평균 PnL / 절대값 평균 MDD. 실제 TPI 공식이 제공되면 교체 필요.

[일별 성과 최근 데이터]
{table_to_context(daily.tail(20))}

[청산 사유별 성과]
{table_to_context(exit_perf)}
"""

render_ai_chat(
    "Overview",
    overview_context,
    [
        "TPI와 MDD 중심으로 성과를 평가해줘",
        "이 전략의 가장 큰 문제는 뭐야?",
        "먼저 확인할 개선 포인트는?",
    ],
)
