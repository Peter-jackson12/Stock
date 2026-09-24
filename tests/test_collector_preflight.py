"""Synthetic tests for the Operator's read-only collector preflight."""
from pathlib import Path
from types import SimpleNamespace

from control_tower.collector_preflight import (
    BLOCKED,
    PASS,
    UNVERIFIED,
    inspect_collector_preflight,
    reduce_collector_preflight,
    run_official_preflight,
)


def official_ready():
    return {
        "probe_ok": True,
        "result": {
            "python_bits": 32,
            "login_attempted": False,
            "ocx_instantiated": False,
            "ready": True,
            "ocx_registered": True,
            "ocx_file_exists": True,
        },
    }


def process_clear():
    return {
        "probe_ok": True,
        "collector_processes": [],
        "same_python_runtime_other_processes": [],
        "runtime_or_openapi_windows": [],
        "unidentified_python_processes": [],
        "actions_taken": [],
    }


def reduced(**overrides):
    values = {
        "runtime": {"exists": True, "path": "C:/fixture/.venv32/Scripts/python.exe"},
        "official_preflight": official_ready(),
        "disk": {
            "free_bytes": 10**9,
            "minimum_code_admission_bytes": 256 * 1024 * 1024,
            "meets_code_minimum": True,
        },
        "processes": process_clear(),
        "lease": {"state": "absent", "confirmed_free": True, "file_created": False},
    }
    values.update(overrides)
    return reduce_collector_preflight(**values)


def by_id(report):
    return {item["id"]: item for item in report["checks"]}


def test_observed_local_facts_pass_but_run_contract_market_and_approval_stay_unverified():
    report = reduced()
    checks = by_id(report)
    assert report["local_status"] == UNVERIFIED
    assert report["status"] == UNVERIFIED
    assert all(checks[item]["status"] == PASS for item in (
        "collector_runtime", "official_preflight", "storage_floor", "collector_process",
        "runtime_window", "collector_lease",
    ))
    assert checks["run_contract"]["status"] == UNVERIFIED
    assert checks["market_session"]["status"] == UNVERIFIED
    assert checks["execution_approval"]["status"] == UNVERIFIED
    assert report["execution_approved"] is False
    assert report["market_verified"] is False
    assert any("no OCX" in action for action in report["non_actions"])
    assert "시작 버튼 상태를 바꾸지 않습니다" in report["note"]


def test_known_runtime_process_window_and_lease_conflicts_are_blocked():
    process = process_clear()
    process["collector_processes"] = [{"pid": 11}]
    process["runtime_or_openapi_windows"] = [{"pid": 12, "title": "Runtime Error"}]
    report = reduced(
        runtime={"exists": False},
        official_preflight={"probe_ok": False, "error": "must not run"},
        processes=process,
        lease={"state": "not_confirmed_free", "confirmed_free": False, "file_created": False},
    )
    checks = by_id(report)
    assert report["status"] == report["local_status"] == BLOCKED
    assert checks["collector_runtime"]["status"] == BLOCKED
    assert checks["official_preflight"]["status"] == BLOCKED
    assert checks["collector_process"]["status"] == BLOCKED
    assert checks["runtime_window"]["status"] == BLOCKED
    assert checks["collector_lease"]["status"] == BLOCKED


def test_probe_gaps_never_become_pass_or_absence():
    report = reduced(
        official_preflight={"probe_ok": False, "error": "timeout"},
        processes={"probe_ok": False, "error": "AccessDenied"},
        lease={"state": "probe_error", "confirmed_free": False, "file_created": False},
    )
    checks = by_id(report)
    assert report["local_status"] == UNVERIFIED
    assert checks["official_preflight"]["status"] == UNVERIFIED
    assert checks["collector_process"]["status"] == UNVERIFIED
    assert checks["runtime_window"]["status"] == UNVERIFIED
    assert checks["collector_lease"]["status"] == UNVERIFIED


def test_same_runtime_or_unidentified_python_keeps_conflict_unverified():
    process = process_clear()
    process["same_python_runtime_other_processes"] = [{"pid": 22}]
    process["unidentified_python_processes"] = [{"pid": 23}]
    check = by_id(reduced(processes=process))["collector_process"]
    assert check["status"] == UNVERIFIED
    assert "동일 런타임 1개" in check["detail"]
    assert "식별 불가 Python 1개" in check["detail"]


def test_same_ui_runtime_without_collector_marker_is_warn_only():
    process = process_clear()
    process["same_python_runtime_other_processes"] = [{"pid": 22}]
    check = by_id(reduced(processes=process))["collector_process"]
    assert check["status"] == "WARN"


def test_reader_uses_injected_readers_without_creating_state(tmp_path):
    executable = tmp_path / ".venv32/Scripts/python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    calls = []

    def official(runtime, module):
        calls.append((runtime, module))
        return official_ready()

    report = inspect_collector_preflight(
        tmp_path,
        official_runner=official,
        disk_reader=lambda root: {
            "free_bytes": 10**9,
            "minimum_code_admission_bytes": 256 * 1024 * 1024,
            "meets_code_minimum": True,
        },
        process_reader=lambda root: process_clear(),
        lease_reader=lambda root: {
            "state": "absent", "confirmed_free": True, "file_created": False,
        },
    )
    assert report["local_status"] == UNVERIFIED
    assert calls == [(executable, tmp_path / "collector/kiwoom/preflight.py")]
    assert not (tmp_path / "operations_state").exists()


def test_missing_runtime_never_invokes_the_32_bit_runner(tmp_path):
    calls = []
    report = inspect_collector_preflight(
        tmp_path,
        official_runner=lambda *args: calls.append(args),
        disk_reader=lambda root: {},
        process_reader=lambda root: {"probe_ok": False, "error": "fixture"},
        lease_reader=lambda root: {"state": "absent", "confirmed_free": True, "file_created": False},
    )
    assert not calls
    assert by_id(report)["collector_runtime"]["status"] == BLOCKED
    assert not (tmp_path / "operations_state").exists()


def test_official_runner_loads_only_the_pure_preflight_module():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"python_bits":32,"login_attempted":false,"ocx_instantiated":false,'
                '"ready":true,"ocx_registered":true,"ocx_file_exists":true}'
            ),
            stderr="",
        )

    result = run_official_preflight(
        Path("C:/fixture/python.exe"), Path("C:/fixture/collector/kiwoom/preflight.py"), runner=runner,
    )
    assert result["probe_ok"] is True
    command, kwargs = calls[0]
    assert Path(command[0]) == Path("C:/fixture/python.exe")
    assert command[1:4] == ["-I", "-B", "-c"]
    assert Path(command[-1]) == Path("C:/fixture/collector/kiwoom/preflight.py")
    assert "kiwoom_universe_logger" not in " ".join(command)
    assert kwargs["timeout"] == 10.0 and kwargs["check"] is False
