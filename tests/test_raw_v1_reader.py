import sqlite3

import pytest

from engine.raw_v1_reader import RawV1Error, read_raw_v1


def database(tmp_path, trades=(), quotes=()):
    path = tmp_path / "closed.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE raw_trades(t_time,code,price,vol,is_buy)")
        conn.execute("CREATE TABLE raw_quotes(q_time,code,offer_p,offer_v,bid_p,bid_v)")
        conn.executemany("INSERT INTO raw_trades VALUES (?,?,?,?,?)", trades)
        conn.executemany("INSERT INTO raw_quotes VALUES (?,?,?,?,?,?)", quotes)
    return path


def trade(time="090001", code="005930"):
    return (time, code, 100, 1, 0)


def quote(time="090001", ask="101"):
    return (time, "005930", ask, "10", "99", "10")


def read(path, **kwargs):
    with read_raw_v1(path, allow_second_only=True, **kwargs) as events:
        return list(events)


def test_requires_explicit_research_opt_in_before_open(tmp_path):
    with pytest.raises(RawV1Error, match="opt-in"):
        with read_raw_v1(tmp_path / "missing.db"):
            pass
    assert not (tmp_path / "missing.db").exists()


def test_prior_second_quotes_only_and_all_events_preserved(tmp_path):
    path = database(tmp_path, [trade(), trade("090002")],
                    [quote("090000"), quote(ask="111"), quote(ask="121")])
    before = path.read_bytes()
    views = read(path)
    trades = [v for v in views if v.event.table == "raw_trades"]
    assert len(views) == 5
    assert [v.quote.fields[2] for v in trades] == ["101", "121"]
    assert all(v.policy == "prior_second_v1" for v in views)
    assert all(v.event.order_quality == "second_only" for v in views)
    assert all(v.event.venue == "unknown" for v in views)
    assert path.read_bytes() == before


def test_same_second_only_quote_cannot_fill_missing_quote(tmp_path):
    views = read(database(tmp_path, [trade()], [quote()]))
    assert views[0].event.table == "raw_trades"
    assert views[0].quote is None and views[0].quote_status == "missing"


def test_stale_and_other_symbol_are_explicit(tmp_path):
    views = read(database(tmp_path, [trade("090002"), trade("090002", "000660")],
                          [quote("090000")]), max_quote_age_seconds=1)
    assert views[1].quote_status == "stale"
    assert views[1].quote_age_seconds == 2
    assert views[2].quote_status == "missing"


def test_duplicate_values_keep_provenance(tmp_path):
    views = read(database(tmp_path, [trade(), trade()]))
    assert [v.event.rowid for v in views] == [1, 2]
    assert views[0].event.fields == views[1].event.fields
    assert views[0].event.file.endswith("closed.db")


@pytest.mark.parametrize("table", ["trades", "quotes"])
def test_clock_reversal_rejects_run(tmp_path, table):
    values = [trade("090002"), trade()] if table == "trades" else [quote("090002"), quote()]
    with pytest.raises(RawV1Error, match="reversed"):
        read(database(tmp_path, **{table: values}))


@pytest.mark.parametrize("clock", ["250000", "096000", "090060", "90001", None, b"090001"])
def test_invalid_clock_rejects_without_coercion(tmp_path, clock):
    with pytest.raises(RawV1Error):
        read(database(tmp_path, [trade(clock)]))


def test_early_exit_closes_generator_and_read_transaction(tmp_path):
    path = database(tmp_path, [trade(), trade()])
    with read_raw_v1(path, allow_second_only=True) as events:
        next(events)
    with pytest.raises(StopIteration):
        next(events)
    with sqlite3.connect(path, timeout=0.1) as conn:
        conn.execute("BEGIN EXCLUSIVE")


def test_missing_source_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        read(path)
    assert not path.exists()
