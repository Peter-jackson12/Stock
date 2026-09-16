import sqlite3

import pandas as pd
import pytest

from core.price_policy import PriceBasisError
from engine import data_loader
from engine.engine import BackTestEngine, FEATURE_SOURCE_INLINE
from engine.price_inputs import strict_previous_close
from tests.test_daily_price_policy import mark, write_prices

CODE = "A005930"
TODAY = "20220426"


def frame(dates=("20220425", TODAY), prices=(100000, 101000)):
    return pd.DataFrame({"Code": ["Name", *dates], CODE: ["삼성전자", *prices]})


@pytest.mark.parametrize("value", [None, "", "bad", float("nan"), float("inf"), -float("inf"), 0, -1])
def test_strict_previous_close_rejects_invalid_values(value):
    with pytest.raises(PriceBasisError, match="전일 종가"):
        strict_previous_close(frame(prices=(value, 101000)), frame(), CODE, TODAY)


@pytest.mark.parametrize("dates", [
    (TODAY,), ("20220425",), ("20220425", TODAY, TODAY),
    ("20220427", TODAY), ("20220431", TODAY), ("20220425.0", TODAY),
])
def test_missing_duplicate_unsorted_invalid_dates_fail(dates):
    bad = frame(dates=dates, prices=[100000] * len(dates))
    with pytest.raises(PriceBasisError):
        strict_previous_close(bad, frame(), CODE, TODAY)


def test_does_not_skip_null_predecessor_to_find_older_price():
    close = frame(("20220422", "20220425", TODAY), (99000, None, 101000))
    with pytest.raises(PriceBasisError, match="전일 종가"):
        strict_previous_close(close, close, CODE, TODAY)


def test_mismatched_predecessor_dates_fail():
    close = frame(("20220422", TODAY))
    with pytest.raises(PriceBasisError, match="전일 날짜가 다릅니다"):
        strict_previous_close(close, frame(), CODE, TODAY)


def test_weekend_gap_and_nondefault_index_do_not_change_row_selection():
    data = frame(("20220422", "20220425"), (100000, 90000))
    data.index = [10, 20, 30]
    assert strict_previous_close(data, data, CODE, "20220425") == 100000


class TrackingConnection(sqlite3.Connection):
    closed = False
    def close(self):
        self.closed = True
        super().close()


def setup_engine(tmp_path, monkeypatch, strict=True):
    write_prices(tmp_path)
    mark(tmp_path)
    monkeypatch.setattr(data_loader, "CSV_PATH", tmp_path)
    return BackTestEngine(require_actual_prices=strict, runs_root=tmp_path / "runs",
                          feature_source=FEATURE_SOURCE_INLINE, split=1)


def lob(open_price=100000):
    conn = sqlite3.connect(":memory:", factory=TrackingConnection)
    conn.execute('CREATE TABLE "005930" (time, open, high, low, close, vol, buy, sell, ticks)')
    conn.execute('INSERT INTO "005930" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                 ("090001", open_price, 100000, 100000, 100000, 1, 1, 0, 1))
    return conn


def test_missing_previous_close_aborts_run_closes_db_and_does_not_save(tmp_path, monkeypatch):
    engine = setup_engine(tmp_path, monkeypatch)
    engine.daily_data["close"].loc[1, CODE] = ""
    conn = lob()
    monkeypatch.setattr(engine, "_collectable_dates", lambda: [TODAY])
    monkeypatch.setattr(engine, "_declared_codes", lambda _: ["005930"])
    monkeypatch.setattr(engine.loader, "get_lob_db_connection", lambda _: conn)
    def forbidden(*a, **kw):
        raise AssertionError("failed strict run must not write success results")
    monkeypatch.setattr(engine, "_save_run", forbidden)
    monkeypatch.setattr(pd.DataFrame, "to_csv", forbidden)
    with pytest.raises(PriceBasisError, match=TODAY):
        engine.run()
    assert conn.closed
    assert engine.storage_key == ""


@pytest.mark.parametrize("value", [0, -1, float("inf"), "bad"])
def test_invalid_lob_open_cannot_supply_tick_size_fallback(tmp_path, monkeypatch, value):
    engine = setup_engine(tmp_path, monkeypatch)
    conn = lob(value)
    try:
        with pytest.raises(PriceBasisError, match="LOB"):
            engine._process_stock(conn, CODE, "005930", TODAY)
    finally:
        conn.close()


def test_legacy_replay_keeps_existing_missing_close_behavior(tmp_path, monkeypatch):
    engine = setup_engine(tmp_path, monkeypatch, strict=False)
    engine.daily_data["close"].loc[1, CODE] = ""
    assert engine._prev_close(CODE, TODAY) is None
    conn = lob()
    try:
        engine._process_stock(conn, CODE, "005930", TODAY)
    finally:
        conn.close()
