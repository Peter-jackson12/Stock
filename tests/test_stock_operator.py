"""Operator UX 합성 검사. 사용자 PC/OCX/실데이터를 사용하지 않는다."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType

import pytest

from scripts import stock_operator as operator

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    root = tmp_path / "Stock 한글 ' space"
    for relative in operator.REQUIRED_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    runtime = {"platform": "win32", "version": (3, 14, 0), "bits": 64,
               "prefix": str(root / ".venv"), "executable": str(root / ".venv/Scripts/python.exe")}
    monkeypatch.setattr(operator, "_runtime", lambda: runtime)
    monkeypatch.setattr(operator.metadata, "version", lambda name: "1.0-fixture")
    return root, runtime


def fake_readers(monkeypatch, root, *, raw_status="unavailable", claim=None):
    calls = []
    module = ModuleType("control_tower.status")

    def log_reader(actual_root, *, now):
        calls.append(("log", actual_root, now))
        return {"status": "unavailable", "log_path": str(root / "logs/fixture.log")}

    def raw_reader(actual_root, *, now):
        calls.append(("raw", actual_root, now))
        result = {"status": raw_status, "path": str(root / "operations_state/capture_status.json")}
        if claim is not None:
            result["payload"] = {"observed_at_utc": "2026-01-01T00:00:00+00:00",
                "identity": {"session_id": "fixture"},
                "snapshot": {"state": claim, "accepted_callbacks": 10, "committed_seq": 12,
                             "pending_callbacks": 0}}
            result["age_seconds"] = 99
        return result

    module.observe_collector = log_reader
    module.observe_raw_capture = raw_reader
    monkeypatch.setitem(sys.modules, "control_tower.status", module)
    return calls


def test_help_without_environment_or_readers(tmp_path, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("help must not inspect anything")
    monkeypatch.setattr(operator, "doctor", forbidden)
    monkeypatch.setattr(operator, "status", forbidden)
    monkeypatch.setattr(operator, "ui_command", forbidden)
    assert operator.main([], root=tmp_path) == 0
    assert "START_HERE.md" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("command", ["start", "stop", "canary", "preflight", "backtest", "ui", "collector"])
def test_unknown_or_execution_command_fails_before_io(command, tmp_path):
    with pytest.raises(SystemExit) as error:
        operator.main([command], root=tmp_path)
    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_doctor_success_is_inventory_only(inventory):
    root, _ = inventory
    report = operator.doctor(root)
    assert report["inventory_passed"] is True
    assert report["operator_ready"] == "unverified"
    assert report["runtime_imports_verified"] is False
    assert report["ocx_verified"] is False
    assert report["execution_approved"] is False
    optional = next(c for c in report["checks"] if c["name"] == "collector_runtime_file")
    assert optional["status"] == "WARN"


@pytest.mark.parametrize("field,value", [("platform", "linux"), ("bits", 32),
    ("version", (3, 13, 9)), ("prefix", "/wrong-environment")])
def test_doctor_rejects_wrong_environment(inventory, field, value):
    root, runtime = inventory
    runtime[field] = value
    assert operator.doctor(root)["inventory_passed"] is False


def test_doctor_missing_package_does_not_install(inventory, monkeypatch):
    root, _ = inventory
    def missing(name):
        raise operator.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(operator.metadata, "version", missing)
    report = operator.doctor(root)
    assert report["inventory_passed"] is False
    assert all(c["status"] == "FAIL" for c in report["checks"] if c["name"].startswith("package:"))
    assert not (root / ".venv").exists()


def test_doctor_missing_entrypoint(inventory):
    root, _ = inventory
    (root / "dashboard/app.py").unlink()
    assert operator.doctor(root)["inventory_passed"] is False


def test_doctor_does_not_open_or_execute_venv32(inventory, monkeypatch):
    root, _ = inventory
    path = root / ".venv32/Scripts/python.exe"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not an executable")
    def forbidden(*args, **kwargs):
        raise AssertionError("must not execute a child process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    report = operator.doctor(root)
    check = next(c for c in report["checks"] if c["name"] == "collector_runtime_file")
    assert check["status"] == "INFO"
    assert report["ocx_verified"] is False


@pytest.mark.parametrize("saved_status,claim", [("unavailable", None), ("recent", "running"),
    ("stale", "closed"), ("clock_ahead", "closed"), ("recent", "failed")])
def test_saved_evidence_never_becomes_live_readiness(monkeypatch, tmp_path, saved_status, claim):
    calls = fake_readers(monkeypatch, tmp_path, raw_status=saved_status, claim=claim)
    report = operator.status(tmp_path)
    assert report["raw_status_evidence"]["status"] == saved_status
    assert report["raw_status_evidence"]["producer_claim_state"] == claim
    assert all(report[key] == "unverified" for key in operator.UNVERIFIED)
    assert report["execution_approved"] is False
    assert len(calls) == 2 and calls[0][1:] == calls[1][1:]
    assert list(tmp_path.iterdir()) == []


def test_status_does_not_leak_raw_payload_or_recent_messages(monkeypatch, tmp_path):
    fake_readers(monkeypatch, tmp_path, raw_status="recent", claim="running")
    report = operator.status(tmp_path)
    assert "payload" not in report["raw_status_evidence"]
    assert "recent_messages" not in report["log_evidence"]


def test_ui_command_quotes_paths_and_never_launches(tmp_path, monkeypatch):
    root = tmp_path / "Stock 한글 ' $() ` ; space"
    def forbidden(*args, **kwargs):
        raise AssertionError("must not launch")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    report = operator.ui_command(root)
    assert report["launched"] is False
    assert report["execution_approved"] is False
    assert "''" in report["command"]
    assert "Set-Location -LiteralPath '" in report["command"]
    assert "--server.address 127.0.0.1" in report["command"]
    assert "읽기 전용이 아닙니다" in report["warning"]
    assert not root.exists()


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\0b"])
def test_ui_command_rejects_control_characters(bad):
    with pytest.raises(ValueError):
        operator._ps_quote(bad)


def test_json_doctor_exit_and_schema(inventory, capsys):
    root, runtime = inventory
    runtime["bits"] = 32
    assert operator.main(["doctor", "--json"], root=root) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "stock_operator_v1"
    assert report["inventory_passed"] is False


def test_json_error_is_nonzero(monkeypatch, tmp_path, capsys):
    def fail(root):
        raise PermissionError("synthetic permission denied")
    monkeypatch.setattr(operator, "status", fail)
    assert operator.main(["status", "--json"], root=tmp_path) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "PermissionError"


def test_read_only_source_has_no_execution_api():
    import ast
    tree = ast.parse(Path(operator.__file__).read_text(encoding="utf-8"))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert not imports.intersection({"subprocess", "sqlite3", "streamlit", "webbrowser", "winreg"})
    assert "control_tower.status" in imports


# 기존 reader와의 결합 검사. pytest 임시 fixture만 사용한다.
def test_integration_empty_root_is_unknown_and_does_not_create_files(tmp_path):
    report = operator.status(tmp_path)
    assert report["log_evidence"]["status"] == "unavailable"
    assert report["raw_status_evidence"]["status"] == "unavailable"
    assert report["process_state"] == "unverified"
    assert list(tmp_path.iterdir()) == []


def test_integration_invalid_status_is_not_accepted(tmp_path):
    state = tmp_path / "operations_state/capture_status.json"
    state.parent.mkdir()
    original = b'{"state":"closed"}'
    state.write_bytes(original)
    report = operator.status(tmp_path)
    assert report["raw_status_evidence"]["status"] == "unavailable"
    assert report["raw_status_evidence"]["producer_claim_state"] is None
    assert state.read_bytes() == original


def test_integration_oversized_status_is_not_accepted(tmp_path):
    state = tmp_path / "operations_state/capture_status.json"
    state.parent.mkdir()
    state.write_bytes(b"x" * (64 * 1024 + 1))
    report = operator.status(tmp_path)
    assert report["raw_status_evidence"]["status"] == "unavailable"
    assert state.stat().st_size == 64 * 1024 + 1


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell bootstrap integration")
def test_powershell_help_and_missing_venv_do_not_fallback(tmp_path, shell):
    executable = shutil.which(shell)
    assert executable, f"Windows CI must provide {shell}"
    root = tmp_path / "Stock space"
    root.mkdir()
    launcher = root / "stock.ps1"
    shutil.copyfile(ROOT / "stock.ps1", launcher)
    for command, expected in [("help", 0), ("doctor", 2), ("collector", 2)]:
        run = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-File", str(launcher), command],
                             capture_output=True, timeout=30)
        assert run.returncode == expected, (run.stdout, run.stderr)
    assert list(root.iterdir()) == [launcher]


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell argv and exit integration")
def test_powershell_forwards_exact_argv_and_exit_without_changing_cwd(tmp_path, shell):
    executable = shutil.which(shell)
    assert executable, f"Windows CI must provide {shell}"
    root = tmp_path / "Stock space"
    root.mkdir()
    shutil.copyfile(ROOT / "stock.ps1", root / "stock.ps1")
    # 잘못된 runtime fixture. 기동 실패를 성공으로 바꾸지 않는다.
    python = root / ".venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"not a PE file")
    run = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-File",
                          str(root / "stock.ps1"), "doctor", "-Json"],
                         capture_output=True, cwd=tmp_path, timeout=30)
    assert run.returncode != 0
    assert not (root / "operations_state").exists()
    assert not (root / ".venv32").exists()


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
@pytest.mark.skipif(sys.platform != "win32", reason="Windows argv and exact exit propagation")
def test_powershell_valid_runtime_forwards_arguments_and_exit(tmp_path, shell):
    executable = shutil.which(shell)
    assert executable, f"Windows CI must provide {shell}"
    root = tmp_path / "Stock 한글 ' space"
    root.mkdir()
    shutil.copyfile(ROOT / "stock.ps1", root / "stock.ps1")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")],
                   check=True, capture_output=True, timeout=30)
    entry = root / "scripts/stock_operator.py"
    entry.parent.mkdir()
    entry.write_text(
        "import sys,json\n"
        "print(json.dumps({'args':sys.argv[1:],'isolated':sys.flags.isolated,'no_bytecode':sys.dont_write_bytecode}))\n"
        "raise SystemExit(7)\n", encoding="utf-8")
    run = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-File",
                          str(root / "stock.ps1"), "doctor", "-Json"],
                         capture_output=True, cwd=tmp_path, timeout=30)
    assert run.returncode == 7, (run.stdout, run.stderr)
    result = json.loads(run.stdout.decode("utf-8-sig"))
    assert result == {"args": ["doctor", "--json"], "isolated": 1, "no_bytecode": True}
    assert not (root / "operations_state").exists()
    assert not (entry.parent / "__pycache__").exists()


def test_integration_start_here_links_and_readme_entry():
    import runpy
    documentation = runpy.run_path(str(ROOT / "tests/test_documentation.py"))
    assert not documentation["link_errors"](ROOT, "START_HERE.md")
    assert "[처음 실행하는 사람](START_HERE.md)" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "[Operator 시작 안내](START_HERE.md)" in (ROOT / "HANDOFF.md").read_text(encoding="utf-8")
