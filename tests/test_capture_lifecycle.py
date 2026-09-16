"""In-memory fake peer and tiny JSON messages; no process/OCX/raw access."""
from dataclasses import replace
import json

import pytest

from control_tower.lifecycle import (
    CaptureLifecycle, CaptureReport, Finalization, ProcessIdentity, StopCommand, StopReceiver,
    decode_report, decode_stop, encode_report, encode_stop,
)


def identity(**changes):
    base = ProcessIdentity("session-a", 123, "2026-09-16T00:00:00+00:00", "C:/fixture/python.exe",
                           "test-revision", 32, "fixture", "synthetic", "C:/fixture/a.db")
    return replace(base, **changes)


class FakePeer:
    def __init__(self, manager):
        self.manager = manager
        self.report = CaptureReport(manager.identity, 1, "starting")
        self.receiver = StopReceiver(manager.identity)
        self.stop_calls = 0
        self.now = 0
        manager.receive(decode_report(encode_report(self.report)), now_ns=self.now)

    def send(self, **changes):
        report = replace(self.report, revision=self.report.revision + 1, **changes)
        self.now += 1
        self.manager.receive(decode_report(encode_report(report)), now_ns=self.now)
        self.report = report

    def ready(self):
        self.send(state="login_required")
        self.send(state="subscribed")
        self.send(state="receiving", callback_count=3, accepted_seq=3, committed_seq=1,
                  queued=1, in_flight=1, last_event_ns=50, last_commit_at_utc="2026-09-16T00:01:00Z")

    def stop(self, command):
        if self.receiver.accept(decode_stop(encode_stop(command)), self.report):
            self.stop_calls += 1
            self.send(state="draining", input_stopped=True, stop_request_id=command.request_id)

    def close(self):
        self.send(state="closed", committed_seq=3, queued=0, in_flight=0, writer_closed=True,
                  finalization=Finalization(3, 51, "a" * 64))


def running():
    manager = CaptureLifecycle(identity(), heartbeat_timeout_ns=10)
    peer = FakePeer(manager)
    peer.ready()
    return manager, peer


def test_normal_stop_needs_drain_commit_and_finalization():
    manager, peer = running()
    command = manager.request_stop("stop-1", identity(), now_ns=3, timeout_ns=20)
    assert manager.view(now_ns=3)["stop_status"] == "pending"
    peer.stop(command)
    assert manager.view(now_ns=4)["stop_status"] == "acknowledged"
    assert manager.view(now_ns=4)["state"] == "draining"
    peer.close()
    view = manager.view(now_ns=5)
    assert view["state"] == "closed" and view["stop_status"] == "completed"
    assert view["data_quality"] == "unverified" and not view["automatic_restart"]
    manager.lost_contact(identity(), reason="peer exited", now_ns=6)
    assert manager.view(now_ns=100)["state"] == "closed"


def test_startup_stop_with_no_events_still_needs_explicit_finalization():
    manager = CaptureLifecycle(identity(), heartbeat_timeout_ns=10)
    peer = FakePeer(manager)
    peer.stop(manager.request_stop("s", identity(), now_ns=0, timeout_ns=5))
    peer.send(state="closed", writer_closed=True, finalization=Finalization(0, 1, "a" * 64))
    assert manager.view(now_ns=2)["stop_status"] == "completed"


def test_peer_rejects_command_for_unobserved_revision():
    _, peer = running()
    with pytest.raises(ValueError, match="controllable"):
        peer.receiver.accept(StopCommand("s", identity(), 100), peer.report)
    assert peer.receiver.command is None


def test_duplicate_stop_and_lost_reply_do_not_repeat_input_stop():
    manager, peer = running()
    command = manager.request_stop("stop-1", identity(), now_ns=3, timeout_ns=20)
    peer.stop(command)
    assert manager.request_stop("stop-1", identity(), now_ns=4, timeout_ns=999) == command
    peer.stop(command)
    peer.close()
    peer.stop(command)
    assert peer.stop_calls == 1
    with pytest.raises(ValueError, match="conflicting"):
        peer.receiver.accept(replace(command, expected_revision=5), peer.report)


