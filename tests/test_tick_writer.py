import queue
import sqlite3
import threading

import pytest

from collector.kiwoom.tick_writer import TickWriter


TRADE = ("090001", "005930", 50000.0, 2, 1)
QUOTE = ("090001", "005930", "50000", "2", "49950", "3")


def setup_writer(tmp_path, n=1, **kwargs):
    trades, quotes = queue.Queue(), queue.Queue()
    for _ in range(n):
        trades.put(TRADE)
        quotes.put(QUOTE)
    return TickWriter(tmp_path / "ticks.db", trades, quotes, **kwargs)


def counts(path):
    with sqlite3.connect(path) as conn:
        return tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in ("raw_trades", "raw_quotes"))


def test_shutdown_drains_multiple_batches_and_preserves_existing_rows(tmp_path):
    writer = setup_writer(tmp_path, n=2501)
    writer.stop.set()
    writer.run()
    assert writer.done.is_set() and writer.error is None
    assert writer.pending == 0
    assert counts(writer.path) == (2501, 2501)
    second = setup_writer(tmp_path, n=1)
    second.stop.set()
    second.run()
    assert counts(writer.path) == (2502, 2502)
    assert (second.total_trades, second.total_quotes) == (1, 1)


class ConnectionProxy:
    def __init__(self, conn):
        self.conn = conn
        self.commits = 0
    def __getattr__(self, name):
        return getattr(self.conn, name)
    def commit(self):
        self.commits += 1
        return self.conn.commit()


def test_shutdown_waits_for_inflight_commit_and_counts_only_committed_rows(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class BlockingCommit(ConnectionProxy):
        def commit(self):
            if self.commits == 1:
                entered.set()
                assert release.wait(5)
            return super().commit()
    writer = setup_writer(tmp_path, connect=lambda *a, **kw: BlockingCommit(sqlite3.connect(*a, **kw)))
    thread = threading.Thread(target=writer.run)
    thread.start()
    try:
        assert entered.wait(5)
        assert writer.trades.empty() and writer.quotes.empty()
        assert writer.pending == 2
        assert (writer.total_trades, writer.total_quotes) == (0, 0)
        writer.stop.set()
        assert not writer.done.is_set()
    finally:
        release.set()
        writer.stop.set()
        thread.join(5)
    assert not thread.is_alive()
    assert counts(writer.path) == (1, 1)
    assert writer.pending == 0 and writer.error is None


@pytest.mark.parametrize("failure", ["commit", "quote_insert"])
def test_failed_transaction_retains_uncommitted_counts_and_rolls_back(tmp_path, failure):
    class FailingConnection(ConnectionProxy):
        def commit(self):
            if failure == "commit" and self.commits == 1:
                raise sqlite3.OperationalError("disk full")
            return super().commit()
        def executemany(self, sql, rows):
            if failure == "quote_insert" and "raw_quotes" in sql:
                raise sqlite3.OperationalError("write failed")
            return self.conn.executemany(sql, rows)
    writer = setup_writer(tmp_path, n=1001,
                          connect=lambda *a, **kw: FailingConnection(sqlite3.connect(*a, **kw)))
    writer.stop.set()
    writer.run()
    assert writer.done.is_set() and writer.error
    assert writer.pending == 2002  # includes the 2 rows not yet removed from queues
    assert (writer.total_trades, writer.total_quotes) == (0, 0)
    assert counts(writer.path) == (0, 0)


def test_database_open_failure_leaves_queues_visible(tmp_path):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database")
    writer = setup_writer(tmp_path, connect=fail)
    writer.run()
    assert writer.done.is_set() and "unable to open" in writer.error
    assert writer.pending == 2


def test_idle_writer_wakes_for_stop(tmp_path):
    writer = setup_writer(tmp_path, n=0, interval=60)
    thread = threading.Thread(target=writer.run)
    thread.start()
    writer.stop.set()
    thread.join(5)
    assert not thread.is_alive()
    assert writer.error is None and counts(writer.path) == (0, 0)
