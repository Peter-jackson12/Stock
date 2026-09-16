import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from control_tower.capture_history import CaptureHistory
from control_tower.ipc import CaptureConnection, ControlChannel
from control_tower.lifecycle import (
    MAX_MESSAGE_BYTES, CaptureReport, ProcessIdentity, StopCommand,
    encode_report,
)


@pytest.fixture
def pair():
    left, right = socket.socketpair()
    channel = ControlChannel(left, timeout=0.1)
    try:
        yield channel, right
    finally:
        channel.close()
        right.close()


def report():
    # Synthetic identity, deliberately not an OS process attestation.
    return CaptureReport(ProcessIdentity("fixture", 1, "2026-09-16T00:00:00Z",
                         "C:/fixture/python.exe", "fixture", 32, "fixture",
                         "synthetic", "C:/fixture/raw.db"), 1, "starting")


def connection(tmp_path, channel):
    capture = CaptureHistory(tmp_path).register(report().identity, heartbeat_timeout_ns=10**10)
    return CaptureConnection(capture, channel)


def test_roundtrip_and_coalesced_frames(pair):
    channel, sock = pair
    peer = ControlChannel(sock)
    peer.send_report(report())
    peer.send_report(replace(report(), revision=2))
    assert channel.receive_report() == report()
    assert channel.receive_report().revision == 2
    command = StopCommand("stop", report().identity, 2)
    channel.send_stop(command)
    assert peer.receive_stop() == command


@pytest.mark.parametrize("packet", [
    struct.pack("!I", 0), struct.pack("!I", MAX_MESSAGE_BYTES + 1),
    b"\x00\x00", struct.pack("!I", 30) + b"{}",
    struct.pack("!I", 1) + b"\xff", struct.pack("!I", 2) + b"{}",
])
def test_bad_frames_close_channel(pair, packet):
    channel, sock = pair
    sock.sendall(packet)
    sock.shutdown(socket.SHUT_WR)
    with pytest.raises((ValueError, EOFError)):
        channel.receive_report()
    assert channel.closed
    with pytest.raises(ConnectionError):
        channel.receive_report()


def test_receive_timeout_is_terminal(pair):
    channel, _ = pair
    with pytest.raises(TimeoutError):
        channel.receive_report()
    assert channel.closed


def test_partial_reads_share_one_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("control_tower.ipc.time.monotonic", lambda: now[0])

    class SlowSocket:
        packet = bytearray(struct.pack("!I", 20) + b"x" * 20)
        timeouts = []
        closed = False

        def settimeout(self, value):
            self.timeouts.append(value)

        def recv(self, size):
            now[0] += 0.03
            return bytes([self.packet.pop(0)])

        def close(self):
            self.closed = True

    sock = SlowSocket()
    channel = ControlChannel(sock, timeout=0.1)
    with pytest.raises(TimeoutError):
        channel.receive_report()
    assert sock.closed and len(sock.timeouts) == 4
    assert sock.timeouts == sorted(sock.timeouts, reverse=True)


@pytest.mark.parametrize("timeout", [0, -1, 6, float("inf"), float("nan"), True])
def test_timeout_configuration_bounded(pair, timeout):
    with pytest.raises(ValueError):
        ControlChannel(pair[1], timeout=timeout)


def test_wrong_session_is_not_persisted_as_report(tmp_path, pair):
    channel, sock = pair
    conn = connection(tmp_path, channel)
    other = replace(report(), identity=replace(report().identity, session_id="other"))
    ControlChannel(sock).send_report(other)
    with pytest.raises(ValueError, match="identity"):
        conn.receive()
    assert conn.capture.report is None
    assert conn.capture.view(now_ns=time.monotonic_ns())["state"] == "unknown"
    assert channel.closed


def test_disconnect_after_send_is_unknown_and_cannot_resend(tmp_path, pair):
    channel, sock = pair
    conn = connection(tmp_path, channel)
    peer = ControlChannel(sock)
    peer.send_report(report())
    conn.receive()
    conn.capture.request_stop("stop", report().identity, now_ns=time.monotonic_ns(), timeout_ns=10**10)
    conn.dispatch_stop()
    assert peer.receive_stop().request_id == "stop"
    peer.close()
    with pytest.raises(EOFError):
        conn.receive()
    assert conn.capture.view(now_ns=time.monotonic_ns())["stop_status"] == "unknown"
    with pytest.raises(ValueError):
        conn.dispatch_stop()


