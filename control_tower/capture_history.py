"""Durable control history and explicit manager recovery; no transport or raw I/O.

Each handle has one serialized caller. Recovering a registered session fences old
handles in SQLite, not at an external peer. A future transport must also fence
dispatch: a command already returned to an old caller cannot be recalled here.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from control_tower.lifecycle import (
    CaptureLifecycle, ProcessIdentity, decode_report, decode_stop,
    encode_report, encode_stop,
)


class StaleManagerError(RuntimeError):
    """A newer manager owns this session; this handle may no longer act."""


class CaptureHistory:
    def __init__(self, root):
        self.path = Path(root) / "operations_state" / "capture_control.sqlite3"

    @contextmanager
    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=2)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported capture history schema")
            conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, identity_json TEXT NOT NULL,
                heartbeat_timeout_ns INTEGER NOT NULL, generation INTEGER NOT NULL,
                event_count INTEGER NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS events (
                session_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, recorded_at_utc TEXT NOT NULL,
                PRIMARY KEY(session_id, ordinal))""")
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
    def _append(conn, session_id, ordinal, kind, payload):
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?)", (
            session_id, ordinal, kind,
            json.dumps(payload, sort_keys=True, allow_nan=False),
            datetime.now(timezone.utc).isoformat(),
        ))

    def register(self, identity, *, heartbeat_timeout_ns):
        manager = CaptureLifecycle(identity, heartbeat_timeout_ns=heartbeat_timeout_ns)
        with self._write() as conn:
            conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (
                identity.session_id, json.dumps(asdict(identity), sort_keys=True),
                heartbeat_timeout_ns, 1, 0,
            ))
        return DurableCapture(self, manager, generation=1, event_count=0)

    def recover(self, identity):
        """Explicitly take ownership of known history, without resending any stop.

        Replays only this session's control messages, never capture events/raw DBs.
        Missing/corrupt/incompatible history raises; it is never repaired by guess.
        """
        if not isinstance(identity, ProcessIdentity):
            raise ValueError("explicit managed identity required")
        with self._write() as conn:
            row = conn.execute("SELECT identity_json, heartbeat_timeout_ns, generation, event_count "
                               "FROM sessions WHERE session_id=?", (identity.session_id,)).fetchone()
            if row is None:
                raise ValueError("session is not registered")
            if ProcessIdentity(**json.loads(row[0])) != identity:
                raise ValueError("process/session identity mismatch")
            manager = CaptureLifecycle(identity, heartbeat_timeout_ns=row[1])
            count = 0
            for ordinal, kind, payload in conn.execute(
                "SELECT ordinal, kind, payload FROM events WHERE session_id=? ORDER BY ordinal",
                (identity.session_id,),
            ):
                count += 1
                if ordinal != count:
                    raise ValueError("control history has a gap")
                _apply(manager, kind, json.loads(payload))
            if count != row[3]:
                raise ValueError("control history count mismatch")
            manager.manager_restarted()
            self._append(conn, identity.session_id, count + 1, "recovery", {})
            conn.execute("UPDATE sessions SET generation=?, event_count=? WHERE session_id=?",
                         (row[2] + 1, count + 1, identity.session_id))
        return DurableCapture(self, manager, generation=row[2] + 1, event_count=count + 1)


def _apply(manager, kind, payload):
    """Use the same reducer for live updates and replayed journal entries."""
    if kind == "report" and set(payload) == {"report", "now_ns"}:
        return manager.receive(decode_report(payload["report"]), now_ns=payload["now_ns"])
    if kind == "stop" and set(payload) == {"command", "now_ns", "timeout_ns"}:
        expected = decode_stop(payload["command"])
        actual = manager.request_stop(expected.request_id, expected.target,
                                      now_ns=payload["now_ns"], timeout_ns=payload["timeout_ns"])
        if actual != expected:
            raise ValueError("journal stop does not match report revision")
        return actual
    if kind == "loss" and set(payload) == {"reason", "now_ns"}:
        return manager.lost_contact(manager.identity, **payload)
    if kind == "recovery" and payload == {}:
        return manager.manager_restarted()
    raise ValueError("unsupported control history event")


class DurableCapture:
    """Commit before exposing a new report/command; failed writes leave memory intact.

    Stored stop_command is audit evidence, not a dispatch queue. Recovered commands
    cannot be returned by request_stop, even when delivery before the crash is unknown.
    """
    def __init__(self, history, manager, *, generation, event_count):
        self._history, self._manager = history, manager
        self._generation, self._event_count = generation, event_count
        self._recovered_command = manager.stop_command is not None

    @property
    def identity(self):
        return self._manager.identity

    @property
    def report(self):
        return self._manager.report

    @property
    def stop_command(self):
        return self._manager.stop_command

    def _check_owner(self, conn):
        row = conn.execute("SELECT generation, event_count FROM sessions WHERE session_id=?",
                           (self.identity.session_id,)).fetchone()
        if row != (self._generation, self._event_count):
            raise StaleManagerError("session ownership/history changed; recover explicitly")

    def _mutate(self, kind, payload):
        candidate = deepcopy(self._manager)
        result = _apply(candidate, kind, payload)
        with self._history._write() as conn:
            self._check_owner(conn)
            self._history._append(conn, self.identity.session_id, self._event_count + 1, kind, payload)
            conn.execute("UPDATE sessions SET event_count=event_count+1 WHERE session_id=?",
                         (self.identity.session_id,))
        self._manager = candidate
        self._event_count += 1
        return result

    def receive(self, report, *, now_ns):
        return self._mutate("report", {"report": encode_report(report), "now_ns": now_ns})

    def request_stop(self, request_id, target, *, now_ns, timeout_ns):
        if self._recovered_command:
            raise ValueError("recovered stop is audit-only; reconcile reports, do not resend")
        candidate = deepcopy(self._manager)
        command = candidate.request_stop(request_id, target, now_ns=now_ns, timeout_ns=timeout_ns)
        return self._mutate("stop", {"command": encode_stop(command), "now_ns": now_ns,
                                     "timeout_ns": timeout_ns})

    def lost_contact(self, identity, *, reason, now_ns):
        if identity != self.identity:
            raise ValueError("process/session identity mismatch")
        return self._mutate("loss", {"reason": reason, "now_ns": now_ns})

    def view(self, *, now_ns):
        # Read-only ownership check; viewing never writes or renews peer liveness.
        conn = sqlite3.connect(self._history.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        try:
            self._check_owner(conn)
        finally:
            conn.close()
        return self._manager.view(now_ns=now_ns)
