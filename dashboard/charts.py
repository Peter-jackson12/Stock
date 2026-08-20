from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go


def cumulative_pnl_chart(daily: pd.DataFrame):
    fig = px.line(daily, x="date", y="cumulative_pnl", markers=False, labels={"date": "Date", "cumulative_pnl": "Cumulative PnL (%)"})
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=35, b=10), title="누적 PnL 추이")
    return fig


def daily_avg_pnl_chart(daily: pd.DataFrame):
    fig = px.bar(daily, x="date", y="avg_pnl", labels={"date": "Date", "avg_pnl": "Avg PnL (%)"})
    fig.add_hline(y=0, line_width=1)
    fig.update_layout(height=340, margin=dict(l=10, r=10, t=35, b=10), title="일별 평균 PnL")
    return fig


def exit_reason_chart(exit_df: pd.DataFrame):
    fig = px.bar(exit_df, x="msg", y="trade_count", text_auto=True, labels={"msg": "Exit reason", "trade_count": "Trades"})
    fig.update_layout(height=340, margin=dict(l=10, r=10, t=35, b=10), title="청산 사유별 거래 건수")
    return fig


def pnl_distribution_chart(df: pd.DataFrame):
    fig = px.histogram(df, x="pnl", nbins=60, labels={"pnl": "PnL (%)", "count": "Trades"})
    fig.add_vline(x=0, line_width=1)
    fig.update_layout(height=360, margin=dict(l=10, r=10, t=35, b=10), title="PnL 분포")
    return fig


def strategy_group_chart(grouped: pd.DataFrame):
    fig = go.Figure()
    fig.add_trace(go.Bar(x=grouped["group"], y=grouped["avg_pnl"], name="평균 PnL (%)"))
    fig.add_trace(go.Scatter(x=grouped["group"], y=grouped["win_rate"], mode="lines+markers", name="승률 (%)", yaxis="y2"))
    fig.update_layout(
        height=410,
        margin=dict(l=10, r=10, t=40, b=10),
        title="구간별 평균 PnL과 승률",
        yaxis=dict(title="Avg PnL (%)"),
        yaxis2=dict(title="Win rate (%)", overlaying="y", side="right"),
        legend=dict(orientation="h"),
    )
    return fig


def strategy_comparison_chart(comparison: pd.DataFrame):
    fig = go.Figure()
    fig.add_trace(go.Bar(x=comparison["strategy"], y=comparison["tpi"], name="POC TPI"))
    fig.add_trace(go.Scatter(x=comparison["strategy"], y=comparison["win_rate"], mode="lines+markers", name="승률 (%)", yaxis="y2"))
    fig.update_layout(
        height=410,
        margin=dict(l=10, r=10, t=40, b=10),
        title="전략별 POC TPI와 승률 비교",
        yaxis=dict(title="POC TPI"),
        yaxis2=dict(title="Win rate (%)", overlaying="y", side="right"),
        legend=dict(orientation="h"),
    )
    return fig