def test_stop_timeout_is_unknown_and_retry_does_not_extend_deadline():
    manager, peer = running()
    command = manager.request_stop("stop-1", identity(), now_ns=3, timeout_ns=2)
    peer.stop(command)
    manager.request_stop("stop-1", identity(), now_ns=4, timeout_ns=999)
    view = manager.view(now_ns=5)
    assert view["stop_status"] == "unknown" and view["state"] == "draining"
    assert peer.stop_calls == 1
    # A late, matching finalization can resolve uncertainty without retrying a side effect.
    peer.now = 5
    peer.close()
    assert manager.view(now_ns=6)["stop_status"] == "completed"


def test_no_report_stale_or_lost_contact_cannot_authorize_control():
    manager = CaptureLifecycle(identity(), heartbeat_timeout_ns=10)
    assert manager.view(now_ns=0)["state"] == "unknown"
    with pytest.raises(ValueError, match="fresh"):
        manager.request_stop("s", identity(), now_ns=0, timeout_ns=2)
    peer = FakePeer(manager)
    peer.ready()
    assert manager.view(now_ns=13)["state"] == "unknown"
    with pytest.raises(ValueError, match="fresh"):
        manager.request_stop("s", identity(), now_ns=13, timeout_ns=2)
    peer.now = 13
    peer.send()
    manager.lost_contact(identity(), reason="connection lost", now_ns=15)
    assert manager.view(now_ns=15)["state"] == "unknown"
    assert manager.view(now_ns=15)["last_reported_state"] == "receiving"


def test_duplicate_heartbeat_does_not_refresh_liveness():
    manager, peer = running()
    assert not manager.receive(peer.report, now_ns=12)
    assert manager.view(now_ns=13)["state"] == "unknown"


@pytest.mark.parametrize("change", [{"session_id": "new"}, {"pid": 456},
    {"started_at_utc": "2026-09-16T01:00:00Z"}, {"code_revision": "different"},
    {"executable": "C:/fixture/other.exe"}, {"dataset_path": "C:/fixture/new.db"},
    {"server": "live"}, {"python_bits": 64}, {"feed_scope": "other"}])
def test_identity_mismatch_never_controls_a_reused_pid_or_other_session(change):
    manager, peer = running()
    foreign = identity(**change)
    with pytest.raises(ValueError, match="identity"):
        manager.request_stop("s", foreign, now_ns=3, timeout_ns=10)
    with pytest.raises(ValueError, match="identity"):
        manager.receive(replace(peer.report, identity=foreign, revision=4), now_ns=3)
    with pytest.raises(ValueError, match="identity"):
        peer.receiver.accept(StopCommand("s", foreign, 3), peer.report)
    assert peer.stop_calls == 0


def test_restarted_session_cannot_consume_old_command_or_old_reports():
    old, peer = running()
    command = old.request_stop("s", identity(), now_ns=3, timeout_ns=5)
    new_identity = identity(session_id="new", started_at_utc="2026-09-16T01:00:00Z", dataset_path="C:/fixture/new.db")
    new = CaptureLifecycle(new_identity, heartbeat_timeout_ns=10)
    new_peer = FakePeer(new)
    with pytest.raises(ValueError, match="identity"):
        new_peer.stop(command)
    with pytest.raises(ValueError, match="identity"):
        new.receive(peer.report, now_ns=1)
    assert new_peer.stop_calls == 0


@pytest.mark.parametrize("change", [{"queued": 0}, {"in_flight": 0}, {"committed_seq": 4},
    {"input_stopped": True}, {"writer_closed": True}, {"dropped": 1}, {"write_failures": 1},
    {"callback_count": True}, {"last_event_ns": None}, {"last_commit_at_utc": "2026-09-16T00:00:00"}])
def test_contradictory_reports_rejected(change):
    _, peer = running()
    with pytest.raises(ValueError):
        replace(peer.report, **change)


