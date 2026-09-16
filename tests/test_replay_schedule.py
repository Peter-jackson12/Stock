from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from control_tower.jobs import JobStore
from control_tower.replay_schedule import schedule_replay, schedules, dispatch_schedule, expire_missed, NS
from tests.test_control_tower import raw_file, plan

NOW = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)


def setup(root):
    for relative in (".venv/Scripts/pythonw.exe", "scripts/scheduled_replay.py"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return plan(root, raw_file(root))


def test_registration_uses_fixed_xml_no_catchup_or_credentials(tmp_path):
    job = setup(tmp_path)
    calls = []
    schedule_replay(tmp_path, job, NOW + timedelta(minutes=2), now=NOW,
        run=lambda *a, **k: calls.append((a, k)) or SimpleNamespace(returncode=0))
    xml = ET.parse(tmp_path / "operations_state" / f"schedule_{job}.xml")
    find = lambda key: xml.find(f".//{{{NS}}}{key}").text
    assert find("StartWhenAvailable") == "false"
    assert find("LogonType") == "InteractiveToken"
    assert find("RunLevel") == "LeastPrivilege"
    assert find("Command").endswith("pythonw.exe")
    assert job in find("Arguments")
    assert "/F" not in calls[0][0][0]
    assert schedules(tmp_path)[0]["registration"] == "registered"
    assert not dispatch_schedule(tmp_path, job, now=NOW)
    assert dispatch_schedule(tmp_path, job, now=NOW + timedelta(minutes=2))
    assert not dispatch_schedule(tmp_path, job, now=NOW + timedelta(minutes=3))
    assert JobStore(tmp_path).recent()[0]["status"] == "queued"


def test_missed_or_cancelled_reservation_cannot_execute(tmp_path):
    job = setup(tmp_path)
    schedule_replay(tmp_path, job, NOW + timedelta(minutes=2), now=NOW, run=lambda *a, **k: SimpleNamespace(returncode=0))
    assert expire_missed(tmp_path, now=NOW + timedelta(hours=1)) == 1
    assert not dispatch_schedule(tmp_path, job, now=NOW + timedelta(hours=1))
    with pytest.raises(ValueError):
        JobStore(tmp_path).queue_replay(job)
    assert JobStore(tmp_path).cancel(job)
    assert not dispatch_schedule(tmp_path, job, now=NOW + timedelta(minutes=2))


def test_cancel_before_due_blocks_the_os_action(tmp_path):
    job = setup(tmp_path)
    schedule_replay(tmp_path, job, NOW + timedelta(minutes=2), now=NOW, run=lambda *a, **k: SimpleNamespace(returncode=0))
    assert JobStore(tmp_path).cancel(job)
    assert schedules(tmp_path)[0]["state"] == "cancelled"
    assert not dispatch_schedule(tmp_path, job, now=NOW + timedelta(minutes=2))
    assert JobStore(tmp_path).recent()[0]["status"] == "cancelled"


def test_uncertain_registration_remains_durable_and_is_not_repeated(tmp_path):
    job = setup(tmp_path)
    calls = []
    def run(*a, **k):
        calls.append(True)
        raise TimeoutError("unknown registration outcome")
    with pytest.raises(RuntimeError):
        schedule_replay(tmp_path, job, NOW + timedelta(minutes=2), now=NOW, run=run)
    assert schedules(tmp_path)[0]["registration"] == "unknown"
    with pytest.raises(Exception):
        schedule_replay(tmp_path, job, NOW + timedelta(minutes=3), now=NOW, run=run)
    assert len(calls) == 1


@pytest.mark.parametrize("due", [NOW, NOW.replace(tzinfo=None), NOW + timedelta(days=8),
                                  datetime(2026, 9, 17, 1, tzinfo=timezone.utc)])
def test_invalid_or_market_time_is_not_scheduled(tmp_path, due):
    job = setup(tmp_path)
    with pytest.raises(ValueError):
        schedule_replay(tmp_path, job, due, now=NOW, run=lambda *a, **k: pytest.fail("must not register"))
    assert schedules(tmp_path) == []
