"""Offline control-history backup/checkpoint; raw datasets are never targets."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from collector.kiwoom.collector_lease import CollectorLease
from control_tower.offline_worker import require_offline
from control_tower.storage_guard import require_disk_space, MIN_FREE_BYTES

TARGETS = {"jobs": "jobs.sqlite3", "managed_captures": "managed_captures.sqlite3"}
MAX_BYTES = 64 * 1024 * 1024


def archive_history(root, kind, *, prune_before=None, now=None):
    root = Path(root).resolve()
    if kind not in TARGETS:
        raise ValueError("only jobs and managed-capture history can be maintained")
    now = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    if prune_before is not None:
        if prune_before.tzinfo is None or prune_before > now - timedelta(days=30):
            raise ValueError("rotation must retain at least 30 days")
        prune_before = prune_before.astimezone(timezone.utc).isoformat()
    require_offline(root, now)
    source = root / "operations_state" / TARGETS[kind]
    if not source.is_file() or source.resolve() != source:
        raise ValueError("existing non-redirected control history required")
    with CollectorLease(root):
        require_offline(root, now)
        total = sum(p.stat().st_size for p in (source, Path(str(source) + "-wal")) if p.exists())
        if total > MAX_BYTES:
            raise ValueError("maintenance input exceeds 64 MiB; use separately reviewed offline procedure")
        require_disk_space(root, minimum=MIN_FREE_BYTES + total * 2)
        conn = sqlite3.connect(source, timeout=.25)
        try:
            conn.execute("BEGIN IMMEDIATE")
            table, stamp = ("jobs", "updated_at") if kind == "jobs" else ("launches", "updated")
            state_column = "status" if kind == "jobs" else "state"
            busy = ("running",) if kind == "jobs" else ("launching", "running", "stopping", "unknown")
            marks = ",".join("?" for _ in busy)
            if conn.execute(f"SELECT 1 FROM {table} WHERE {state_column} IN ({marks}) LIMIT 1", busy).fetchone():
                raise ValueError("unresolved execution prevents history maintenance")
            directory = root / "operations_state/history_archives" / uuid4().hex
            directory.mkdir(parents=True, exist_ok=False)
            archive = directory / source.name
            deadline = time.monotonic() + 5
            def progress(status, remaining, total_pages):
                if time.monotonic() > deadline:
                    raise TimeoutError("history backup exceeded cooperative five-second budget")
            reader = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=.25)
            backup = sqlite3.connect(archive)
            try:
                reader.backup(backup, pages=64, progress=progress, sleep=.01)
                backup.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                if backup.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValueError("archive integrity check failed")
            finally:
                backup.close()
                reader.close()
            with archive.open("r+b") as stream:
                content = stream.read(MAX_BYTES + 1)
                if len(content) > MAX_BYTES:
                    raise ValueError("archive exceeds 64 MiB budget")
                digest = hashlib.sha256(content).hexdigest()
                os.fsync(stream.fileno())
            receipt = dict(schema="control_history_archive_v1", kind=kind, archived_at=now.isoformat(),
                archive=str(archive), sha256=digest, prune_before=prune_before)
            # Publish verified copy before any live-row rotation. A crash before
            # SQL commit preserves duplicates, never loses the original evidence.
            receipt_path = directory / "archive.json"
            with receipt_path.open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            receipt["removed_rows"] = 0
            if prune_before is not None:
                where = f"{state_column} IN ('closed','failed','cancelled','succeeded') AND julianday({stamp}) < julianday(?)"
                if kind == "managed_captures":
                    conn.execute(f"DELETE FROM audit WHERE launch_id IN (SELECT id FROM launches WHERE {where})", (prune_before,))
                elif conn.execute("SELECT 1 FROM sqlite_master WHERE name='replay_schedules'").fetchone():
                    conn.execute(f"DELETE FROM replay_schedules WHERE job_id IN (SELECT id FROM jobs WHERE {where})", (prune_before,))
                receipt["removed_rows"] = conn.execute(f"DELETE FROM {table} WHERE {where}", (prune_before,)).rowcount
            conn.execute("CREATE TABLE IF NOT EXISTS history_archives (id TEXT PRIMARY KEY, receipt TEXT NOT NULL)")
            conn.execute("INSERT INTO history_archives VALUES (?,?)", (directory.name, json.dumps(receipt)))
            conn.commit()
            receipt["checkpoint"] = list(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            return receipt
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
