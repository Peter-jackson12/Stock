"""Durable local jobs. Only result inspection is executable in phase 1.

SQLite transactions serialize claims and cancellation; UI sessions own no worker.
Interrupted running jobs remain running/uncertain, never auto-requeued by time.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

MAX_JSON_BYTES = 32 * 1024
MAX_ERROR_CHARS = 2048
#: Unfinished statuses. Same set as the active_request dedup index below.
ACTIVE_STATUSES = ("planned", "queued", "running")


def bounded_json(value):
    if not isinstance(value, dict):
        raise ValueError("job payload/result must be an object")
    encoded = json.dumps(value, sort_keys=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("job payload/result exceeds 32 KiB limit")
    return encoded


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, root):
        self.path = Path(root) / "operations_state" / "jobs.sqlite3"

    @contextmanager
    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                fingerprint TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, owner TEXT, result TEXT, error TEXT)""")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS active_request ON jobs(fingerprint)
                WHERE status IN ('planned','queued','running')""")
            conn.execute("""CREATE TABLE IF NOT EXISTS replay_schedules (
                job_id TEXT PRIMARY KEY, due TEXT NOT NULL, expires TEXT NOT NULL,
                state TEXT NOT NULL, registration TEXT NOT NULL, error TEXT)""")
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _record(row):
        record = dict(row)
        record["payload"] = json.loads(record["payload"])
        record["result"] = json.loads(record["result"]) if record["result"] else None
        return record

    def _read(self, sql, parameters):
        """Read-only bounded query. A missing DB is an empty listing, not a new file."""
        if not self.path.exists():
            return []
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        conn.row_factory = sqlite3.Row
        try:
            return [self._record(row) for row in conn.execute(sql, parameters).fetchall()]
        finally:
            conn.close()

    @staticmethod
    def _bounded(limit):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("bounded job listing required")

    def recent(self, limit=30):
        self._bounded(limit)
        return self._read("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))

    def active(self, limit=100):
        """Unfinished jobs, oldest first, independent of the recent-history window.

        Dedup returns an existing unfinished job ID however old it is, so the UI
        must reach every such job without listing the whole history.
        """
        self._bounded(limit)
        return self._read(f"SELECT * FROM jobs WHERE status IN {ACTIVE_STATUSES!r} "
                          "ORDER BY created_at, id LIMIT ?", (limit,))

    def finished(self, limit=30):
        """Most recent terminal jobs (succeeded/failed/cancelled and any non-active status)."""
        self._bounded(limit)
        return self._read(f"SELECT * FROM jobs WHERE status NOT IN {ACTIVE_STATUSES!r} "
                          "ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))

    def get(self, job_id):
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("job id required")
        rows = self._read("SELECT * FROM jobs WHERE id=?", (job_id,))
        return rows[0] if rows else None

    def submit(self, kind, payload):
        if kind not in ("inspect_result", "replay_raw_v2") or not isinstance(payload, dict):
            raise ValueError("unsupported job")
        encoded = bounded_json(payload)
        fingerprint = hashlib.sha256((kind + encoded).encode()).hexdigest()
        with self._write() as conn:
            old = conn.execute("SELECT id FROM jobs WHERE fingerprint=? AND status IN ('planned','queued','running')",
                               (fingerprint,)).fetchone()
            if old:
                return old[0]
            job_id, now = uuid4().hex, utc_now()
            conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (job_id, kind, encoded, fingerprint, "queued" if kind == "inspect_result" else "planned",
                          now, now, None, None, None))
            return job_id

    def cancel(self, job_id):
        with self._write() as conn:
            changed = conn.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=? AND status IN ('planned','queued')",
                                (utc_now(), job_id)).rowcount == 1
            if changed:
                conn.execute("UPDATE replay_schedules SET state='cancelled' WHERE job_id=? AND state='pending'", (job_id,))
            return changed

    def claim_inspection(self, owner):
        if not owner:
            raise ValueError("worker identity required")
        with self._write() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE kind='inspect_result' AND status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE jobs SET status='running',owner=?,updated_at=? WHERE id=?",
                         (owner, utc_now(), row["id"]))
            job = self._record(row)
            return job | {"status": "running", "owner": owner}

    def queue_replay(self, job_id):
        with self._write() as conn:
            if conn.execute("SELECT 1 FROM replay_schedules WHERE job_id=?", (job_id,)).fetchone():
                raise ValueError("scheduled jobs cannot be manually requeued; cancel and create a new plan")
            changed = conn.execute("UPDATE jobs SET status='queued',updated_at=? WHERE id=? AND kind='replay_raw_v2' AND status='planned'",
                (utc_now(), job_id)).rowcount
            if changed != 1:
                raise ValueError("only an unexecuted replay plan can be queued")

    def claim_replay(self, job_id, owner):
        if not owner:
            raise ValueError("worker identity required")
        with self._write() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=? AND kind='replay_raw_v2' AND status='queued'", (job_id,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE jobs SET status='running',owner=?,updated_at=? WHERE id=?", (owner, utc_now(), job_id))
            return self._record(row) | {"status": "running", "owner": owner}

    def complete(self, job_id, owner, *, result=None, error=None):
        if (result is None) == (error is None):
            raise ValueError("exactly one worker outcome required")
        if error is not None and (not isinstance(error, str) or not error.strip()):
            raise ValueError("nonempty error required")
        encoded = bounded_json(result) if result is not None else None
        error = error[:MAX_ERROR_CHARS] if error is not None else None
        with self._write() as conn:
            changed = conn.execute("""UPDATE jobs SET status=?,result=?,error=?,updated_at=?
                WHERE id=? AND status='running' AND owner=?""",
                ("failed" if error is not None else "succeeded", encoded,
                 error, utc_now(), job_id, owner)).rowcount
            if changed != 1:
                raise ValueError("job is not running under this worker")
