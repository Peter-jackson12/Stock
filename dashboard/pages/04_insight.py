import streamlit as st

from dashboard.data_service import load_results, filter_results
from dashboard.analyzer import generate_insights, compare_poc_strategies, recommend_strategy
from dashboard.metrics import summary_metrics, exit_reason_performance, hour_performance
from dashboard.ollama_client import render_ai_chat, table_to_context

st.set_page_config(page_title="Insight", page_icon="💡", layout="wide")
st.title("💡 Automated Insight")
st.caption("기획안의 최종 의사결정에 맞춰 성과를 해석하고, POC 수준의 추천 전략까지 자동으로 제시합니다.")

df = load_results()

min_date, max_date = df["date"].min().date(), df["date"].max().date()
left, right = st.columns([2, 1])
with left:
    date_range = st.date_input("분석 기간", value=(min_date, max_date), min_value=min_date, max_value=max_date)
with right:
    reason = st.multiselect("청산 사유", sorted(df["msg"].dropna().astype(str).unique().tolist()))

if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date = end_date = date_range

filtered = filter_results(df, start_date=start_date, end_date=end_date, exit_reasons=reason)
m = summary_metrics(filtered)

cols = st.columns(5)
cols[0].metric("거래 수", f"{m['trades']:,}")
cols[1].metric("평균 PnL", f"{m['avg_pnl']:.3f}%")
cols[2].metric("승률", f"{m['win_rate']:.2f}%")
cols[3].metric("POC TPI", f"{m['tpi']:.3f}")
cols[4].metric("평균 MDD", f"{m['avg_mdd']:.3f}%")

st.markdown("### 자동 분석 결과")
for item in generate_insights(filtered):
    text = f"**{item['title']}**  \n{item['text']}"
    if item["level"] == "danger":
        st.error(text)
    elif item["level"] == "warning":
        st.warning(text)
    elif item["level"] == "success":
        st.success(text)
    else:
        st.info(text)

st.markdown("### POC 추천 전략")
comparison = compare_poc_strategies(filtered, cbv_percentile=70, latest_entry_hour=9)
best = recommend_strategy(comparison)
if best:
    st.success(
        f"현재 데이터의 기본 비교 기준에서는 **{best['strategy']}**이 POC TPI 기준 가장 높습니다. "
        f"TPI {best['tpi']:.3f}, 승률 {best['win_rate']:.2f}%, 평균 MDD {best['avg_mdd']:.3f}%입니다."
    )
    st.dataframe(comparison, use_container_width=True, hide_index=True)
else:
    st.info("추천 가능한 전략 비교 결과가 없습니다.")

st.warning("POC TPI는 `평균 PnL ÷ |평균 MDD|` 대체식입니다. 실제 프로젝트에서는 본인이 사용하는 TPI 산식으로 교체하세요.")

rule_insights = generate_insights(filtered)
insight_text = "\n".join([f"- {x['title']}: {x['text']}" for x in rule_insights])
insight_context = f"""
[분석 기간]
- {start_date} ~ {end_date}
- 청산 사유 필터: {reason if reason else '전체'}

[핵심 KPI]
- 거래 수: {m['trades']:,}건
- 평균 PnL: {m['avg_pnl']:.4f}%
- 승률: {m['win_rate']:.2f}%
- POC TPI: {m['tpi']:.4f}
- 평균 MDD: {m['avg_mdd']:.4f}%
- Profit Factor: {m['profit_factor']:.4f}

[규칙 기반 자동 분석]
{insight_text}

[전략 비교]
{table_to_context(comparison, max_rows=10) if not comparison.empty else '없음'}

[청산 사유별 집계]
{table_to_context(exit_reason_performance(filtered))}

[시간대별 집계]
{table_to_context(hour_performance(filtered))}
"""

render_ai_chat(
    "Insight",
    insight_context,
    [
        "추천 전략을 왜 골랐는지 설명해줘",
        "TPI와 MDD를 같이 보고 개선 순서를 정해줘",
        "다음 백테스트 실험 3개를 제안해줘",
    ],
)
