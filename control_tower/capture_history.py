"""Durable control history and explicit manager recovery; no transport or raw I/O.

Each handle has one serialized caller. Guarded dispatch serializes local sender
acceptance against recovery. It cannot recall bytes already sent or authenticate
a remote peer. Future transports must bound sends and preserve session identity.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from control_tower.history_limits import (
    DEFAULT_RECORDS, DEFAULT_BYTES, HistoryLimitError, validate_limits, validate_timeout,
)

from control_tower.lifecycle import (
    CaptureLifecycle, ProcessIdentity, decode_report, decode_stop,
    encode_report, encode_stop,
)

MAX_RECONCILE_REPORTS = 128


class StaleManagerError(RuntimeError):
    """A newer manager owns this session; this handle may no longer act."""


class CaptureHistory:
    def __init__(self, root, *, replay_max_events=DEFAULT_RECORDS, replay_max_bytes=DEFAULT_BYTES,
                 replay_timeout=2.0):
        validate_limits(replay_max_events, replay_max_bytes)
        validate_timeout(replay_timeout)
        self.replay_max_events, self.replay_max_bytes = replay_max_events, replay_max_bytes
        self.replay_timeout = replay_timeout
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
            if type(row[3]) is not int or not 0 <= row[3] <= self.replay_max_events:
                raise HistoryLimitError("manager history exceeds replay event budget")
            manager = CaptureLifecycle(identity, heartbeat_timeout_ns=row[1])
            count, byte_count = 0, 0
            deadline = time.monotonic() + self.replay_timeout
            for ordinal, kind, payload, size in conn.execute(
                "SELECT ordinal,kind,CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload ELSE NULL END,"
                "length(CAST(payload AS BLOB)) FROM events WHERE session_id=? ORDER BY ordinal LIMIT ?",
                (min(self.replay_max_bytes, 8 * 1024 * 1024), identity.session_id, self.replay_max_events + 1),
            ):
                count += 1
                byte_count += size
                if (count > self.replay_max_events or payload is None or byte_count > self.replay_max_bytes
                        or time.monotonic() >= deadline):
                    raise HistoryLimitError("manager replay exceeded event/byte/time budget")
                if ordinal != count:
                    raise ValueError("control history has a gap")
                _apply(manager, kind, json.loads(payload))
            if time.monotonic() >= deadline:
                raise HistoryLimitError("manager replay exceeded time budget")
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
    if kind == "dispatch" and set(payload) == {"command", "now_ns"}:
        return manager.record_dispatch(decode_stop(payload["command"]), now_ns=payload["now_ns"])
    if kind == "reconcile" and set(payload) == {"reports", "now_ns"}:
        reports = payload["reports"]
        if not isinstance(reports, list) or not 1 <= len(reports) <= MAX_RECONCILE_REPORTS:
            raise ValueError("bounded nonempty report batch required")
        for text in reports:
            report = decode_report(text)
            expected = 1 if manager.report is None else manager.report.revision + 1
            if report.revision != expected:
                raise ValueError("reconciliation requires the next contiguous revision")
            manager.receive(report, now_ns=payload["now_ns"])
        # Historical delivery is not a fresh heartbeat, even if it fills the gap.
        manager.lost_contact(manager.identity, reason="history reconciled; fresh heartbeat required",
                             now_ns=payload["now_ns"])
        return len(reports)
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

    def reconcile(self, reports, *, now_ns):
        """Atomically validate/store a bounded page of missing historical reports.

        Call receive only for subsequent fresh reports from the live channel.
        Duplicates/gaps invalidate the whole page. This method never dispatches stop.
        """
        if not isinstance(reports, (list, tuple)) or not 1 <= len(reports) <= MAX_RECONCILE_REPORTS:
            raise ValueError("bounded nonempty report batch required")
        return self._mutate("reconcile", {"reports": [encode_report(r) for r in reports], "now_ns": now_ns})

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

    def dispatch_stop(self, sender, *, now_ns):
        """Commit intent, then fence a bounded synchronous sender against takeover.

        sender(command) must not call back into this store; network implementations
        need their own short timeout. Its return value is not a completion report.
        An attempted dispatch is never retried, including failure before delivery.
        Local SQLite ownership cannot retract a command already accepted by a peer.
        """
        if self._recovered_command:
            raise ValueError("recovered stop is audit-only; reconcile reports, do not resend")
        command = self.stop_command
        if command is None or not callable(sender):
            raise ValueError("stored stop and callable sender required")
        self._mutate("dispatch", {"command": encode_stop(command), "now_ns": now_ns})
        try:
            with self._history._write() as conn:
                self._check_owner(conn)
                sender(command)
        except BaseException:
            try:
                self.lost_contact(self.identity, reason="dispatch outcome unknown", now_ns=now_ns)
            except (StaleManagerError, sqlite3.Error):
                pass  # persisted intent still prevents retry after recovery
            raise

    def view(self, *, now_ns):
        # Read-only ownership check; viewing never writes or renews peer liveness.
        conn = sqlite3.connect(self._history.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        try:
            self._check_owner(conn)
        finally:
            conn.close()
        return self._manager.view(now_ns=now_ns)
