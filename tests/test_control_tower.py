"""Small synthetic files only; no OCX, network, live raw DB or real workers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import sqlite3
from types import SimpleNamespace

import pytest

from collector.raw_v2 import RawV2Writer
from control_tower.jobs import JobStore, MAX_JSON_BYTES
from control_tower import service, status


NOW = datetime(2026, 9, 16, 10, 0, tzinfo=status.KST)


def heartbeat(stamp=NOW, trades=10, quotes=20):
    return f"{stamp:%Y-%m-%d %H:%M:%S} ⏱️ [10:00:00] 체결: {trades:,}건 | 호가: {quotes:,}건 (대기큐: 3)\n"


def log_file(root, content):
    path = root / "logs/kiwoom_universe_20260916.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def result_file(root, **changes):
    path = root / "research_runs/example/result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = dict(schema="tick_research_result_v1", status="completed_with_open_position",
                 event_count=2, open_quantity=1, final_cash="100", orders=[{}], fills=[{}], signals=[{}])
    path.write_text(json.dumps(value | changes), encoding="utf-8")
    return path


def raw_file(root, *, closed=True):
    path = root / "sampledata/raw_ticks_v2/example.db"
    with RawV2Writer(path, source="fixture", session_id="s", market_date="2026-09-16", feed_scope="test") as writer:
        if closed:
            writer.finish(close_ns=1)
    return path


def plan(root, db, **changes):
    values = dict(db=str(db), code="005930", venue="unknown", quantity=1, cash="100000", fee_rate="0.001",
                  buy_latency_sec="1", sell_latency_sec="1", cancel_latency_sec="1",
                  max_quote_age_sec="2", cooldown_sec="10", exit_rule="fixed")
    return service.plan_replay(root, **(values | changes))


def test_empty_screen_reads_do_not_create_state(tmp_path):
    assert status.observe_collector(tmp_path, now=NOW)["status"] == "unavailable"
    assert JobStore(tmp_path).recent() == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("age,expected", [(2, "recent"), (181, "stale"), (-30, "clock_ahead")])
def test_observation_is_freshness_not_health(tmp_path, age, expected):
    log_file(tmp_path, heartbeat(NOW - timedelta(seconds=age), trades=10000))
    observed = status.observe_collector(tmp_path, now=NOW)
    assert observed["status"] == expected
    assert observed["heartbeat"]["trades"] == 10000
    assert observed["process_state"] == "unverified" and observed["control"] == "external"


def test_bounded_tail_and_damaged_line(tmp_path):
    old = heartbeat(NOW - timedelta(hours=1))
    damaged = heartbeat().replace("2026-09-16", "2026-99-16")
    log_file(tmp_path, old + "x" * (status.MAX_LOG_BYTES * 2) + "\n" + damaged + "파싱 오류\n" + heartbeat())
    observed = status.observe_collector(tmp_path, now=NOW)
    assert observed["status"] == "recent"
    assert observed["recent_messages"] == ["파싱 오류"]
    log_file(tmp_path, old + "x" * (status.MAX_LOG_BYTES * 2) + "\n" + damaged)
    assert status.observe_collector(tmp_path, now=NOW)["status"] == "no_heartbeat"


def test_job_deduplication_and_cancel_persist_across_instances(tmp_path):
    store = JobStore(tmp_path)
    first = store.submit("inspect_result", {"path": "same"})
    assert JobStore(tmp_path).submit("inspect_result", {"path": "same"}) == first
    assert store.cancel(first)
    assert not store.cancel(first)
    assert JobStore(tmp_path).recent()[0]["status"] == "cancelled"
    assert store.submit("inspect_result", {"path": "same"}) != first


def test_two_workers_cannot_claim_same_job_or_cancel_claimed_job(tmp_path):
    store = JobStore(tmp_path)
    job_id = store.submit("inspect_result", {"path": "same"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda owner: JobStore(tmp_path).claim_inspection(owner), ("a", "b")))
    claim, = [item for item in claims if item is not None]
    assert claim["id"] == job_id and not store.cancel(job_id)
    with pytest.raises(ValueError, match="worker"):
        store.complete(job_id, "foreign", result={})
    store.complete(job_id, claim["owner"], result={"status": "completed_flat"})
    assert JobStore(tmp_path).recent()[0]["status"] == "succeeded"
    assert store.claim_inspection("c") is None


def test_interrupted_running_job_is_not_retried(tmp_path):
    store = JobStore(tmp_path)
    job_id = store.submit("inspect_result", {"path": "same"})
    store.claim_inspection("dead-worker")
    assert JobStore(tmp_path).claim_inspection("new-worker") is None
    assert store.submit("inspect_result", {"path": "same"}) == job_id
    assert store.recent()[0]["owner"] == "dead-worker"


@pytest.mark.parametrize("changes", [{}, {"error": ""}, {"error": "failure", "result": {}},
                                     {"result": []}, {"result": {"bad": float("nan")}}])
def test_invalid_outcomes_cannot_mark_success(tmp_path, changes):
    store = JobStore(tmp_path)
    job_id = store.submit("inspect_result", {})
    store.claim_inspection("owner")
    with pytest.raises(ValueError):
        store.complete(job_id, "owner", **changes)
    assert store.recent()[0]["status"] == "running"


def test_job_request_size_and_kind_are_bounded_before_writing(tmp_path):
    store = JobStore(tmp_path)
    for kind, payload in [("shell", {}), ("inspect_result", {"data": "x" * MAX_JSON_BYTES})]:
        with pytest.raises(ValueError):
            store.submit(kind, payload)
    assert not store.path.exists()


@pytest.mark.parametrize("research_status", ["failed", "running", "completed_with_open_position"])
def test_worker_preserves_research_outcome(tmp_path, research_status):
    path = result_file(tmp_path, status=research_status)
    job_id = service.queue_inspection(tmp_path, path)
    before = path.read_bytes()
    assert service.run_one_inspection(tmp_path) == job_id
    job, = JobStore(tmp_path).recent()
    assert job["status"] == "succeeded"
    assert job["result"]["status"] == research_status
    assert job["result"]["diagnostics_only"] == (research_status in ("failed", "running"))
    assert "pnl" not in job["result"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad_payload", [{"path": "../outside/result.json"}, {"command": "anything"}])
def test_worker_revalidates_persisted_payload(tmp_path, bad_payload):
    store = JobStore(tmp_path)
    job_id = store.submit("inspect_result", bad_payload)
    assert service.run_one_inspection(tmp_path) == job_id
    assert store.recent()[0]["status"] == "failed"


def test_worker_missing_or_large_result_records_failure(tmp_path):
    path = result_file(tmp_path, error="x" * MAX_JSON_BYTES)
    service.queue_inspection(tmp_path, path)
    service.run_one_inspection(tmp_path)
    assert JobStore(tmp_path).recent()[0]["status"] == "failed"
    assert "32 KiB" in JobStore(tmp_path).recent()[0]["error"]
    service.queue_inspection(tmp_path, path)
    path.unlink()
    service.run_one_inspection(tmp_path)
    assert "FileNotFoundError" in JobStore(tmp_path).recent()[0]["error"]


def test_paths_cannot_escape_allowed_data_folders(tmp_path):
    outside = tmp_path / "result.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="research_runs"):
        service.queue_inspection(tmp_path, outside)
    wrong = tmp_path / "research_runs/not-result.json"
    wrong.parent.mkdir()
    wrong.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="filename"):
        service.queue_inspection(tmp_path, wrong)
    assert not JobStore(tmp_path).path.exists()


def test_replay_is_only_a_plan_and_does_not_scan_events(tmp_path):
    path = raw_file(tmp_path)
    # An event table that cannot be replayed proves saving is only header validation.
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE events")
    before = path.read_bytes()
    job_id = plan(tmp_path, path)
    assert plan(tmp_path, path) == job_id
    job, = JobStore(tmp_path).recent()
    assert job["status"] == "planned"
    assert job["payload"]["validation"] == "header_only_not_execution_approval"
    assert service.run_one_inspection(tmp_path) is None
    assert not (tmp_path / "research_runs").exists()
    assert path.read_bytes() == before


def test_incomplete_raw_cannot_be_planned(tmp_path):
    with pytest.raises(ValueError, match="incomplete"):
        plan(tmp_path, raw_file(tmp_path, closed=False))
    assert not JobStore(tmp_path).path.exists()


@pytest.mark.parametrize("bad_header", ["oversized", "duplicate", "null"])
def test_bad_manifest_is_rejected_before_plan_is_saved(tmp_path, bad_header):
    path = raw_file(tmp_path)
    with sqlite3.connect(path) as conn:
        if bad_header == "oversized":
            conn.execute("UPDATE metadata SET value=?", ('{"extra":"' + 'x' * 65537 + '"}',))
        elif bad_header == "duplicate":
            conn.execute("INSERT INTO metadata SELECT value FROM metadata")
        else:
            conn.execute("DROP TABLE metadata")
            conn.execute("CREATE TABLE metadata(value TEXT)")
            conn.execute("INSERT INTO metadata VALUES(NULL)")
    with pytest.raises(ValueError, match="manifest"):
        plan(tmp_path, path)
    assert not JobStore(tmp_path).path.exists()


@pytest.mark.parametrize("change", [{"fee_rate": ""}, {"fee_rate": "1"}, {"cash": "NaN"},
                                   {"quantity": 0}, {"buy_latency_sec": "-1"}, {"venue": ""}])
def test_plan_requires_explicit_valid_assumptions(tmp_path, change):
    with pytest.raises(ValueError):
        plan(tmp_path, raw_file(tmp_path), **change)
    assert not JobStore(tmp_path).path.exists()


def test_worker_launch_has_static_argv_and_no_shell(monkeypatch):
    calls = []
    monkeypatch.setattr(service.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(pid=123))
    assert service.start_inspection_worker() == 123
    argv, kwargs = calls[0]
    assert argv == [service.sys.executable, str(service.ROOT / "scripts/control_worker.py")]
    assert kwargs["shell"] is False
    if service.os.name == "nt":
        assert kwargs["creationflags"] & service.subprocess.CREATE_NO_WINDOW
    else:
        assert kwargs["start_new_session"] is True


def test_worker_batch_is_bounded(monkeypatch):
    from scripts import control_worker
    calls = []
    monkeypatch.setattr(control_worker, "run_one_inspection", lambda root: calls.append(root) or "id")
    assert control_worker.main() == 0
    assert len(calls) == 10
