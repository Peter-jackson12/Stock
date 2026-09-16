import json

import pandas as pd
import pytest

from collector import daily_collector as collector
from collector.daily_snapshot import write_snapshot, read_all_snapshots, rebuild_wide_csvs


def rows(date="20260916", code="005930", shares=100, mkt=10, ratio=None):
    return pd.DataFrame([dict(date=date, code=code, name="삼성전자", shares=shares, mkt=mkt, float=ratio)])


def test_partial_retry_preserves_values_codes_and_names(tmp_path):
    write_snapshot(tmp_path, "20260916", rows())
    write_snapshot(tmp_path, "20260916", rows(code="000660", shares=None, mkt=None))
    write_snapshot(tmp_path, "20260916", rows(shares=None, mkt=None, ratio=30))
    saved = read_all_snapshots(tmp_path).set_index("code")
    assert set(saved.index) == {"005930", "000660"}
    assert saved.loc["005930", "shares"] == 100
    assert saved.loc["005930", "mkt"] == 10
    assert saved.loc["005930", "float"] == 30
    rebuild_wide_csvs(tmp_path, name_map={})
    wide = pd.read_csv(tmp_path / "shares.csv", encoding="cp949")
    assert list(wide["Code"]) == ["Name", "20260916"]
    assert wide.loc[0, "A005930"] == "삼성전자"
    assert "A000660" in wide


def test_retry_does_not_overwrite_first_valid_value_or_mix_market_cap(tmp_path):
    write_snapshot(tmp_path, "20260916", rows(mkt=None))
    write_snapshot(tmp_path, "20260916", rows(shares=200, mkt=20))
    saved = read_all_snapshots(tmp_path).iloc[0]
    assert saved.shares == 100
    assert pd.isna(saved.mkt)
    write_snapshot(tmp_path, "20260916", rows(shares=100, mkt=10))
    assert read_all_snapshots(tmp_path).iloc[0].mkt == 10


def test_date_column_legacy_read_and_next_day_preserved(tmp_path):
    directory = tmp_path / "snapshots"
    directory.mkdir()
    legacy = directory / "20260915.csv"
    rows("20260915").drop(columns="date").to_csv(legacy, index=False)
    original = legacy.read_bytes()
    write_snapshot(tmp_path, "20260916", rows())
    assert set(read_all_snapshots(tmp_path).date) == {"20260915", "20260916"}
    assert legacy.read_bytes() == original
    assert "date" in pd.read_csv(directory / "20260916.csv")


@pytest.mark.parametrize("bad", [rows("20260915"), rows().drop(columns="date"), pd.concat([rows(), rows()])])
def test_invalid_snapshot_does_not_replace_file(tmp_path, bad):
    out = write_snapshot(tmp_path, "20260916", rows())
    original = out.read_bytes()
    with pytest.raises(ValueError):
        write_snapshot(tmp_path, "20260916", bad)
    assert out.read_bytes() == original


def test_atomic_replace_failure_preserves_snapshot(tmp_path, monkeypatch):
    import collector.daily_snapshot as snapshots
    out = write_snapshot(tmp_path, "20260916", rows())
    original = out.read_bytes()
    def fail(*args):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(snapshots.os, "replace", fail)
    with pytest.raises(OSError):
        write_snapshot(tmp_path, "20260916", rows(ratio=30))
    assert out.read_bytes() == original
    assert not list(out.parent.glob("*.tmp"))


def mock_source(monkeypatch, dates, observed="20260916", empty=False):
    frame = pd.DataFrame([dict(open=10, high=12, low=9, close=11, vol=100)] * len(dates), index=dates)
    if empty:
        frame = pd.DataFrame()
    def fetch(*args, **kwargs):
        return "삼성전자", frame, dict(shares=100, float=None, observed_date=observed,
                                     shares_reason=None, float_reason="no_crawler_implemented")
    monkeypatch.setattr(collector, "fetch_stock_meta_and_candles", fetch)
    monkeypatch.setattr(collector, "observation_date", lambda: "20260916")
    monkeypatch.setattr(collector.time, "sleep", lambda _: None)


def test_historical_request_cannot_backdate_current_shares(tmp_path, monkeypatch):
    mock_source(monkeypatch, ["20220425", "20220426"])
    collector.FastDailyCollector(tmp_path).collect("20220425", "20220426", ["005930"])
    assert not list((tmp_path / "snapshots").glob("*.csv"))
    meta = json.loads((tmp_path / "_meta_manifest.json").read_text(encoding="utf-8"))
    assert meta["codes"]["005930"]["shares_applied_to"] is None
    assert meta["price_basis"] == "split_adjusted_observed"


def test_stale_calendar_does_not_backdate_and_does_not_use_old_close(tmp_path, monkeypatch):
    mock_source(monkeypatch, ["20260915"])
    collector.FastDailyCollector(tmp_path).collect("20260915", "20260916", ["005930"])
    saved = read_all_snapshots(tmp_path).iloc[0]
    assert saved.date == "20260916"
    assert saved.shares == 100
    assert pd.isna(saved.mkt)


def test_midnight_observation_is_not_assigned_to_previous_day(tmp_path, monkeypatch):
    mock_source(monkeypatch, ["20260916"], observed="20260917")
    collector.FastDailyCollector(tmp_path).collect("20260916", "20260916", ["005930"])
    assert pd.isna(read_all_snapshots(tmp_path).iloc[0].shares)


def test_all_candles_failed_still_preserves_snapshot_and_records_reason(tmp_path, monkeypatch):
    write_snapshot(tmp_path, "20260916", rows())
    mock_source(monkeypatch, [], observed=None, empty=True)
    collector.FastDailyCollector(tmp_path).collect("20260916", "20260916", ["005930", "000660"])
    saved = read_all_snapshots(tmp_path).set_index("code")
    assert saved.loc["005930", "shares"] == 100
    assert pd.isna(saved.loc["000660", "shares"])
    meta = json.loads((tmp_path / "_meta_manifest.json").read_text(encoding="utf-8"))
    assert meta["codes"]["000660"]["candles_reason"] == "no_candles"


def test_fchart_cannot_overwrite_actual_price_matrix(tmp_path, monkeypatch):
    mock_source(monkeypatch, ["20260916"])
    actual = tmp_path / "close.csv"
    actual.write_text("existing actual price input", encoding="utf-8")
    collector.FastDailyCollector(tmp_path).collect("20260916", "20260916", ["005930"])
    assert actual.read_text(encoding="utf-8") == "existing actual price input"
    assert (tmp_path / "unverified_fchart" / "close.csv").exists()
    from core.price_policy import PRICE_MANIFEST
    marker = json.loads((tmp_path / "unverified_fchart" / PRICE_MANIFEST).read_text())
    assert marker["price_basis"] == "split_adjusted_observed"
    assert "close.csv" in marker["files"]
    assert not (tmp_path / PRICE_MANIFEST).exists()
    assert read_all_snapshots(tmp_path).iloc[0].shares == 100
