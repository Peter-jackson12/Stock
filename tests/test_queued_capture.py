"""Real worker threads and tiny temporary SQLite files; no OCX/login."""
import json
import sqlite3
import threading

import pytest

from collector.kiwoom.capture_session import CaptureSession
from collector.kiwoom.queued_capture import QueuedCapture
from collector.raw_v2 import read_raw_v2


UTC = "2026-09-16T00:00:00Z"


def create(tmp_path, **kwargs):
    return QueuedCapture(tmp_path / "capture.db", source="fixture", session_id="s",
        market_date="2026-09-16", feed_scope="fixture", price_policy="signed_magnitude",
        direction_policy="signed_volume", started_ns=0, started_at_utc=UTC, **kwargs).start()


def ready(c):
    assert c.ready.wait(5)
    assert c.snapshot()["state"] == "running"


def trade(c, ns=1, **changes):
    return c.submit_tick(**(dict(code="005930", venue="unknown", real_type="주식체결",
        fids={"20": "090000", "10": "10000", "15": "+2"}, received_ns=ns,
        received_at_utc=UTC) | changes))


def records(tmp_path):
    with read_raw_v2(tmp_path / "capture.db") as (meta, rows):
        return meta, list(rows)


def stored(tmp_path):
    with sqlite3.connect(tmp_path / "capture.db") as conn:
        meta = json.loads(conn.execute("SELECT value FROM metadata").fetchone()[0])
        rows = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM events ORDER BY seq")]
    return meta, rows


def test_drain_order_raw_copy_and_exclusive_stop(tmp_path):
    c = create(tmp_path, capacity=64, batch_size=3)
    ready(c)
    fids = {"20": "090000", "10": "10000", "15": "+2"}
    assert trade(c, fids=fids)
    fids["15"] = "999"
    for ns in range(2, 21):
        assert trade(c, ns)
    c.request_stop(21)
    c.request_stop(21)
    with pytest.raises(ValueError):
        trade(c, 22)
    assert c.wait(5)
    snapshot = c.snapshot()
    assert snapshot["state"] == "closed" and snapshot["pending_callbacks"] == 0
    assert snapshot["accepted_callbacks"] == snapshot["committed_callbacks"] == 20
    assert snapshot["queued"] == snapshot["in_flight"] == 0
    meta, rows = records(tmp_path)
    assert [row["event"].seq for row in rows] == list(range(1, 22))
    assert rows[1]["raw_fields"]["fids"]["15"] == "+2"
    assert meta["close_ns"] == 21
    with pytest.raises(ValueError):
        c.start()


def blocking_factory(entered, release, *, fail=False, thread_ids=None):
    class BlockingSession(CaptureSession):
        def __init__(self, *args, **kwargs):
            if thread_ids is not None:
                thread_ids.append(threading.get_ident())
            super().__init__(*args, **kwargs)
        def commit(self):
            entered.set()
            assert release.wait(5)
            if fail:
                raise sqlite3.OperationalError("injected disk failure")
            return super().commit()
    return BlockingSession


def test_inflight_is_not_committed_and_timeout_is_not_closed(tmp_path):
    entered, release = threading.Event(), threading.Event()
    ids = []
    c = create(tmp_path, session_factory=blocking_factory(entered, release, thread_ids=ids))
    ready(c)
    try:
        assert trade(c)
        assert entered.wait(5)
        assert c.snapshot()["in_flight"] == c.snapshot()["pending_callbacks"] == 1
        assert c.snapshot()["committed_callbacks"] == 0
        assert ids[0] != threading.get_ident()
        c.request_stop(2)
        assert not c.wait(.01)
        assert c.snapshot()["state"] == "draining"
    finally:
        release.set()
        assert c.wait(5)
    assert c.snapshot()["state"] == "closed"


def test_overflow_drains_accepted_and_persists_marker_without_closed(tmp_path):
    entered, release = threading.Event(), threading.Event()
    c = create(tmp_path, capacity=1, session_factory=blocking_factory(entered, release))
    ready(c)
    try:
        assert trade(c, 1)
        assert entered.wait(5)
        assert trade(c, 2)
        assert not trade(c, 3)
        with pytest.raises(ValueError):
            trade(c, 4)
        with pytest.raises(ValueError):
            c.request_stop(4)
    finally:
        release.set()
        assert c.wait(5)
    snap = c.snapshot()
    assert snap["state"] == "interrupted" and snap["dropped_callbacks"] == 1
    assert snap["committed_callbacks"] == 2 and snap["pending_callbacks"] == 0
    meta, rows = stored(tmp_path)
    assert meta["state"] == "incomplete"
    assert rows[-1]["event"]["control_type"] == "queue_overflow"
    assert [row["event"]["received_ns"] for row in rows] == [0, 1, 2, 3]
    with pytest.raises(ValueError, match="incomplete"):
        records(tmp_path)


