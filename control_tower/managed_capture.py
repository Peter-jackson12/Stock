"""Local durable launch/stop mailbox for explicitly created small capture runs.

The checkout/Windows account is the trust boundary. This is not a remote API.
No PID-based kill, automatic adoption or replay of an accepted stop is provided.
"""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import time
from uuid import uuid4

from control_tower.lifecycle import ProcessIdentity, StopCommand, CaptureLifecycle, decode_report, encode_report
from control_tower.windows_process import WindowsProcess
from control_tower.storage_guard import require_disk_space

TOKEN_ENV = "STOCK_CAPTURE_LAUNCH_TOKEN"
ACTIVE = ("launching", "running", "stopping", "unknown")


def utc():
    return datetime.now(timezone.utc).isoformat()


def validate_plan(codes, duration, server):
    if (not isinstance(codes, list) or not 1 <= len(codes) <= 10
            or any(not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) for code in codes)
            or len(set(codes)) != len(codes)):
        raise ValueError("one to ten unique six-digit codes required")
    if type(duration) is not int or not 1 <= duration <= 300 or server not in ("mock", "live"):
        raise ValueError("explicit server and 1..300 second duration required")


class ManagedCaptures:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / "operations_state" / "managed_captures.sqlite3"

    @contextmanager
    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=0.1)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported managed capture schema")
            conn.execute("""CREATE TABLE IF NOT EXISTS launches (
                id TEXT PRIMARY KEY, plan TEXT NOT NULL, token_hash TEXT NOT NULL,
                allowed_executables TEXT NOT NULL, state TEXT NOT NULL, owner TEXT,
                identity TEXT, revision INTEGER, stop_id TEXT, stop_accepted INTEGER NOT NULL DEFAULT 0,
                final_report TEXT, error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL)""")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_capture ON launches((1)) WHERE state IN ('launching','running','stopping','unknown')")
            conn.execute("CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY, launch_id TEXT NOT NULL, kind TEXT NOT NULL, recorded TEXT NOT NULL)")
            conn.execute("PRAGMA user_version=1")
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
    def _event(conn, launch_id, kind):
        conn.execute("INSERT INTO audit(launch_id,kind,recorded) VALUES (?,?,?)", (launch_id, kind, utc()))

    def create(self, codes, duration, server, allowed_executables):
        validate_plan(codes, duration, server)
        if not allowed_executables or any(not Path(p).is_absolute() for p in allowed_executables):
            raise ValueError("explicit executable allowlist required")
        launch_id, token = uuid4().hex, secrets.token_hex(32)
        with self._write() as conn:
            conn.execute("""INSERT INTO launches(id,plan,token_hash,allowed_executables,state,created,updated)
                VALUES (?,?,?,?,?,?,?)""", (launch_id, json.dumps(dict(codes=codes, duration=duration, server=server)),
                hashlib.sha256(token.encode()).hexdigest(), json.dumps(allowed_executables), "launching", utc(), utc()))
            self._event(conn, launch_id, "launch_intent")
        return launch_id, token

    def get(self, launch_id=None):
        if not self.path.exists():
            return None
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM launches WHERE id=?" if launch_id else
                "SELECT * FROM launches ORDER BY rowid DESC LIMIT 1", (launch_id,) if launch_id else ()).fetchone()
            if row is None:
                return None
            result = dict(row)
            result.pop("token_hash")
            result.pop("allowed_executables")
            for key in ("plan", "owner", "identity"):
                result[key] = json.loads(result[key]) if result[key] else None
            return result
        finally:
            conn.close()

    def claim(self, launch_id, token, facts):
        with self._write() as conn:
            row = conn.execute("SELECT * FROM launches WHERE id=?", (launch_id,)).fetchone()
            if (row is None or row["state"] != "launching" or row["owner"] is not None
                    or not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row["token_hash"])):
                raise ValueError("launch capability invalid, consumed or cancelled")
            allowed = [os.path.normcase(os.path.normpath(p)) for p in json.loads(row["allowed_executables"])]
            if facts.python_bits != 32 or os.path.normcase(os.path.normpath(facts.executable)) not in allowed:
                raise ValueError("unexpected capture process executable/bitness")
            conn.execute("UPDATE launches SET owner=?,updated=? WHERE id=?", (json.dumps(asdict(facts), sort_keys=True), utc(), launch_id))
            self._event(conn, launch_id, "child_claimed")
            return json.loads(row["plan"])

    @staticmethod
    def _owned(conn, launch_id, facts):
        row = conn.execute("SELECT * FROM launches WHERE id=?", (launch_id,)).fetchone()
        if row is None or row["owner"] != json.dumps(asdict(facts), sort_keys=True) or row["state"] not in ACTIVE:
            raise ValueError("managed process ownership mismatch")
        return row

    def bind(self, launch_id, facts, report):
        if not facts.matches(report.identity) or report.revision != 1 or report.state != "starting":
            raise ValueError("new capture report must match claimed process")
        with self._write() as conn:
            row = self._owned(conn, launch_id, facts)
            if row["identity"] is not None or row["stop_accepted"]:
                raise ValueError("session already bound or cancelled")
            if report.identity.server != json.loads(row["plan"])["server"]:
                raise ValueError("capture server differs from stored plan")
            conn.execute("UPDATE launches SET identity=?,revision=?,state='running',updated=? WHERE id=?",
                (json.dumps(asdict(report.identity)), report.revision, utc(), launch_id))
            self._event(conn, launch_id, "session_bound")

    def request_stop(self, launch_id):
        with self._write() as conn:
            row = conn.execute("SELECT * FROM launches WHERE id=?", (launch_id,)).fetchone()
            if row is None or row["state"] not in ACTIVE:
                raise ValueError("no active managed capture")
            if row["stop_id"]:
                return row["stop_id"]
            request_id = uuid4().hex
            # Cancellation before claim revokes the launch capability. A delayed
            # child cannot pass claim and create an OCX instance afterward.
            state = "cancelled" if row["owner"] is None else "stopping"
            conn.execute("UPDATE launches SET stop_id=?,state=?,updated=? WHERE id=?", (request_id, state, utc(), launch_id))
            self._event(conn, launch_id, "cancel_unclaimed" if state == "cancelled" else "stop_requested")
            return request_id

    def poll(self, launch_id, facts, *, heartbeat=False):
        # Most Qt timer ticks need only a small read, not a durable transaction.
        if not heartbeat:
            current = self.get(launch_id)
            if current is None or current["owner"] != asdict(facts) or current["state"] not in ACTIVE:
                raise ValueError("managed process ownership mismatch")
            if not current["stop_id"] or current["stop_accepted"]:
                return None
        with self._write() as conn:
            row = self._owned(conn, launch_id, facts)
            if heartbeat:
                conn.execute("UPDATE launches SET updated=? WHERE id=?", (utc(), launch_id))
            if not row["stop_id"] or row["stop_accepted"]:
                return None
            conn.execute("UPDATE launches SET stop_accepted=1,updated=? WHERE id=?", (utc(), launch_id))
            self._event(conn, launch_id, "stop_accepted")
            identity = ProcessIdentity(**json.loads(row["identity"])) if row["identity"] else None
            return (row["stop_id"], None if identity is None else StopCommand(row["stop_id"], identity, row["revision"]))

    def finish(self, launch_id, facts, report=None, error=None):
        with self._write() as conn:
            row = self._owned(conn, launch_id, facts)
            state = "failed"
            if report is not None:
                if row["identity"] is None or report.identity != ProcessIdentity(**json.loads(row["identity"])):
                    raise ValueError("final capture identity mismatch")
                if report.state != "closed":
                    raise ValueError("explicit closed report required")
                expected = row["stop_id"] if row["stop_accepted"] else None
                if report.stop_request_id != expected:
                    raise ValueError("final stop acknowledgement mismatch")
                state = "closed" if not error else "failed"
            elif row["identity"] is None and row["stop_accepted"]:
                state = "cancelled"
            conn.execute("UPDATE launches SET state=?,final_report=?,error=?,updated=? WHERE id=?",
                (state, None if report is None else encode_report(report), error, utc(), launch_id))
            self._event(conn, launch_id, state)

    def spawn_uncertain(self, launch_id, error):
        with self._write() as conn:
            conn.execute("UPDATE launches SET state='unknown',error=?,updated=? WHERE id=? AND owner IS NULL AND state='launching'",
                         (str(error)[:1024], utc(), launch_id))
            self._event(conn, launch_id, "spawn_outcome_unknown")

    def reconcile(self, launch_id, *, process_factory=WindowsProcess):
        """Resolve a dead owned child from bounded history; never repeat its stop."""
        from collector.kiwoom.collector_lease import CollectorLease
        from control_tower.windows_process import ProcessFacts
        # A new compliant collector cannot start during reconciliation.
        with CollectorLease(self.root), self._write() as conn:
            row = conn.execute("SELECT * FROM launches WHERE id=?", (launch_id,)).fetchone()
            if row is None or row["state"] not in ACTIVE or row["owner"] is None:
                raise ValueError("claimed unresolved launch required")
            facts = ProcessFacts(**json.loads(row["owner"]))
            try:
                with process_factory(facts.pid) as process:
                    if process.facts == facts:
                        raise ValueError("owned process still alive; do not reconcile as stopped")
            except OSError as exc:
                # ERROR_INVALID_PARAMETER is OpenProcess's nonexistent PID outcome.
                # Access denied and other inspection errors are not death evidence.
                if getattr(exc, "winerror", None) != 87:
                    raise
            final = None
            if row["identity"] is not None:
                identity = ProcessIdentity(**json.loads(row["identity"]))
                if not re.fullmatch(r"[0-9a-f]{32}", identity.session_id):
                    raise ValueError("unexpected operational session id")
                path = self.root / "operations_state/capture_sessions" / identity.session_id / "operations_state/peer_reports.sqlite3"
                if path.exists():
                    history = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
                    try:
                        # This operational adapter emits exactly start/drain/closed.
                        rows = history.execute("SELECT substr(payload,1,32769) FROM reports ORDER BY revision LIMIT 4").fetchall()
                    finally:
                        history.close()
                    if len(rows) > 3:
                        raise ValueError("unexpected operational history length")
                    lifecycle = CaptureLifecycle(identity, heartbeat_timeout_ns=1)
                    for index, (payload,) in enumerate(rows):
                        report = decode_report(payload)
                        lifecycle.receive(report, now_ns=index)
                        if index == 0 and row["stop_accepted"]:
                            lifecycle.request_stop(row["stop_id"], identity, now_ns=0, timeout_ns=1)
                    if lifecycle.report is not None and lifecycle.report.state == "closed":
                        final = lifecycle.report
            state = "closed" if final is not None else "failed"
            conn.execute("UPDATE launches SET state=?,final_report=?,error=?,updated=? WHERE id=?",
                (state, encode_report(final) if final else None,
                 None if final else "process exited without a complete durable close report", utc(), launch_id))
            self._event(conn, launch_id, "reconciled_" + state)
            return state