@pytest.mark.parametrize("change", [{"writer_closed": False}, {"input_stopped": False},
    {"dropped": 1}, {"write_failures": 1}, {"finalization": None},
    {"finalization": Finalization(2, 51, "a" * 64)}, {"finalization": Finalization(3, 50, "a" * 64)},
    {"committed_seq": 2, "in_flight": 1}])
def test_incomplete_storage_cannot_be_reported_as_closed(change):
    manager, peer = running()
    peer.stop(manager.request_stop("s", identity(), now_ns=3, timeout_ns=10))
    values = dict(state="closed", committed_seq=3, queued=0, in_flight=0, writer_closed=True,
                  finalization=Finalization(3, 51, "a" * 64)) | change
    with pytest.raises(ValueError):
        replace(peer.report, **values)
    assert manager.view(now_ns=4)["stop_status"] == "acknowledged"


def test_no_adoption_or_restart_after_interruption():
    manager, peer = running()
    empty = CaptureLifecycle(identity(), heartbeat_timeout_ns=10)
    with pytest.raises(ValueError, match="handshake"):
        empty.receive(peer.report, now_ns=3)
    peer.send(state="interrupted", dropped=1)
    with pytest.raises(ValueError):
        peer.send(state="receiving", dropped=0)
    assert manager.report.state == "interrupted"


def test_storage_failure_is_failed_stop_and_does_not_authorize_restart():
    manager, peer = running()
    peer.stop(manager.request_stop("s", identity(), now_ns=3, timeout_ns=20))
    peer.send(state="failed", write_failures=1)
    view = manager.view(now_ns=5)
    assert view["stop_status"] == "failed" and view["state"] == "failed"
    assert not view["automatic_restart"]


def test_revision_counter_and_clock_regressions_rejected():
    manager, peer = running()
    for changed in ({"revision": 6}, {"revision": 3}, {"revision": 5, "callback_count": 2},
                    {"revision": 5, "last_event_ns": 49}):
        with pytest.raises(ValueError):
            manager.receive(replace(peer.report, **changed), now_ns=3)
    with pytest.raises(ValueError, match="clock"):
        manager.view(now_ns=2)


def test_new_input_cannot_sneak_into_draining_and_ack_must_match():
    manager, peer = running()
    command = manager.request_stop("s", identity(), now_ns=3, timeout_ns=10)
    with pytest.raises(ValueError, match="acknowledgement"):
        peer.send(state="draining", input_stopped=True, stop_request_id="foreign")
    peer.stop(command)
    with pytest.raises(ValueError, match="resume"):
        peer.send(accepted_seq=4, queued=2, last_event_ns=51)
    with pytest.raises(ValueError, match="already requested"):
        manager.request_stop("new", identity(), now_ns=peer.now, timeout_ns=10)


def test_finalization_is_immutable_after_close():
    manager, peer = running()
    peer.stop(manager.request_stop("s", identity(), now_ns=3, timeout_ns=10))
    peer.close()
    with pytest.raises(ValueError, match="cannot change"):
        peer.send(finalization=Finalization(3, 52, "b" * 64))
    assert manager.view(now_ns=peer.now)["stop_status"] == "completed"


def test_wire_schema_roundtrip_and_unknown_command_rejected():
    manager, peer = running()
    assert decode_report(encode_report(peer.report)) == peer.report
    command = manager.request_stop("s", identity(), now_ns=3, timeout_ns=10)
    assert decode_stop(encode_stop(command)) == command
    for decoder, text in ((decode_report, encode_report(peer.report)), (decode_stop, encode_stop(command))):
        data = json.loads(text)
        with pytest.raises(ValueError):
            decoder(json.dumps(data | {"schema": "future"}))
        with pytest.raises(ValueError):
            decoder(json.dumps(data | {"extra": 1}))
        with pytest.raises(ValueError):
            decoder("x" * 32769)
    with pytest.raises(ValueError, match="command"):
        decode_stop(encode_stop(command).replace('"stop"', '"kill"'))
