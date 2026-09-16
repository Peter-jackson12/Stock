"""Bounded synthetic subprocess peer, no writer, OCX or production identity."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from control_tower.ipc import ControlChannel
from control_tower.lifecycle import Finalization, StopReceiver, decode_report


def main():
    config = json.loads(sys.stdin.readline())
    assert struct.calcsize("P") * 8 == config["expected_bits"]
    channel = ControlChannel(socket.fromshare(bytes.fromhex(config["socket"])), timeout=5)
    try:
        initial = decode_report(config["report"])
        channel.send_report(initial)
        command = channel.receive_stop()
        assert StopReceiver(initial.identity).accept(command, initial)
        if config["outcome"] == "exit_without_report":
            return
        draining = replace(initial, revision=2, state="draining", input_stopped=True,
                           stop_request_id=command.request_id)
        channel.send_report(draining)
        channel.send_report(replace(draining, revision=3, state="closed", writer_closed=True,
                            finalization=Finalization(0, 1, hashlib.sha256(b"").hexdigest())))
    finally:
        channel.close()


if __name__ == "__main__":
    main()
