from __future__ import annotations

import streamlit as st

from dashboard.data_service import load_results
from dashboard.metrics import summary_metrics

st.set_page_config(page_title="Stock Strategy Dashboard", page_icon="📈", layout="wide")

st.title("📈 Stock Strategy Analytics POC")
st.caption("백테스트 결과 CSV와 피벗테이블을 직접 조회하던 과정을 KPI·차트·자동 해석으로 대체하는 대시보드")

try:
    df = load_results()
    metrics = summary_metrics(df)
except Exception as exc:
    st.error(f"데이터 로딩에 실패했습니다: {exc}")
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("총 거래", f"{metrics['trades']:,}건")
c2.metric("평균 PnL", f"{metrics['avg_pnl']:.3f}%")
c3.metric("승률", f"{metrics['win_rate']:.2f}%")
c4.metric("평균 MDD", f"{metrics['avg_mdd']:.3f}%")

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
    st.write(f"컬럼 수: {len(df.columns)}개 (파생 컬럼 포함)")
    st.dataframe(df.head(20), use_container_width=True, hide_index=True)
