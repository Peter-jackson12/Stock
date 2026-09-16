from dataclasses import replace
import json
import sqlite3

import pytest

from collector.kiwoom.capture_session import CaptureSession
from collector.kiwoom.queued_capture import QueuedCapture
from collector.kiwoom.queue_control import QueueStopReports
from collector.raw_v2 import read_raw_v2
from control_tower.lifecycle import CaptureLifecycle, CaptureReport, ProcessIdentity

UTC = "2026-09-16T00:00:00Z"


@pytest.fixture
def setup(tmp_path):
    queues = []

    def create(factory=CaptureSession):
        path = tmp_path / f"{len(queues)}.db"
        queue = QueuedCapture(path, source="fixture", session_id="s", feed_scope="fixture",
            market_date="2026-09-16", price_policy="signed_magnitude", direction_policy="signed_volume",
            started_ns=0, started_at_utc=UTC, session_factory=factory).start()
        queues.append(queue)
        assert queue.ready.wait(5) and queue.snapshot()["state"] == "running"
        identity = ProcessIdentity("s", 1, UTC, "C:/fixture/python.exe", "fixture", 32,
                                   "fixture", "fixture", str(path))
        initial = CaptureReport(identity, 1, "starting")
        manager = CaptureLifecycle(identity, heartbeat_timeout_ns=100)
        manager.receive(initial, now_ns=0)
        command = manager.request_stop("stop", identity, now_ns=1, timeout_ns=100)
        return queue, initial, command, manager

    yield create
    for queue in queues:
        queue.abort("test cleanup")
        assert queue.wait(6)


def test_raw_counts_and_manifest_match_actual_writer(setup):
    queue, initial, command, manager = setup()
    queue.submit_tick(code="005930", venue="unknown", real_type="주식체결", received_ns=1,
                      received_at_utc=UTC, fids={"20": "090000", "10": "10001", "15": "bad"})
    reports = QueueStopReports(queue, initial.identity).stop(command, initial, close_ns=2)
    draining = next(reports)
    assert draining.callback_count == 1 and draining.accepted_seq == 3  # start + tick + parse_error
    assert not queue.done.is_set() and not draining.writer_closed
    manager.receive(draining, now_ns=2)
    assert manager.view(now_ns=2)["stop_status"] == "acknowledged"
    closed = next(reports)
    manager.receive(closed, now_ns=3)
    assert manager.view(now_ns=3)["stop_status"] == "completed"
    assert manager.view(now_ns=3)["data_quality"] == "unverified"
    with read_raw_v2(initial.identity.dataset_path) as (meta, rows):
        assert len(list(rows)) == closed.finalization.final_seq == 3
        assert meta["payload_sha256"] == closed.finalization.payload_sha256
    reports.close()


def test_abandoned_drain_report_leaves_incomplete(setup):
    queue, initial, command, _ = setup()
    reports = QueueStopReports(queue, initial.identity).stop(command, initial, close_ns=1)
    next(reports)
    reports.close()
    assert queue.wait(5) and queue.snapshot()["state"] == "interrupted"
    assert queue.snapshot()["finalization"] is None
    with sqlite3.connect(initial.identity.dataset_path) as conn:
        assert json.loads(conn.execute("SELECT value FROM metadata").fetchone()[0])["state"] == "incomplete"


@pytest.mark.parametrize("failure", ["finish", "close"])
def test_storage_failure_never_emits_closed(setup, failure):
    class BrokenSession(CaptureSession):
        def finish(self, close_ns):
            if failure == "finish":
                raise OSError("injected finish failure")
            super().finish(close_ns)

        def __exit__(self, *exc):
            super().__exit__(*exc)
            if failure == "close":
                raise OSError("injected close failure")

    queue, initial, command, _ = setup(BrokenSession)
    reports = QueueStopReports(queue, initial.identity).stop(command, initial, close_ns=1)
    assert next(reports).state == "draining"
    with pytest.raises(ValueError, match="close cleanly"):
        next(reports)
    assert queue.snapshot()["state"] == "failed" and queue.snapshot()["finalization"] is None


def test_held_worker_has_independent_timeout(setup):
    queue, _, _, _ = setup()
    queue.request_stop(1, hold_finalize=True)
    assert queue.drained.wait(5)
    assert queue.wait(6)
    assert queue.snapshot()["state"] == "interrupted"
    assert queue.snapshot()["error"] == "finalization release timed out"
    with pytest.raises(ValueError):
        queue.complete_stop()


def test_wrong_dataset_not_bound(setup):
    queue, initial, _, _ = setup()
    with pytest.raises(ValueError, match="identity"):
        QueueStopReports(queue, replace(initial.identity, dataset_path="C:/wrong/raw.db"))


def test_stop_not_reexecuted(setup):
    queue, initial, command, _ = setup()
    adapter = QueueStopReports(queue, initial.identity)
    assert len(list(adapter.stop(command, initial, close_ns=1))) == 2
    with pytest.raises(ValueError, match="already accepted"):
        list(adapter.stop(command, initial, close_ns=1))
