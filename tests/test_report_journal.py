from dataclasses import replace
import socket
import sqlite3
import threading

import pytest

from control_tower.capture_history import CaptureHistory
from control_tower.ipc import CaptureConnection, ControlChannel
from control_tower.lifecycle import CaptureReport, Finalization, ProcessIdentity
from control_tower.report_journal import ReportJournal, ReplayRequest, ReplayPage, encode_page, decode_page


def initial():
    return CaptureReport(ProcessIdentity("s", 1, "2026-09-16T00:00:00Z", "C:/fixture/python.exe",
        "fixture", 32, "fixture", "fixture", "C:/fixture/raw.db"), 1, "starting")


def exchange(capture, journal, limit=128):
    left, right = socket.socketpair()
    manager, peer = ControlChannel(left), ControlChannel(right)
    errors = []
    def serve():
        try:
            journal.serve_replay(peer)
        except BaseException as exc:
            errors.append(exc)
        finally:
            peer.close()
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        return CaptureConnection(capture, manager, clock=lambda: 10).reconcile_page(limit=limit)
    finally:
        manager.close()
        thread.join(3)
        assert not thread.is_alive()
        assert not errors, errors


def test_store_before_send_failure_and_reopen(tmp_path):
    first = initial()
    journal = ReportJournal(tmp_path, first.identity)
    class FailedChannel:
        def send_report(self, report):
            assert journal.page(ReplayRequest(first.identity, 0)).reports == (first,)
            raise TimeoutError("delivery unknown")
    with pytest.raises(TimeoutError):
        journal.publish(first, FailedChannel())
    reopened = ReportJournal(tmp_path, first.identity)
    assert reopened.page(ReplayRequest(first.identity, 0)).reports == (first,)
    assert not reopened.append(first)
    with pytest.raises(ValueError, match="conflict"):
        reopened.append(replace(first, state="login_required"))


def test_missing_closed_report_reconciles_without_stop_resend(tmp_path):
    first = initial()
    history = CaptureHistory(tmp_path)
    manager = history.register(first.identity, heartbeat_timeout_ns=100)
    manager.receive(first, now_ns=0)
    manager.request_stop("stop", first.identity, now_ns=1, timeout_ns=100)
    manager.dispatch_stop(lambda command: None, now_ns=2)
    journal = ReportJournal(tmp_path / "peer", first.identity)
    drain = replace(first, revision=2, state="draining", input_stopped=True, stop_request_id="stop")
    closed = replace(drain, revision=3, state="closed", writer_closed=True, finalization=Finalization(0, 1, "0" * 64))
    for report in (first, drain, closed):
        journal.append(report)
    recovered = history.recover(first.identity)
    assert recovered.view(now_ns=0)["stop_status"] == "unknown"
    page = exchange(recovered, journal, limit=1)
    assert page.head_revision == 3 and page.reports == (drain,)
    assert recovered.view(now_ns=10)["state"] == "unknown"
    exchange(recovered, journal)
    assert recovered.view(now_ns=10)["stop_status"] == "completed"
    with pytest.raises(ValueError, match="audit-only"):
        recovered.dispatch_stop(lambda command: pytest.fail("stop resent"), now_ns=10)
    assert history.recover(first.identity).view(now_ns=0)["state"] == "closed"


def test_historical_receiving_and_empty_page_do_not_refresh(tmp_path):
    first = initial()
    journal = ReportJournal(tmp_path / "peer", first.identity)
    for i, state in enumerate(("starting", "login_required", "subscribed", "receiving"), 1):
        journal.append(replace(first, revision=i, state=state))
    manager = CaptureHistory(tmp_path).register(first.identity, heartbeat_timeout_ns=100)
    exchange(manager, journal)
    assert manager.report.state == "receiving"
    assert manager.view(now_ns=10)["state"] == "unknown"
    assert not exchange(manager, journal).reports
    assert manager.view(now_ns=10)["state"] == "unknown"
    manager.receive(replace(manager.report, revision=5), now_ns=11)
    assert manager.view(now_ns=11)["state"] == "receiving"


def test_byte_bounded_pages_roundtrip(tmp_path):
    first = initial()
    journal = ReportJournal(tmp_path, first.identity)
    for i in range(1, 101):
        journal.append(replace(first, revision=i))
    cursor = 0
    pages = 0
    while cursor < 100:
        page = journal.page(ReplayRequest(first.identity, cursor))
        encoded = encode_page(page)
        assert len(encoded.encode()) <= 32768 and decode_page(encoded) == page
        assert len(page.reports) < 100
        cursor = page.reports[-1].revision
        pages += 1
    assert pages > 1


