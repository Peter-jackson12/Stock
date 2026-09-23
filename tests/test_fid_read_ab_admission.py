from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from collector.kiwoom.fid_read_ab_admission import (
    AdmissionInputs,
    KST,
    RUN_BLOCKED,
    RUN_READY,
    RUN_UNCERTAIN,
    evaluate_admission,
    exact_diagnostic_cli_contract,
    inspect_existing_lease,
    inspect_processes,
)

REV = "e2c4b282e11b3ef6eafe1882c01a69e15155a394"


def inputs(**overrides):
    value = dict(
        repo_root="C:/fixture",
        expected_revision=REV,
        official_market_date="2026-09-28",
        official_market_source_note="KRX official trading-day page checked at 2026-09-28T09:10+09:00",
        execution_approved=True,
    )
    value.update(overrides)
    return AdmissionInputs(**value)


ENTRY_BLOB = "2" * 40  # synthetic tracked blob id; no Git object is read.


def git_facts(root="C:/fixture"):
    return {
        "head": REV,
        "clean": True,
        "status_lines": [],
        "status_truncated": False,
        "toplevel": root,
        "checker_source_root": root,
        "entrypoint": {
            "path": "collector/kiwoom/kiwoom_universe_logger.py",
            "tracked": True,
            "index_stage": 0,
            "index_blob": ENTRY_BLOB,
            "head_blob": ENTRY_BLOB,
            "worktree_blob": ENTRY_BLOB,
        },
        "hidden_index_flag_count": 0,
        "network_used": False,
    }


def facts():
    return dict(
        now_kst=datetime(2026, 9, 28, 10, 0, tzinfo=KST),
        git=git_facts(),
        preflight={
            "executable": "C:/Python310-32/python.exe",
            "python_bits": 32,
            "login_attempted": False,
            "ocx_instantiated": False,
            "ready": True,
            "ocx_registered": True,
            "ocx_file_exists": True,
        },
        disk={"free_bytes": 10**12, "meets_code_minimum": True},
        processes={
            "probe_ok": True,
            "collector_processes": [],
            "same_python_runtime_other_processes": [],
            "runtime_or_openapi_windows": [],
            "unidentified_python_processes": [],
            "actions_taken": [],
        },
        lease={"state": "absent", "confirmed_free": True, "file_created": False},
        cli_contract=exact_diagnostic_cli_contract(),
    )


def assess(admission_inputs=None, **changes):
    f = facts()
    f.update(changes)
    return evaluate_admission(admission_inputs or inputs(), **f)


def test_ready_requires_every_local_and_external_admission_condition():
    report = assess()
    assert report["status"] == RUN_READY
    assert report["blockers"] == []
    assert report["uncertain"] == []
    assert report["checks"]["market_attestation"]["external_attestation_only"] is True
    assert report["checks"]["processes"]["actions_taken"] == []
    assert any("no OCX" in item for item in report["non_actions"])


@pytest.mark.parametrize("change,blocker", [
    ({"git": {"head": "other", "clean": True}}, "revision"),
    ({"git": {"head": REV, "clean": False}}, "working_tree"),
    ({"preflight": {"python_bits": 64}}, "preflight"),
    ({"disk": {"free_bytes": 1, "meets_code_minimum": False}}, "disk"),
    ({"lease": {"state": "not_confirmed_free", "confirmed_free": False}}, "collector_lease"),
    ({"cli_contract": {"valid": False, "error": "bad combo"}}, "cli_contract"),
])
def test_hard_local_failures_block_before_login(change, blocker):
    report = assess(**change)
    assert report["status"] == RUN_BLOCKED
    assert blocker in {item["id"] for item in report["blockers"]}


def test_collector_and_runtime_window_are_blockers():
    proc = facts()["processes"]
    proc = {
        **proc,
        "collector_processes": [{"pid": 11}],
        "runtime_or_openapi_windows": [{"pid": 12, "title": "Microsoft Visual C++ Runtime Error"}],
    }
    report = assess(processes=proc)
    assert report["status"] == RUN_BLOCKED
    assert {"collector_process", "runtime_window"} <= {i["id"] for i in report["blockers"]}


def test_same_python_runtime_without_known_collector_is_uncertain():
    proc = facts()["processes"]
    proc = {**proc, "same_python_runtime_other_processes": [{"pid": 22}]}
    report = assess(processes=proc)
    assert report["status"] == RUN_UNCERTAIN
    assert {i["id"] for i in report["uncertain"]} == {"same_python_runtime"}


def test_process_probe_failure_is_uncertain_not_ready():
    proc = {
        "probe_ok": False,
        "error": "AccessDenied",
        "collector_processes": [],
        "same_python_runtime_other_processes": [],
        "runtime_or_openapi_windows": [],
        "actions_taken": [],
    }
    report = assess(processes=proc)
    assert report["status"] == RUN_UNCERTAIN
    assert report["uncertain"][0]["id"] == "process_probe"


