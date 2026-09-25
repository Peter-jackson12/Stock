from dataclasses import replace

import pytest

from research.fast_backtest.universe import (
    HistoricalMetadata,
    UniverseFilter,
    UnsupportedUniverseFilter,
    apply_cheap_filters,
    select_historical_snapshot,
)


def metadata(code="005930", as_of_date="2026-09-18", **changes):
    base = HistoricalMetadata(
        as_of_date=as_of_date,
        code=code,
        name="삼성전자",
        market="KOSPI",
        close_krw=261000,
        volume=17489615,
        trading_value_krw=4554291006250,
        market_cap_krw=1525878716688000,
        listed_shares=5846278608,
        float_ratio_pct=None,
        float_shares=None,
        float_market_cap_krw=None,
        source="fixture",
        validation_status="VALID",
    )
    return replace(base, **changes)


def test_causal_preopen_selects_latest_prior_trading_snapshot_and_ignores_future():
    rows = [
        metadata(as_of_date="2026-09-17", close_krw=250000),
        metadata(as_of_date="2026-09-18", close_krw=261000),
        metadata(as_of_date="2026-09-21", close_krw=270000),
        metadata(as_of_date="2026-09-22", close_krw=280000),
    ]

    selected = select_historical_snapshot(rows, trade_date="2026-09-21")

    assert selected.metadata_as_of_date == "2026-09-18"
    assert selected.rows[0].close_krw == 261000
    assert selected.non_causal is False
    assert selected.screening_only is True


def test_causal_preopen_never_uses_same_day_eod():
    selected = select_historical_snapshot(
        [metadata(as_of_date="2026-09-17"), metadata(as_of_date="2026-09-18")],
        trade_date="2026-09-18",
    )
    assert selected.metadata_as_of_date == "2026-09-17"


def test_posthoc_same_day_requires_opt_in_and_marks_non_causal():
    rows = [metadata()]
    with pytest.raises(ValueError, match="explicit opt-in"):
        select_historical_snapshot(rows, trade_date="2026-09-18", mode="posthoc_same_day")

    selected = select_historical_snapshot(
        rows,
        trade_date="2026-09-18",
        mode="posthoc_same_day",
        posthoc_same_day_opt_in=True,
    )
    assert selected.metadata_as_of_date == "2026-09-18"
    assert selected.non_causal is True
    assert selected.screening_only is True


def test_market_price_trading_value_and_total_market_cap_filters():
    rows = [
        metadata(code="005930"),
        metadata(
            code="000001",
            market="KOSDAQ",
            close_krw=1000,
            trading_value_krw=10,
            market_cap_krw=20,
        ),
    ]
    selected = select_historical_snapshot(rows, trade_date="2026-09-21")
    filtered = apply_cheap_filters(
        selected,
        UniverseFilter(
            allowed_market=("KOSPI",),
            min_price=2000,
            max_price=300000,
            min_trading_value=100,
            min_market_cap=100,
            max_market_cap=2000000000000000,
        ),
    )
    assert [row.code for row in filtered.rows] == ["005930"]
    assert filtered.size_filter_basis == "total_market_cap_proxy"


def test_float_filter_is_explicitly_rejected_without_float_data():
    selected = select_historical_snapshot([metadata()], trade_date="2026-09-21")
    with pytest.raises(UnsupportedUniverseFilter, match="not a silent fallback"):
        apply_cheap_filters(selected, UniverseFilter(min_float_market_cap=1))


def test_duplicate_code_is_rejected():
    with pytest.raises(ValueError, match="duplicate metadata code"):
        select_historical_snapshot([metadata(), metadata()], trade_date="2026-09-21")
