"""Bounded synthetic subprocess peer with optional tiny raw writer; no OCX."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from control_tower.ipc import ControlChannel
from control_tower.lifecycle import Finalization, StopReceiver, decode_report
from control_tower.windows_process import WindowsProcess
from control_tower.report_journal import ReportJournal
from collector.kiwoom.queued_capture import QueuedCapture
from collector.kiwoom.queue_control import QueueStopReports


def main():
    config = json.loads(sys.stdin.readline())
    assert struct.calcsize("P") * 8 == config["expected_bits"]
    channel = ControlChannel(socket.fromshare(bytes.fromhex(config["socket"])), timeout=5)
    try:
        initial = decode_report(config["report"])
        with WindowsProcess(os.getpid()) as own_process:
            own_process.verify(initial.identity)
        journal = ReportJournal(Path(initial.identity.dataset_path).parent / "peer", initial.identity)
        journal.publish(initial, channel)
        command = channel.receive_stop()
        assert StopReceiver(initial.identity).accept(command, initial)
        if config["outcome"] == "exit_without_report":
            return
        if config["outcome"] in ("raw_closed", "raw_replay"):
            queue = QueuedCapture(initial.identity.dataset_path, source="fixture",
                session_id=initial.identity.session_id, feed_scope=initial.identity.feed_scope,
                market_date="2026-09-16", price_policy="signed_magnitude", direction_policy="signed_volume",
                started_ns=0, started_at_utc="2026-09-16T00:00:00Z").start()
            reports = None
            try:
                assert queue.ready.wait(5) and queue.snapshot()["state"] == "running"
                queue.submit_tick(code="005930", venue="unknown", real_type="주식체결", received_ns=1,
                    received_at_utc="2026-09-16T00:00:00Z", fids={"20": "090000", "10": "10001", "15": "bad"})
                reports = QueueStopReports(queue, initial.identity).stop(command, initial, close_ns=2)
                for current in reports:
                    if config["outcome"] == "raw_replay":
                        journal.append(current)  # Manager lost contact; save both reports.
                    else:
                        journal.publish(current, channel)
            finally:
                if reports is not None:
                    reports.close()
                queue.abort("fixture cleanup")
                assert queue.wait(6)
            if config["outcome"] == "raw_replay":
                channel.close()
                channel = ControlChannel(socket.fromshare(bytes.fromhex(config["replay_socket"])), timeout=5)
                journal.serve_replay(channel)
            return
        draining = replace(initial, revision=2, state="draining", input_stopped=True,
                           stop_request_id=command.request_id)
        journal.publish(draining, channel)
        journal.publish(replace(draining, revision=3, state="closed", writer_closed=True,
                            finalization=Finalization(0, 1, hashlib.sha256(b"").hexdigest())), channel)
    finally:
        channel.close()


if __name__ == "__main__":
    main()
