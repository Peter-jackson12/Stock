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

    def recent(self, limit=30):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("bounded job listing required")
        if not self.path.exists():
            return []
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._record(row) for row in rows]
        finally:
            conn.close()

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
            return conn.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=? AND status IN ('planned','queued')",
                                (utc_now(), job_id)).rowcount == 1

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
