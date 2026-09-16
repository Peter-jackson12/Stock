"""Offline raw-v2 SQLite contract prototype; not connected to the live collector.

Each record retains normalized input AND provider raw fields. Receipt sequence is
assigned by the caller at capture time. Missing/invalid source values belong in
raw_fields and nullable normalized fields; never synthesize a legacy sequence.
"""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from engine.tick_ordering import OrderedTick, ReceiveOrderReplay


SCHEMA = "raw_v2_prototype_1"


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _utc(text):
    if not isinstance(text, str):
        raise ValueError("UTC receipt timestamp text required")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("explicit UTC receipt timestamp required")


class RawV2Writer:
    """New-file only. Explicit finish is required; context exit alone is incomplete.

    One owner/thread. This is a storage prototype, not a queue/OCX capture adapter.
    Commit batches explicitly to bound crash loss. Never resume an old session.
    """
    def __init__(self, path, *, source, session_id, market_date, feed_scope):
        self.order = ReceiveOrderReplay(source=source, session_id=session_id, max_quote_age_ns=0)
        if date.fromisoformat(market_date).isoformat() != market_date or not feed_scope:
            raise ValueError("ISO market date and explicit feed scope required")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("xb"):
            pass
        self.conn = sqlite3.connect(self.path)
        self.finished = False
        self.failed = False
        self.count = 0
        self.digest = hashlib.sha256()
        self.meta = dict(schema=SCHEMA, source=source, session_id=session_id,
                         market_date=market_date, feed_scope=feed_scope, state="incomplete")
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("CREATE TABLE metadata(value TEXT NOT NULL)")
            self.conn.execute("CREATE TABLE events(seq INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            self.conn.execute("INSERT INTO metadata VALUES (?)", (_json(self.meta),))
            self.conn.commit()
        except BaseException:
            self.conn.close()
            raise

    def append(self, event, *, received_at_utc, raw_fields, exchange_ts_raw=None,
               source_time_precision="unknown"):
        if self.finished or self.failed:
            raise ValueError("session already finished or failed")
        self.failed = True  # any append failure prevents a false closed marker
        _utc(received_at_utc)
        if not isinstance(raw_fields, dict) or not isinstance(source_time_precision, str) or not source_time_precision:
            raise ValueError("raw fields object and source precision required")
        if event.seq != self.count + 1:
            raise ValueError("unfiltered capture requires contiguous common sequence starting at 1")
        payload = _json(dict(event=asdict(event), received_at_utc=received_at_utc,
                             raw_fields=raw_fields, exchange_ts_raw=exchange_ts_raw,
                             source_time_precision=source_time_precision))
        self.order.accept(event)
        self.conn.execute("INSERT INTO events VALUES (?,?)", (event.seq, payload))
        self.digest.update(payload.encode("utf-8") + b"\n")
        self.count += 1
        self.failed = False

    def commit(self):
        self.conn.commit()

    def finish(self, *, close_ns):
        if self.finished or self.failed or type(close_ns) is not int or close_ns <= (self.order.last_received_ns or 0):
            raise ValueError("exclusive closing timestamp must follow all events")
        final = self.meta | dict(state="closed", close_ns=close_ns, event_count=self.count,
                                 payload_sha256=self.digest.hexdigest())
        self.conn.execute("UPDATE metadata SET value=?", (_json(final),))
        self.conn.commit()
        self.meta, self.finished = final, True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        # Committed batches survive but incomplete sessions cannot be replayed.
        self.conn.close()


@contextmanager
def read_raw_v2(path):
    """Yield (manifest, iterator of envelopes) from a closed read-only snapshot.

    Exhaust the iterator to verify its checksum/count. Any late error invalidates
    the whole run. A closed header is a producer claim, not external feed proof.
    No filtering precedes global sequence/integrity validation.
    """
    path = Path(path).resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
    iterator = None
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        rows = conn.execute("SELECT value FROM metadata").fetchall()
        if len(rows) != 1:
            raise ValueError("exactly one manifest required")
        meta = json.loads(rows[0][0])
        if meta.get("schema") != SCHEMA or meta.get("state") != "closed":
            raise ValueError("unsupported or incomplete raw dataset")
        if type(meta.get("close_ns")) is not int or meta["close_ns"] <= 0:
            raise ValueError("invalid close boundary")
        if type(meta.get("event_count")) is not int or meta["event_count"] < 0:
            raise ValueError("invalid event count")
        order = ReceiveOrderReplay(source=meta["source"], session_id=meta["session_id"], max_quote_age_ns=0)

        def records():
            digest = hashlib.sha256()
            count = 0
            cursor = conn.execute("SELECT seq,payload FROM events ORDER BY seq")
            try:
                for seq, payload in cursor:
                    count += 1
                    if seq != count:
                        raise ValueError("raw sequence gap")
                    envelope = json.loads(payload)
                    _utc(envelope["received_at_utc"])
                    if not isinstance(envelope["raw_fields"], dict):
                        raise ValueError("raw fields object required")
                    fields = envelope["event"]
                    for name in ("bid_sizes", "ask_sizes"):
                        if fields.get(name) is not None:
                            fields[name] = tuple(fields[name])
                    event = OrderedTick(**fields)
                    if event.seq != seq or event.received_ns >= meta["close_ns"]:
                        raise ValueError("record/header sequence or closing boundary mismatch")
                    order.accept(event)
                    digest.update(payload.encode("utf-8") + b"\n")
                    envelope["event"] = event
                    yield envelope
                if count != meta["event_count"] or digest.hexdigest() != meta.get("payload_sha256"):
                    raise ValueError("raw count/checksum mismatch")
            finally:
                cursor.close()
        iterator = records()
        yield dict(meta), iterator
    finally:
        if iterator is not None:
            iterator.close()
        conn.close()
