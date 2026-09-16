"""SQLite tick writer: stop producers before requesting the final queue drain."""
import queue
import sqlite3
import threading


class TickWriter:
    def __init__(self, path, trades, quotes, *, batch_size=1000, interval=0.5,
                 connect=sqlite3.connect):
        self.path, self.trades, self.quotes = path, trades, quotes
        self.batch_size, self.interval, self.connect = batch_size, interval, connect
        self.stop = threading.Event()
        self.done = threading.Event()
        self.error = None
        self.total_trades = self.total_quotes = 0
        # Retain in-flight rows until commit; error reporting must include them.
        self.trade_batch, self.quote_batch = [], []

    @property
    def pending(self):
        """Exact after run() returns; an approximate observation while it runs."""
        return (self.trades.qsize() + self.quotes.qsize()
                + len(self.trade_batch) + len(self.quote_batch))

    def _take(self, source, batch):
        while len(batch) < self.batch_size:
            try:
                batch.append(source.get_nowait())
            except queue.Empty:
                break

    def run(self):
        conn = None
        try:
            conn = self.connect(self.path, timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL")
            # Preserve the existing live write policy. This is a graceful-stop
            # guarantee, not protection from process kills or power loss.
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("CREATE TABLE IF NOT EXISTS raw_trades "
                         "(t_time TEXT, code TEXT, price REAL, vol INTEGER, is_buy INTEGER)")
            conn.execute("CREATE TABLE IF NOT EXISTS raw_quotes "
                         "(q_time TEXT, code TEXT, offer_p TEXT, offer_v TEXT, bid_p TEXT, bid_v TEXT)")
            conn.commit()
            while True:
                self._take(self.trades, self.trade_batch)
                self._take(self.quotes, self.quote_batch)
                if self.trade_batch or self.quote_batch:
                    conn.executemany("INSERT INTO raw_trades VALUES (?, ?, ?, ?, ?)", self.trade_batch)
                    conn.executemany("INSERT INTO raw_quotes VALUES (?, ?, ?, ?, ?, ?)", self.quote_batch)
                    conn.commit()
                    self.total_trades += len(self.trade_batch)
                    self.total_quotes += len(self.quote_batch)
                    self.trade_batch.clear()
                    self.quote_batch.clear()
                if self.stop.is_set() and self.pending == 0:
                    break
                # Wake immediately at shutdown, and drain subsequent batches
                # without the live loop's 0.5 second pause.
                self.stop.wait(self.interval)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as exc:
                    self.error = self.error or f"close failed: {exc}"
            self.done.set()