@pytest.mark.parametrize("after,limit", [(-1, 1), (True, 1), (0, 0), (0, 129), (0, True)])
def test_request_bounds(after, limit):
    with pytest.raises(ValueError):
        ReplayRequest(initial().identity, after, limit)


def test_identity_and_revision_mismatch_rejected(tmp_path):
    first = initial()
    journal = ReportJournal(tmp_path, first.identity)
    with pytest.raises(ValueError):
        journal.append(replace(first, revision=2))
    other = replace(first.identity, session_id="other")
    with pytest.raises(ValueError):
        ReportJournal(tmp_path, other)
    with pytest.raises(ValueError):
        journal.page(ReplayRequest(other, 0))
    with pytest.raises(ValueError):
        journal.page(ReplayRequest(first.identity, 1))


def test_sql_failure_prevents_send(tmp_path):
    first = initial()
    journal = ReportJournal(tmp_path, first.identity)
    with sqlite3.connect(journal.path) as conn:
        conn.execute("CREATE TRIGGER fail BEFORE INSERT ON reports BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
    class NoSend:
        def send_report(self, report):
            pytest.fail("unjournaled report sent")
    with pytest.raises(sqlite3.Error):
        journal.publish(first, NoSend())
    assert journal.page(ReplayRequest(first.identity, 0)).head_revision == 0


def test_deleted_revision_fails_closed(tmp_path):
    first = initial()
    journal = ReportJournal(tmp_path, first.identity)
    journal.append(first)
    journal.append(replace(first, revision=2))
    with sqlite3.connect(journal.path) as conn:
        conn.execute("DELETE FROM reports WHERE revision=1")
    with pytest.raises(ValueError, match="contiguous"):
        journal.page(ReplayRequest(first.identity, 0))


def test_invalid_lifecycle_page_is_atomic(tmp_path):
    first = initial()
    journal = ReportJournal(tmp_path / "peer", first.identity)
    journal.append(first)
    journal.append(replace(first, revision=2, state="receiving"))
    manager = CaptureHistory(tmp_path).register(first.identity, heartbeat_timeout_ns=100)
    with pytest.raises(ValueError, match="transition"):
        exchange(manager, journal)
    assert manager.report is None


def test_wrong_cursor_reply_not_applied(tmp_path):
    first = initial()
    manager = CaptureHistory(tmp_path).register(first.identity, heartbeat_timeout_ns=100)
    class WrongReply:
        closed = False
        def send_replay_request(self, request):
            pass
        def receive_replay_page(self):
            return ReplayPage(first.identity, 1, 1, ())
        def close(self):
            self.closed = True
    channel = WrongReply()
    with pytest.raises(ValueError, match="match request"):
        CaptureConnection(manager, channel, clock=lambda: 1).reconcile_page()
    assert channel.closed and manager.report is None


def test_existing_handle_rejects_changed_journal_identity(tmp_path):
    first = initial()
    journal = ReportJournal(tmp_path, first.identity)
    with sqlite3.connect(journal.path) as conn:
        conn.execute("UPDATE peer SET identity='{}'")
    with pytest.raises(ValueError, match="identity/schema"):
        journal.append(first)
    with pytest.raises(ValueError, match="identity/schema"):
        journal.page(ReplayRequest(first.identity, 0))


def test_replay_frame_cannot_claim_gap_or_extra_fields():
    import json
    first = initial()
    with pytest.raises(ValueError, match="missing reports"):
        ReplayPage(first.identity, 0, 1, ())
    with pytest.raises(ValueError, match="contiguous"):
        ReplayPage(first.identity, 0, 2, (replace(first, revision=2),))
    payload = json.loads(encode_page(ReplayPage(first.identity, 0, 1, (first,))))
    payload["unexpected"] = True
    with pytest.raises(ValueError, match="schema"):
        decode_page(json.dumps(payload))


def test_partial_page_frame_applies_nothing(tmp_path):
    import struct
    first = initial()
    capture = CaptureHistory(tmp_path).register(first.identity, heartbeat_timeout_ns=100)
    left, right = socket.socketpair()
    channel = ControlChannel(left)
    encoded = encode_page(ReplayPage(first.identity, 0, 1, (first,))).encode()
    right.sendall(struct.pack("!I", len(encoded)) + encoded[:40])
    right.shutdown(socket.SHUT_WR)
    try:
        with pytest.raises(EOFError):
            CaptureConnection(capture, channel, clock=lambda: 1).reconcile_page()
        assert capture.report is None and channel.closed
        assert capture.view(now_ns=1)["state"] == "unknown"
    finally:
        channel.close()
        right.close()
