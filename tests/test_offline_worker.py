from datetime import datetime
import json
import sqlite3

import pytest

from control_tower.jobs import JobStore
from control_tower.offline_worker import run_replay_job, require_offline, KST
from collector.kiwoom.collector_lease import CollectorLease
from tests.test_control_tower import raw_file, plan

AFTER = datetime(2026, 9, 16, 18, tzinfo=KST)


def test_small_closed_input_verified_and_replayed(tmp_path):
    path = raw_file(tmp_path)
    job = plan(tmp_path, path)
    store = JobStore(tmp_path)
    store.queue_replay(job)
    assert run_replay_job(tmp_path, job, now=AFTER) == job
    record = store.recent()[0]
    assert record["status"] == "succeeded", record
    result = json.loads(open(record["result"]["result_path"], encoding="utf-8").read())
    assert result["input_complete"] is True
    assert record["result"]["integrity"] == "verified"
    assert run_replay_job(tmp_path, job, now=AFTER) is None
    with pytest.raises(ValueError):
        store.queue_replay(job)


def test_market_hours_and_collector_lease_block_before_claim(tmp_path):
    job = plan(tmp_path, raw_file(tmp_path))
    store = JobStore(tmp_path)
    store.queue_replay(job)
    with pytest.raises(ValueError):
        run_replay_job(tmp_path, job, now=AFTER.replace(hour=10))
    with CollectorLease(tmp_path), pytest.raises(RuntimeError):
        run_replay_job(tmp_path, job, now=AFTER)
    assert store.recent()[0]["status"] == "queued"


def test_manifest_replacement_fails_without_replay(tmp_path):
    path = raw_file(tmp_path)
    job = plan(tmp_path, path)
    with sqlite3.connect(path) as conn:
        manifest = json.loads(conn.execute("SELECT value FROM metadata").fetchone()[0])
        manifest["session_id"] = "replaced"
        conn.execute("UPDATE metadata SET value=?", (json.dumps(manifest),))
    store = JobStore(tmp_path)
    store.queue_replay(job)
    run_replay_job(tmp_path, job, now=AFTER)
    assert store.recent()[0]["status"] == "failed"
    assert "changed" in store.recent()[0]["error"]
    assert not (tmp_path / "research_runs").exists()


def test_running_job_is_never_retried(tmp_path):
    job = plan(tmp_path, raw_file(tmp_path))
    store = JobStore(tmp_path)
    store.queue_replay(job)
    store.claim_replay(job, "lost-worker")
    assert run_replay_job(tmp_path, job, now=AFTER) is None
    assert store.recent()[0]["owner"] == "lost-worker"


def test_checksum_corruption_never_publishes_success(tmp_path):
    path = raw_file(tmp_path)
    # Empty fixture still has a persisted SHA256; corrupt it before plan creation.
    with sqlite3.connect(path) as conn:
        manifest = json.loads(conn.execute("SELECT value FROM metadata").fetchone()[0])
        manifest["payload_sha256"] = "0" * 64
        conn.execute("UPDATE metadata SET value=?", (json.dumps(manifest),))
    job = plan(tmp_path, path)
    store = JobStore(tmp_path)
    store.queue_replay(job)
    run_replay_job(tmp_path, job, now=AFTER)
    assert store.recent()[0]["status"] == "failed"
    assert not (tmp_path / "research_runs").exists()


def test_symlink_output_escape_rejected(tmp_path):
    path = raw_file(tmp_path)
    job = plan(tmp_path, path)
    store = JobStore(tmp_path)
    with store._write() as conn:
        payload = json.loads(conn.execute("SELECT payload FROM jobs WHERE id=?", (job,)).fetchone()[0])
        argv = payload["argv"]
        argv[argv.index("--output-root") + 1] = str(tmp_path.parent / "outside")
        conn.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(payload), job))
    store.queue_replay(job)
    run_replay_job(tmp_path, job, now=AFTER)
    assert store.recent()[0]["status"] == "failed"
    assert "output" in store.recent()[0]["error"]