def start_managed_capture(root, codes, duration, server, *, popen=subprocess.Popen):
    """Only the fixed collector script; no user-supplied executable or shell."""
    root = Path(root).resolve()
    validate_plan(codes, duration, server)
    require_disk_space(root)
    python = root / ".venv32" / "Scripts" / "python.exe"
    script = root / "collector" / "kiwoom" / "kiwoom_universe_logger.py"
    config = root / ".venv32" / "pyvenv.cfg"
    if not python.is_file() or not script.is_file() or not config.is_file():
        raise ValueError("configured 32-bit collector environment unavailable")
    settings = dict(line.split(" = ", 1) for line in config.read_text().splitlines() if " = " in line)
    base = Path(settings["home"]) / "python.exe"
    if not base.is_absolute() or not base.is_file():
        raise ValueError("32-bit base interpreter unavailable")
    store = ManagedCaptures(root)
    launch_id, token = store.create(codes, duration, server, [str(python), str(base)])
    environment = os.environ.copy()
    environment[TOKEN_ENV] = token
    try:
        with (store.path.parent / f"capture_launch_{launch_id}.log").open("ab") as log:
            popen([str(python), str(script), "--managed-launch", launch_id], cwd=root, env=environment,
                  stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except BaseException as exc:
        store.spawn_uncertain(launch_id, exc)
        raise
    return launch_id


class ManagedCapturePeer:
    def __init__(self, root, launch_id, token):
        self.store, self.launch_id = ManagedCaptures(root), launch_id
        with WindowsProcess(os.getpid()) as process:
            self.facts = process.facts
        self.plan = self.store.claim(launch_id, token, self.facts)
        self.stop = None
        self._heartbeat = 0
        self.finished = False

    def bind(self, report):
        self.store.bind(self.launch_id, self.facts, report)

    def poll(self):
        now = time.monotonic()
        try:
            stop = self.store.poll(self.launch_id, self.facts, heartbeat=now - self._heartbeat >= 5)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                return self.stop is not None  # Retry an unread mailbox, never an accepted action.
            raise
        if now - self._heartbeat >= 5:
            self._heartbeat = now
        if stop is not None:
            self.stop = stop
        return self.stop is not None

    def finish(self, report=None, error=None):
        self.store.finish(self.launch_id, self.facts, report, error)
        self.finished = True
