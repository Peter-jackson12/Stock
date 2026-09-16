"""Offline raw-v2 SQLite contract prototype; not connected to the live collector.

Each record retains normalized input AND provider raw fields. Receipt sequence is
assigned by the caller at capture time. Missing/invalid source values belong in
raw_fields and nullable normalized fields; never synthesize a legacy sequence.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from engine.tick_ordering import OrderedTick, ReceiveOrderReplay


SCHEMA = "raw_v2_prototype_2"
SUPPORTED_SCHEMAS = {"raw_v2_prototype_1", SCHEMA}
MAX_MANIFEST_BYTES = 64 * 1024
CONTROL_TYPES = {"session_start", "session_note", "disconnect", "reconnect",
                 "parse_error", "queue_overflow", "callback_error"}


@dataclass(frozen=True)
class CaptureControl:
    source: str
    session_id: str
    seq: int
    received_ns: int
    control_type: str
    details: dict


def _common(event, meta, expected_seq, last_ns):
    if type(event.seq) is not int or event.seq != expected_seq:
        raise ValueError("contiguous common sequence required")
    if type(event.received_ns) is not int or event.received_ns < 0 or event.received_ns < last_ns:
        raise ValueError("invalid or reversed receipt clock")
    if (event.source, event.session_id) != (meta["source"], meta["session_id"]):
        raise ValueError("capture source/session mismatch")
    if isinstance(event, CaptureControl):
        if event.control_type not in CONTROL_TYPES or not isinstance(event.details, dict):
            raise ValueError("invalid control type/details")


def _load_json(text):
    def reject_constant(value):
        raise ValueError(f"nonfinite JSON value: {value}")
    return json.loads(text, parse_constant=reject_constant)


def _identity(meta):
    if not isinstance(meta, dict):
        raise ValueError("manifest object required")
    for key in ("source", "session_id", "market_date", "feed_scope"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            raise ValueError(f"nonempty manifest {key} text required")
    if date.fromisoformat(meta["market_date"]).isoformat() != meta["market_date"]:
        raise ValueError("ISO market date required")


def _envelope_fields(received_at_utc, raw_fields, exchange_ts_raw, source_time_precision):
    _utc(received_at_utc)
    if not isinstance(raw_fields, dict):
        raise ValueError("raw fields object required")
    if exchange_ts_raw is not None and not isinstance(exchange_ts_raw, str):
        raise ValueError("source time must be original text or null")
    if not isinstance(source_time_precision, str) or not source_time_precision.strip():
        raise ValueError("source time precision text required")


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
        _identity(dict(source=source, session_id=session_id, market_date=market_date, feed_scope=feed_scope))
        self.order = ReceiveOrderReplay(source=source, session_id=session_id, max_quote_age_ns=0)
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("xb"):
            pass
        self.conn = sqlite3.connect(self.path)
        self.finished = False
        self.failed = False
        self.count = 0
        self.last_ns = 0
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
        _envelope_fields(received_at_utc, raw_fields, exchange_ts_raw, source_time_precision)
        _common(event, self.meta, self.count + 1, self.last_ns)
        payload = _json(dict(event=asdict(event), received_at_utc=received_at_utc,
                             raw_fields=raw_fields, exchange_ts_raw=exchange_ts_raw,
                             source_time_precision=source_time_precision))
        if not isinstance(event, CaptureControl):
            self.order.accept(event)
        self.conn.execute("INSERT INTO events VALUES (?,?)", (event.seq, payload))
        self.digest.update(payload.encode("utf-8") + b"\n")
        self.count += 1
        self.last_ns = event.received_ns
        self.failed = False

    def commit(self):
        try:
            self.conn.commit()
        except BaseException:
            self.failed = True
            raise

    def finish(self, *, close_ns):
        if self.finished or self.failed or type(close_ns) is not int or close_ns <= self.last_ns:
            raise ValueError("exclusive closing timestamp must follow all events")
        final = self.meta | dict(state="closed", close_ns=close_ns, event_count=self.count,
                                 payload_sha256=self.digest.hexdigest())
        self.failed = True
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
        # Header-only consumers must not fetch unbounded malformed metadata.
        rows = conn.execute("SELECT substr(CAST(value AS BLOB),1,?) FROM metadata LIMIT 2",
                            (MAX_MANIFEST_BYTES + 1,)).fetchall()
        if len(rows) != 1:
            raise ValueError("exactly one manifest required")
        if rows[0][0] is None or len(rows[0][0]) > MAX_MANIFEST_BYTES:
            raise ValueError("raw manifest exceeds 64 KiB limit or is null")
        meta = _load_json(rows[0][0])
        _identity(meta)
        if meta.get("schema") not in SUPPORTED_SCHEMAS or meta.get("state") != "closed":
            raise ValueError("unsupported or incomplete raw dataset")
        if type(meta.get("close_ns")) is not int or meta["close_ns"] <= 0:
            raise ValueError("invalid close boundary")
        if type(meta.get("event_count")) is not int or meta["event_count"] < 0:
            raise ValueError("invalid event count")
        order = ReceiveOrderReplay(source=meta["source"], session_id=meta["session_id"], max_quote_age_ns=0)

        def records():
            digest = hashlib.sha256()
            count = 0
            last_ns = 0
            cursor = conn.execute("SELECT seq,payload FROM events ORDER BY seq")
            try:
                for seq, payload in cursor:
                    count += 1
                    if seq != count:
                        raise ValueError("raw sequence gap")
                    envelope = _load_json(payload)
                    if not isinstance(envelope, dict):
                        raise ValueError("record envelope object required")
                    _envelope_fields(envelope.get("received_at_utc"), envelope.get("raw_fields"),
                                     envelope.get("exchange_ts_raw"), envelope.get("source_time_precision"))
                    fields = envelope["event"]
                    if not isinstance(fields, dict):
                        raise ValueError("event object required")
                    for name in ("bid_sizes", "ask_sizes"):
                        if fields.get(name) is not None:
                            if not isinstance(fields[name], list):
                                raise ValueError("depth quantities must be arrays or null")
                            fields[name] = tuple(fields[name])
                    if "control_type" in fields:
                        if meta["schema"] != SCHEMA:
                            raise ValueError("control records require prototype 2")
                        event = CaptureControl(**fields)
                    else:
                        event = OrderedTick(**fields)
                    _common(event, meta, count, last_ns)
                    last_ns = event.received_ns
                    if event.seq != seq or event.received_ns >= meta["close_ns"]:
                        raise ValueError("record/header sequence or closing boundary mismatch")
                    if not isinstance(event, CaptureControl):
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