def test_partial_send_failure_persists_uncertainty_and_prevents_retry(tmp_path):
    class PartialSocket:
        calls = 0
        closed = False

        def settimeout(self, timeout):
            assert 0 < timeout <= 1

        def sendall(self, packet):
            self.calls += 1
            assert len(packet) > 4
            raise TimeoutError("some bytes may have been delivered")

        def close(self):
            self.closed = True

    sock = PartialSocket()
    conn = connection(tmp_path, ControlChannel(sock))
    conn.capture.receive(report(), now_ns=time.monotonic_ns())
    conn.capture.request_stop("stop", report().identity, now_ns=time.monotonic_ns(), timeout_ns=10**10)
    with pytest.raises(TimeoutError):
        conn.dispatch_stop()
    assert sock.closed
    assert conn.capture.view(now_ns=time.monotonic_ns())["stop_status"] == "unknown"
    recovered = CaptureHistory(tmp_path).recover(report().identity)
    with pytest.raises(ValueError, match="audit-only"):
        CaptureConnection(recovered, ControlChannel(sock)).dispatch_stop()
    assert sock.calls == 1


def test_command_frame_cannot_be_received_as_report(tmp_path, pair):
    channel, sock = pair
    conn = connection(tmp_path, channel)
    ControlChannel(sock).send_stop(StopCommand("stop", report().identity, 1))
    with pytest.raises(ValueError):
        conn.receive()
    assert conn.capture.report is None and channel.closed


@pytest.mark.skipif(os.name != "nt", reason="Windows socket sharing subprocess integration")
@pytest.mark.parametrize("outcome", ["closed", "exit_without_report"])
@pytest.mark.parametrize("environment", ["current", ".venv32"])
def test_actual_fixture_process(tmp_path, outcome, environment):
    """Share a socket capability only with our new child; no TCP listener/OCX."""
    # uv venv executables may be launcher processes. Direct base Python keeps
    # Popen.pid equal to the process receiving the Windows socket capability.
    interpreter = Path(sys.base_prefix) / "python.exe"
    if environment == ".venv32":
        config_path = Path(__file__).resolve().parents[1] / environment / "pyvenv.cfg"
        if not config_path.exists():
            pytest.skip("optional 32-bit environment unavailable")
        config = dict(line.split(" = ", 1) for line in config_path.read_text().splitlines() if " = " in line)
        interpreter = Path(config["home"]) / "python.exe"
        if not interpreter.exists():
            pytest.skip("optional 32-bit base interpreter unavailable")
    left, right = socket.socketpair()
    process = subprocess.Popen(
        [str(interpreter), str(Path(__file__).parent / "fixtures" / "capture_ipc_peer.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=Path(__file__).resolve().parents[1],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    channel = ControlChannel(left, timeout=5)
    try:
        config = {"socket": right.share(process.pid).hex(), "report": encode_report(report()),
                  "outcome": outcome, "expected_bits": 32 if environment == ".venv32" else struct.calcsize("P") * 8}
        process.stdin.write(json.dumps(config) + "\n")
        process.stdin.flush()
        conn = connection(tmp_path, channel)
        assert conn.receive()
        conn.capture.request_stop("stop", report().identity, now_ns=time.monotonic_ns(), timeout_ns=10**10)
        conn.dispatch_stop()
        if outcome == "closed":
            assert conn.receive()  # draining acknowledgement
            assert conn.receive()  # explicit fixture finalization
            right.close()
            with pytest.raises(EOFError):
                conn.receive()  # EOF cannot undo the recorded finalization.
        else:
            # Close our duplicate so EOF reflects the child, not this handle.
            right.close()
            with pytest.raises(EOFError):
                conn.receive()
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        assert stdout == ""
        state = conn.capture.view(now_ns=time.monotonic_ns())
        assert state["state"] == ("closed" if outcome == "closed" else "unknown")
        assert state["stop_status"] == ("completed" if outcome == "closed" else "unknown")
        assert state["data_quality"] == "unverified"
    finally:
        channel.close()
        right.close()
        if process.poll() is None:
            process.kill()  # Only this explicitly created synthetic child.
        process.communicate(timeout=5)
