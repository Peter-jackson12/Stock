from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from control_tower.jobs import JobStore
from control_tower.history_maintenance import archive_history

NOW = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)


def completed(root):
    store = JobStore(root)
    job = store.submit("inspect_result", {"path": "fixture"})
    store.claim_inspection("worker")
    store.complete(job, "worker", result={"synthetic": True})
    return store, job


def test_backup_keeps_original_and_checkpoint_is_reported(tmp_path):
    store, job = completed(tmp_path)
    result = archive_history(tmp_path, "jobs", now=NOW)
    archive = Path(result["archive"])
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == result["sha256"]
    with sqlite3.connect(archive) as conn:
        assert conn.execute("SELECT id FROM jobs").fetchone()[0] == job
    assert store.recent()[0]["id"] == job
    assert result["removed_rows"] == 0 and result["checkpoint"][0] == 0


def test_rotation_removes_only_old_terminal_rows_after_verified_archive(tmp_path):
    store, job = completed(tmp_path)
    with store._write() as conn:
        conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", ((NOW - timedelta(days=90)).isoformat(), job))
    pending = store.submit("inspect_result", {"path": "pending"})
    result = archive_history(tmp_path, "jobs", now=NOW, prune_before=NOW - timedelta(days=31))
    assert result["removed_rows"] == 1
    assert [r["id"] for r in store.recent()] == [pending]
    with sqlite3.connect(result["archive"]) as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    with sqlite3.connect(store.path) as conn:
        assert json.loads(conn.execute("SELECT receipt FROM history_archives").fetchone()[0])["removed_rows"] == 1


def test_running_and_recent_rotation_are_rejected(tmp_path):
    store, _ = completed(tmp_path)
    with pytest.raises(ValueError):
        archive_history(tmp_path, "jobs", now=NOW, prune_before=NOW - timedelta(days=1))
    store.submit("inspect_result", {"path": "running"})
    store.claim_inspection("still-running")
    with pytest.raises(ValueError, match="unresolved"):
        archive_history(tmp_path, "jobs", now=NOW)
    assert not (tmp_path / "operations_state/history_archives").exists()


def test_raw_cannot_be_a_maintenance_target(tmp_path):
    with pytest.raises(ValueError):
        archive_history(tmp_path, "raw", now=NOW)


def test_archive_sync_failure_never_removes_live_history(tmp_path, monkeypatch):
    from control_tower import history_maintenance
    store, job = completed(tmp_path)
    with store._write() as conn:
        conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", ((NOW - timedelta(days=90)).isoformat(), job))
    def fail(fd):
        raise OSError("archive sync failed")
    monkeypatch.setattr(history_maintenance.os, "fsync", fail)
    with pytest.raises(OSError):
        archive_history(tmp_path, "jobs", now=NOW, prune_before=NOW - timedelta(days=31))
    assert store.recent()[0]["id"] == job
