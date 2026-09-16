from dataclasses import replace
import os
import struct

import pytest

from control_tower.lifecycle import ProcessIdentity
from control_tower.windows_process import WindowsProcess

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows OS process inspection")


def identity(facts):
    return ProcessIdentity("fixture", facts.pid, facts.started_at_utc, facts.executable,
                           "fixture", facts.python_bits, "fixture", "fixture", "C:/fixture/raw.db")


def test_own_process_and_closed_handle():
    with WindowsProcess(os.getpid()) as process:
        assert process.facts.python_bits == struct.calcsize("P") * 8
        claim = identity(process.facts)
        process.verify(claim)
        process.verify(replace(claim, executable=claim.executable.swapcase().replace("\\", "/")))
        assert len(claim.started_at_utc.split(".")[1].rstrip("Z")) == 6
    with pytest.raises(ValueError):
        process.verify(claim, require_alive=False)
    process.close()


@pytest.mark.parametrize("changes", [dict(pid=1), dict(started_at_utc="2026-01-01T00:00:00Z"),
                                    dict(executable="C:/other/python.exe")])
def test_mismatched_os_claim_rejected(changes):
    with WindowsProcess(os.getpid()) as process:
        with pytest.raises(ValueError, match="identity"):
            process.verify(replace(identity(process.facts), **changes))


def test_wrong_bitness_rejected():
    with WindowsProcess(os.getpid()) as process:
        with pytest.raises(ValueError):
            process.verify(replace(identity(process.facts), python_bits=96 - process.facts.python_bits))


@pytest.mark.parametrize("pid", [0, -1, True, 2**32, "12"])
def test_pid_not_truncated(pid):
    with pytest.raises(ValueError):
        WindowsProcess(pid)