def test_commit_error_retains_uncommitted_accounting(tmp_path):
    entered, release = threading.Event(), threading.Event()
    c = create(tmp_path, session_factory=blocking_factory(entered, release, fail=True))
    ready(c)
    try:
        trade(c)
        assert entered.wait(5)
        c.request_stop(2)
    finally:
        release.set()
        assert c.wait(5)
    snap = c.snapshot()
    assert snap["state"] == "failed" and "disk failure" in snap["error"]
    assert snap["pending_callbacks"] == snap["in_flight"] == 1
    assert stored(tmp_path)[0]["state"] == "incomplete"


def test_callback_error_preserves_queued_tail(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class PausedSession(CaptureSession):
        def on_tick(self, **packet):
            entered.set()
            assert release.wait(5)
            return super().on_tick(**packet)
    c = create(tmp_path, session_factory=PausedSession)
    ready(c)
    try:
        trade(c, real_type="unsupported")
        assert entered.wait(5)
        trade(c, 2)
    finally:
        release.set()
        assert c.wait(5)
    assert c.snapshot()["state"] == "interrupted"
    assert c.snapshot()["committed_callbacks"] == 2
    meta, rows = stored(tmp_path)
    assert meta["state"] == "incomplete"
    assert rows[-1]["event"]["details"]["callback"]["received_ns"] == 2


@pytest.mark.parametrize("changes", [{"fids": {15: "+2"}}, {"fids": {"15": "x" * 70000}}])
def test_invalid_or_oversized_raw_input_interrupts(tmp_path, changes):
    c = create(tmp_path)
    ready(c)
    assert not trade(c, **changes)
    assert c.wait(5)
    assert c.snapshot()["state"] == "interrupted"
    assert stored(tmp_path)[1][-1]["event"]["control_type"] == "callback_error"


def test_invalid_receipt_clock_cannot_finalize_cleanly(tmp_path):
    c = create(tmp_path)
    ready(c)
    with pytest.raises(ValueError, match="clock"):
        trade(c, -1)
    assert c.wait(5)
    assert c.snapshot()["state"] == "interrupted"
    assert stored(tmp_path)[0]["state"] == "incomplete"


def test_existing_file_startup_failure_preserves_file(tmp_path):
    path = tmp_path / "capture.db"
    path.write_bytes(b"keep original")
    c = create(tmp_path)
    assert c.ready.wait(5) and c.wait(5)
    assert c.snapshot()["state"] == "failed"
    assert path.read_bytes() == b"keep original"


def test_empty_capture_close_and_boundary_rejection(tmp_path):
    c = create(tmp_path)
    ready(c)
    try:
        with pytest.raises(ValueError, match="boundary"):
            c.request_stop(0)
    finally:
        c.request_stop(1)
        assert c.wait(5)
    assert c.snapshot()["state"] == "closed"
    assert len(records(tmp_path)[1]) == 1


@pytest.mark.parametrize("failure", ["finish", "close"])
def test_finalization_or_connection_close_failure_is_not_success(tmp_path, failure):
    class FailingSession(CaptureSession):
        def finish(self, close_ns):
            if failure == "finish":
                raise sqlite3.OperationalError("finish failed")
            return super().finish(close_ns)
        def __exit__(self, *exc):
            super().__exit__(*exc)
            if failure == "close":
                raise sqlite3.OperationalError("close failed")
    c = create(tmp_path, session_factory=FailingSession)
    ready(c)
    c.request_stop(1)
    assert c.wait(5)
    assert c.snapshot()["state"] == "failed"
    assert failure in c.snapshot()["error"]


def test_startup_abort_does_not_leave_a_late_worker_accepting(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def factory(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return CaptureSession(*args, **kwargs)
    c = create(tmp_path, session_factory=factory)
    try:
        assert entered.wait(5)
        c.abort("startup timeout")
        assert not c.snapshot()["accepting"]
    finally:
        release.set()
        assert c.wait(5)
    assert c.snapshot()["state"] == "interrupted"
    assert stored(tmp_path)[0]["state"] == "incomplete"
