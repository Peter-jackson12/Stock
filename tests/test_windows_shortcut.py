"""Synthetic tests for per-user Windows Stock Operator shortcuts."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from control_tower import windows_shortcut as shortcut


def ready_json(location="desktop", **changes):
    value = {
        "schema": shortcut.SCHEMA,
        "supported": True,
        "location": location,
        "action": "status",
        "changed": False,
        "state": "ready",
        "exists": True,
        "owned": True,
        "matches_expected": True,
        "path": "C:/Users/user/Desktop/Stock Operator.lnk",
        "target": "C:/Projects/TotalStock/Stock/stock.cmd",
        "arguments": "ui",
        "working_directory": "C:/Projects/TotalStock/Stock",
        "description": shortcut.DESCRIPTION,
    }
    value.update(changes)
    return json.dumps(value)


def test_contract_is_current_user_stock_cmd_ui(tmp_path):
    root = tmp_path / "Stock 한글 ' space"
    result = shortcut.shortcut_contract(root, "desktop")
    assert result["expected_target"] == str(root.absolute() / "stock.cmd")
    assert result["expected_arguments"] == "ui"
    assert result["expected_working_directory"] == str(root.absolute())
    assert result["per_user"] is True
    assert result["requires_admin"] is False
    assert result["execution_policy_changed"] is False


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\0b"])
def test_contract_rejects_control_characters(tmp_path, bad):
    with pytest.raises(ValueError):
        shortcut.shortcut_contract(tmp_path / bad, "desktop")


def test_non_windows_fails_closed_without_invoking_powershell(tmp_path):
    calls = []
    result = shortcut.manage_shortcut(
        tmp_path, location="desktop", action="status",
        runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        platform="linux",
    )
    assert result["state"] == "unsupported"
    assert result["changed"] is False
    assert calls == []


def test_install_requires_stock_cmd_before_shell_mutation(tmp_path):
    calls = []
    result = shortcut.manage_shortcut(
        tmp_path, location="desktop", action="install",
        runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        platform="win32", environ={},
    )
    assert result["state"] == "blocked"
    assert "stock.cmd" in result["reason"]
    assert calls == []


def test_windows_call_is_per_user_static_shell_contract(tmp_path):
    (tmp_path / "stock.cmd").write_text("@echo off\n", encoding="utf-8")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=ready_json(), stderr="")

    result = shortcut.manage_shortcut(
        tmp_path, location="desktop", action="install",
        runner=runner, platform="win32", environ={},
    )
    assert result["state"] == "ready"
    assert result["expected_arguments"] == "ui"
    command, kwargs = calls[0]
    assert command[1:5] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
    script = command[-1]
    assert "WScript.Shell" in script
    assert "SpecialFolders.Item('Desktop')" in script
    assert "SpecialFolders.Item('Programs')" in script
    assert "Set-ExecutionPolicy" not in script
    assert "Start-Process" not in script
    assert "Registry" not in script and "HKLM" not in script and "HKCU" not in script
    assert kwargs["env"]["STOCK_SHORTCUT_ACTION"] == "install"
    assert kwargs["env"]["STOCK_SHORTCUT_ROOT"] == str(tmp_path.absolute())
    assert kwargs["env"]["STOCK_SHORTCUT_DESCRIPTION"] == shortcut.DESCRIPTION


def test_status_preserves_actual_stale_target_and_expected_target(tmp_path):
    def runner(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=ready_json(
                state="stale", matches_expected=False,
                target="D:/Old/Stock/stock.cmd",
                working_directory="D:/Old/Stock",
            ),
            stderr="",
        )

    result = shortcut.manage_shortcut(
        tmp_path, location="desktop", action="status",
        runner=runner, platform="win32", environ={},
    )
    assert result["state"] == "stale"
    assert result["target"] == "D:/Old/Stock/stock.cmd"
    assert result["expected_target"] == str(tmp_path.absolute() / "stock.cmd")


def test_shell_contract_is_idempotent_and_conflict_safe():
    script = shortcut._PS_SCRIPT
    assert "$before.state -ne 'conflict'" in script
    assert "$before.state -ne 'ready'" in script
    assert "$before.state -ne 'conflict' -and $before.exists" in script
    assert "$shortcut.Description -eq $description" in script


def test_powershell_failure_is_reported_without_retry(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1, stdout="", stderr="synthetic failure")

    result = shortcut.manage_shortcut(
        tmp_path, location="desktop", action="status",
        runner=runner, platform="win32", environ={},
    )
    assert result["state"] == "error"
    assert "synthetic failure" in result["reason"]
    assert len(calls) == 1
