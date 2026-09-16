"""Peer report outbox and bounded replay pages; never a stop execution queue.

Store immutable reports before sending. Reading this journal proves history,
not current process liveness or raw integrity. Lifecycle validation remains in
the manager's reducer. One journal is scoped to one explicitly known session.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3

from control_tower.lifecycle import MAX_MESSAGE_BYTES, ProcessIdentity, decode_report, encode_report
from control_tower.history_limits import DEFAULT_RECORDS, DEFAULT_BYTES, HistoryLimitError, validate_limits

MAX_PAGE = 128
SCHEMA = "capture_replay_v1"


def _number(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("invalid replay integer")


@dataclass(frozen=True)
class ReplayRequest:
    identity: ProcessIdentity
    after_revision: int
    limit: int = MAX_PAGE

    def __post_init__(self):
        if not isinstance(self.identity, ProcessIdentity):
            raise ValueError("replay identity required")
        _number(self.after_revision)
        _number(self.limit, 1)
        if self.limit > MAX_PAGE:
            raise ValueError("replay page exceeds limit")


@dataclass(frozen=True)
class ReplayPage:
    identity: ProcessIdentity
    after_revision: int
    head_revision: int
    reports: tuple

    def __post_init__(self):
        ReplayRequest(self.identity, self.after_revision)
        _number(self.head_revision)
        if self.head_revision < self.after_revision or not isinstance(self.reports, tuple) or len(self.reports) > MAX_PAGE:
            raise ValueError("invalid replay page bounds")
        for revision, report in enumerate(self.reports, self.after_revision + 1):
            if report.identity != self.identity or report.revision != revision or revision > self.head_revision:
                raise ValueError("replay identity or contiguous revision mismatch")
        if not self.reports and self.head_revision != self.after_revision:
            raise ValueError("replay page has missing reports")


def _encode(kind, payload):
    text = json.dumps(dict(schema=SCHEMA, kind=kind, **payload), sort_keys=True, allow_nan=False)
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("replay frame exceeds size limit")
    return text


def _decode(text, kind, keys):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("bounded replay text required")
    data = json.loads(text)
    if not isinstance(data, dict) or data.pop("schema", None) != SCHEMA or data.pop("kind", None) != kind or set(data) != keys:
        raise ValueError("invalid replay schema or fields")
    try:
        data["identity"] = ProcessIdentity(**data["identity"])
    except (TypeError, KeyError) as exc:
        raise ValueError("invalid replay identity") from exc
    return data


def encode_request(request):
    return _encode("request", asdict(request))


def decode_request(text):
    return ReplayRequest(**_decode(text, "request", {"identity", "after_revision", "limit"}))


def encode_page(page):
    return _encode("page", dict(identity=asdict(page.identity), after_revision=page.after_revision,
        head_revision=page.head_revision, reports=[json.loads(encode_report(r)) for r in page.reports]))


def decode_page(text):
    data = _decode(text, "page", {"identity", "after_revision", "head_revision", "reports"})
    if not isinstance(data["reports"], list) or len(data["reports"]) > MAX_PAGE:
        raise ValueError("bounded report list required")
    data["reports"] = tuple(decode_report(json.dumps(r)) for r in data["reports"])
    return ReplayPage(**data)


class ReportJournal:
    def __init__(self, root, identity, *, max_reports=DEFAULT_RECORDS, max_bytes=DEFAULT_BYTES):
        if not isinstance(identity, ProcessIdentity):
            raise ValueError("explicit journal identity required")
        self.identity = identity
        validate_limits(max_reports, max_bytes)
        self.max_reports, self.max_bytes = max_reports, max_bytes
        self.path = Path(root) / "operations_state" / "peer_reports.sqlite3"
        with self._connect(write=True) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("unsupported peer journal schema")
            conn.execute("CREATE TABLE IF NOT EXISTS peer (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL, head INTEGER NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS reports (revision INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            value = json.dumps(asdict(identity), sort_keys=True)
            conn.execute("INSERT OR IGNORE INTO peer VALUES (1,?,0)", (value,))
            if conn.execute("SELECT identity FROM peer WHERE id=1").fetchone()[0] != value:
                raise ValueError("journal process/session identity mismatch")
            if version < 2:
                head = conn.execute("SELECT head FROM peer WHERE id=1").fetchone()[0]
                if type(head) is not int or not 0 <= head <= max_reports:
                    raise HistoryLimitError("existing peer history exceeds record budget")
                # Bound migration by row count and stored payload sizes; do not
                # decode/rewrite reports or silently truncate old evidence.
                used, count = 0, 0
                for revision, size in conn.execute(
                        "SELECT revision,length(CAST(payload AS BLOB)) FROM reports ORDER BY revision LIMIT ?",
                        (max_reports + 1,)):
                    count += 1
                    if count > max_reports or size > MAX_MESSAGE_BYTES or used + size > max_bytes:
                        raise HistoryLimitError("existing peer history exceeds byte/record budget")
                    if revision != count:
                        raise ValueError("existing peer history has a gap")
                    used += size
                if count != head:
                    raise ValueError("existing peer history count mismatch")
                conn.execute("CREATE TABLE quota (id INTEGER PRIMARY KEY CHECK(id=1), max_reports INTEGER NOT NULL, max_bytes INTEGER NOT NULL, used_bytes INTEGER NOT NULL)")
                conn.execute("INSERT INTO quota VALUES (1,?,?,?)", (max_reports, max_bytes, used))
                conn.execute("PRAGMA user_version=2")
            if conn.execute("SELECT max_reports,max_bytes FROM quota WHERE id=1").fetchone() != (max_reports, max_bytes):
                raise ValueError("peer journal limits differ from persisted policy")

    @contextmanager
    def _connect(self, *, write=False):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=0.5)
        else:
            conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        try:
            if write:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append(self, report):
        payload = encode_report(report)
        if report.identity != self.identity:
            raise ValueError("journal report identity mismatch")
        with self._connect(write=True) as conn:
            head = self._head(conn)
            if report.revision <= head:
                row = conn.execute("SELECT payload FROM reports WHERE revision=?", (report.revision,)).fetchone()
                if row != (payload,):
                    raise ValueError("immutable report conflict")
                return False
            if report.revision != head + 1:
                raise ValueError("contiguous report revision required")
            used = conn.execute("SELECT used_bytes FROM quota WHERE id=1").fetchone()[0]
            size = len(payload.encode("utf-8"))
            if head >= self.max_reports or used + size > self.max_bytes:
                raise HistoryLimitError("peer report history full; preserve journal and stop publication")
            conn.execute("INSERT INTO reports VALUES (?,?)", (report.revision, payload))
            conn.execute("UPDATE peer SET head=? WHERE id=1", (report.revision,))
            conn.execute("UPDATE quota SET used_bytes=used_bytes+? WHERE id=1", (size,))
        return True

    def _head(self, conn):
        row = conn.execute("SELECT identity,head FROM peer WHERE id=1").fetchone()
        if (row is None or row[0] != json.dumps(asdict(self.identity), sort_keys=True)
                or conn.execute("PRAGMA user_version").fetchone()[0] != 2):
            raise ValueError("journal identity/schema changed")
        quota = conn.execute("SELECT max_reports,max_bytes,used_bytes FROM quota WHERE id=1").fetchone()
        if (quota is None or quota[:2] != (self.max_reports, self.max_bytes)
                or type(quota[2]) is not int or not 0 <= quota[2] <= self.max_bytes):
            raise ValueError("journal quota changed or invalid")
        _number(row[1])
        return row[1]

    def publish(self, report, channel):
        self.append(report)  # A failed write must never expose an unjournaled report.
        channel.send_report(report)

    def page(self, request):
        if request.identity != self.identity:
            raise ValueError("replay target mismatch")
        with self._connect() as conn:
            head = self._head(conn)
            if request.after_revision > head:
                raise ValueError("manager cursor exceeds peer history")
            reports = []
            for revision, payload in conn.execute(
                "SELECT revision,payload FROM reports WHERE revision>? ORDER BY revision LIMIT ?",
                (request.after_revision, request.limit),
            ):
                report = decode_report(payload)
                if revision != report.revision:
                    raise ValueError("journal row revision mismatch")
                candidate = ReplayPage(self.identity, request.after_revision, head, tuple(reports + [report]))
                try:
                    encode_page(candidate)
                except ValueError:
                    if not reports:
                        raise  # Never silently skip an untransmittable report.
                    break
                reports.append(report)
            return ReplayPage(self.identity, request.after_revision, head, tuple(reports))

    def serve_replay(self, channel):
        try:
            channel.send_replay_page(self.page(channel.receive_replay_request()))
        except BaseException:
            channel.close()
            raise
