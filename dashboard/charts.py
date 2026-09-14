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
    fig = px.bar(exit_df, x="exit_reason", y="trade_count", text_auto=True,
                 labels={"exit_reason": "Exit reason", "trade_count": "Trades"})
    fig.update_layout(height=340, margin=dict(l=10, r=10, t=35, b=10), title="청산 사유별 거래 건수")
    return fig


def pnl_distribution_chart(df: pd.DataFrame):
    fig = px.histogram(df, x="net_pnl_pct", nbins=60, labels={"net_pnl_pct": "PnL (%)", "count": "Trades"})
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


# ---------------------------------------------------------------------------
# 런 비교 (ARCHITECTURE_V2.md §7.5)
#
# '파일 뷰어'에는 존재할 수 없던 차트들이다. 비교 대상이 하나뿐이면 비교 화면도
# 성립하지 않는다 (§7.1). 런 스토어가 생기면서 비로소 그릴 수 있게 됐다.
# ---------------------------------------------------------------------------

def cumulative_pnl_by_run_chart(daily_by_run: pd.DataFrame, by: str = "run_label"):
    """런별 누적 PnL 곡선을 한 좌표계에 겹쳐 그린다."""
    fig = px.line(
        daily_by_run, x="date", y="cumulative_pnl", color=by, markers=True,
        labels={"date": "Date", "cumulative_pnl": "Cumulative PnL (%)", by: "Run"},
    )
    fig.add_hline(y=0, line_width=1)
    fig.update_layout(
        height=420, margin=dict(l=10, r=10, t=40, b=10),
        title="런별 누적 PnL 비교",
        legend=dict(orientation="h", yanchor="bottom", y=-0.45),
    )
    return fig


def run_comparison_chart(comparison: pd.DataFrame, by: str = "run_label"):
    """런별 누적 PnL(막대)과 승률(선)을 한 화면에서 비교."""
    fig = go.Figure()
    fig.add_trace(go.Bar(x=comparison[by], y=comparison["total_pnl"], name="누적 PnL (%)"))
    fig.add_trace(go.Scatter(
        x=comparison[by], y=comparison["win_rate"], mode="lines+markers",
        name="승률 (%)", yaxis="y2",
    ))
    fig.update_layout(
        height=420,
        margin=dict(l=10, r=10, t=40, b=10),
        title="런별 누적 PnL과 승률",
        yaxis=dict(title="Total PnL (%)"),
        yaxis2=dict(title="Win rate (%)", overlaying="y", side="right"),
        legend=dict(orientation="h"),
        xaxis=dict(tickangle=-15),
    )
    return fig


def exit_rule_chart(rule_df: pd.DataFrame):
    """청산 규칙별 성과 — 3대 트레일링 컷 비교 (§4.1)."""
    fig = go.Figure()
    fig.add_trace(go.Bar(x=rule_df["exit_rule"], y=rule_df["total_pnl"], name="누적 PnL (%)"))
    fig.add_trace(go.Scatter(
        x=rule_df["exit_rule"], y=rule_df["win_rate"], mode="lines+markers",
        name="승률 (%)", yaxis="y2",
    ))
    fig.update_layout(
        height=380,
        margin=dict(l=10, r=10, t=40, b=10),
        title="청산 규칙별 누적 PnL과 승률",
        yaxis=dict(title="Total PnL (%)"),
        yaxis2=dict(title="Win rate (%)", overlaying="y", side="right"),
        legend=dict(orientation="h"),
    )
    return fig