@pytest.mark.parametrize("admission_inputs,now,blocker", [
    (inputs(execution_approved=False), datetime(2026, 9, 28, 10, 0, tzinfo=KST), "execution_approval"),
    (inputs(official_market_date="2026-09-27"), datetime(2026, 9, 28, 10, 0, tzinfo=KST), "market_date"),
    (inputs(official_market_date="bad"), datetime(2026, 9, 28, 10, 0, tzinfo=KST), "market_date"),
    (inputs(official_market_source_note=""), datetime(2026, 9, 28, 10, 0, tzinfo=KST), "market_source"),
    (inputs(), datetime(2026, 9, 28, 9, 14, 59, tzinfo=KST), "target_window"),
    (inputs(), datetime(2026, 9, 28, 15, 15, 0, tzinfo=KST), "target_window"),
])
def test_market_and_user_approval_are_explicit_blockers(admission_inputs, now, blocker):
    report = assess(admission_inputs, now_kst=now)
    assert report["status"] == RUN_BLOCKED
    assert blocker in {item["id"] for item in report["blockers"]}


def test_blocker_takes_precedence_over_uncertainty():
    proc = facts()["processes"]
    proc = {**proc, "same_python_runtime_other_processes": [{"pid": 22}]}
    report = assess(inputs(execution_approved=False), processes=proc)
    assert report["status"] == RUN_BLOCKED
    assert report["uncertain"]


def test_wrong_timezone_is_rejected_instead_of_silently_localized():
    with pytest.raises(ValueError, match="UTC\+09"):
        assess(now_kst=datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc))


def test_exact_diagnostic_cli_contract_matches_mock_full_universe_run():
    contract = exact_diagnostic_cli_contract()
    assert contract["valid"] is True
    assert contract["storage"] == "raw-v2"
    assert contract["capture_telemetry"] is True
    assert contract["duration_seconds"] == 90
    assert contract["full_universe"] is True
    assert not contract["nxt"]
    assert not contract["aftermarket"]
    assert not contract["managed_launch"]
    assert not contract["explicit_ocx_teardown"]


def test_absent_lease_probe_does_not_create_operations_state(tmp_path):
    result = inspect_existing_lease(tmp_path)
    assert result["state"] == "absent"
    assert result["confirmed_free"] is True
    assert result["file_created"] is False
    assert not (tmp_path / "operations_state").exists()


def test_process_probe_excludes_self_and_classifies_collector_runtime_and_same_python(monkeypatch, tmp_path):
    import collector.kiwoom.fid_read_ab_admission as module

    own = 101
    same_exe = os.path.normpath(module.sys.executable)
    monkeypatch.setattr(module, "_powershell_json", lambda *a, **k: {
        "python_processes": [
            {"ProcessId": own, "Name": "python.exe", "CommandLine": "checker", "ExecutablePath": same_exe},
            {"ProcessId": 202, "Name": "python.exe",
             "CommandLine": "python collector/kiwoom/kiwoom_universe_logger.py --fid-read-ab-test",
             "ExecutablePath": same_exe},
            {"ProcessId": 303, "Name": "python.exe", "CommandLine": "python other.py",
             "ExecutablePath": same_exe},
        ],
        "python_process_count": 3,
        "marker_processes": [],
        "marker_process_count": 0,
        "titled_windows": [
            {"Id": 404, "ProcessName": "python", "MainWindowTitle": "Microsoft Visual C++ Runtime Error"},
            {"Id": 505, "ProcessName": "notepad", "MainWindowTitle": "notes"},
        ],
        "titled_window_count": 2,
    })
    report = inspect_processes(tmp_path, own_pid=own)
    assert [p["pid"] for p in report["collector_processes"]] == [202]
    assert [p["pid"] for p in report["same_python_runtime_other_processes"]] == [303]
    assert [p["pid"] for p in report["runtime_or_openapi_windows"]] == [404]
    assert report["actions_taken"] == []


def test_admission_cli_exit_codes_are_ready_blocked_and_uncertain(monkeypatch, capsys, tmp_path):
    import scripts.check_fid_read_ab_admission as script

    base_args = [
        "--repo-root", str(tmp_path),
        "--expected-revision", REV,
        "--official-market-date", "2026-09-28",
        "--official-market-source-note", "KRX checked",
        "--execution-approved",
    ]
    for status, expected_code in ((RUN_READY, 0), (RUN_BLOCKED, 2), (RUN_UNCERTAIN, 3)):
        monkeypatch.setattr(script, "collect_and_evaluate", lambda _inputs, s=status: {
            "schema": "fid_read_ab_admission_v1",
            "status": s,
        })
        code = script.main(base_args)
        assert code == expected_code
        assert json.loads(capsys.readouterr().out)["status"] == status
