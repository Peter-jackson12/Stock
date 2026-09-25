"""Per-user Windows shortcuts for the Stock Operator UI.

Only explicit user actions create or remove .lnk files. The shortcut points to
the existing stock.cmd ui entrypoint and does not grant collector execution.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable, Mapping

SCHEMA = "stock_operator_shortcut_v1"
SHORTCUT_NAME = "Stock Operator.lnk"
DESCRIPTION = "Stock Operator - stock.cmd ui"
LOCATIONS = {"desktop": "Desktop", "start_menu": "Programs"}

_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$root = $env:STOCK_SHORTCUT_ROOT
$action = $env:STOCK_SHORTCUT_ACTION
$location = $env:STOCK_SHORTCUT_LOCATION
$name = $env:STOCK_SHORTCUT_NAME
$description = $env:STOCK_SHORTCUT_DESCRIPTION
$shell = New-Object -ComObject WScript.Shell
if ($location -eq 'desktop') { $folder = $shell.SpecialFolders.Item('Desktop') }
elseif ($location -eq 'start_menu') { $folder = $shell.SpecialFolders.Item('Programs') }
else { throw "unsupported shortcut location" }
if ([string]::IsNullOrWhiteSpace($folder)) { throw "Windows user shortcut folder is unavailable" }
$path = Join-Path -Path $folder -ChildPath $name
$expectedTarget = Join-Path -Path $root -ChildPath 'stock.cmd'
$expectedArguments = 'ui'
$expectedWorking = $root

function Read-Shortcut([string]$shortcutPath) {
    if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
        return @{exists=$false;owned=$false;matches_expected=$false;state='absent';path=$shortcutPath;target=$null;arguments=$null;working_directory=$null;description=$null}
    }
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $owned = ($shortcut.Description -eq $description)
    $matches = ($shortcut.TargetPath -eq $expectedTarget -and $shortcut.Arguments -eq $expectedArguments -and $shortcut.WorkingDirectory -eq $expectedWorking -and $owned)
    $state = if ($matches) {'ready'} elseif ($owned) {'stale'} else {'conflict'}
    return @{exists=$true;owned=$owned;matches_expected=$matches;state=$state;path=$shortcutPath;target=$shortcut.TargetPath;arguments=$shortcut.Arguments;working_directory=$shortcut.WorkingDirectory;description=$shortcut.Description}
}

$before = Read-Shortcut $path
$result = $before
$changed = $false

if ($action -eq 'install') {
    if ($before.state -ne 'conflict' -and $before.state -ne 'ready') {
        $shortcut = $shell.CreateShortcut($path)
        $shortcut.TargetPath = $expectedTarget
        $shortcut.Arguments = $expectedArguments
        $shortcut.WorkingDirectory = $expectedWorking
        $shortcut.Description = $description
        $shortcut.Save()
        $result = Read-Shortcut $path
        $changed = $true
    }
} elseif ($action -eq 'remove') {
    if ($before.state -ne 'conflict' -and $before.exists) {
        Remove-Item -LiteralPath $path -Force
        $result = Read-Shortcut $path
        $changed = $true
    }
} elseif ($action -ne 'status') {
    throw "unsupported shortcut action"
}

[ordered]@{
    schema='stock_operator_shortcut_v1'
    supported=$true
    location=$location
    action=$action
    changed=$changed
    state=$result.state
    exists=$result.exists
    owned=$result.owned
    matches_expected=$result.matches_expected
    path=$result.path
    target=$result.target
    arguments=$result.arguments
    working_directory=$result.working_directory
    description=$result.description
} | ConvertTo-Json -Compress -Depth 4
"""

def _validate_text(value, label):
    text = str(value)
    if any(character in text for character in ("\r", "\n", "\x00")):
        raise ValueError(f"{label} contains a control character")
    return text

def shortcut_contract(root: Path, location: str) -> dict:
    if location not in LOCATIONS:
        raise ValueError("shortcut location must be desktop or start_menu")
    root = Path(_validate_text(Path(root).absolute(), "repository root"))
    return {
        "schema": SCHEMA,
        "location": location,
        "name": SHORTCUT_NAME,
        "expected_target": str(root / "stock.cmd"),
        "expected_arguments": "ui",
        "expected_working_directory": str(root),
        "description": DESCRIPTION,
        "per_user": True,
        "requires_admin": False,
        "execution_policy_changed": False,
    }

def _powershell_path(environ: Mapping[str, str]) -> str:
    system_root = environ.get("SystemRoot")
    if system_root:
        candidate = Path(system_root) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if candidate.is_file():
            return str(candidate)
    return "powershell.exe"

def manage_shortcut(root: Path, *, location: str, action: str,
                    runner: Callable = subprocess.run, platform=None, environ=None) -> dict:
    contract = shortcut_contract(root, location)
    if action not in {"status", "install", "remove"}:
        raise ValueError("shortcut action must be status, install, or remove")
    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return {
            **contract, "supported": False, "action": action, "changed": False,
            "state": "unsupported",
            "reason": "Windows shortcut management is available only on Windows.",
        }
    if action == "install" and not Path(contract["expected_target"]).is_file():
        return {
            **contract, "supported": True, "action": action, "changed": False,
            "state": "blocked", "reason": "stock.cmd is missing; shortcut was not created.",
        }

    child_env = dict(os.environ if environ is None else environ)
    child_env.update({
        "STOCK_SHORTCUT_ROOT": contract["expected_working_directory"],
        "STOCK_SHORTCUT_ACTION": action,
        "STOCK_SHORTCUT_LOCATION": location,
        "STOCK_SHORTCUT_NAME": SHORTCUT_NAME,
        "STOCK_SHORTCUT_DESCRIPTION": DESCRIPTION,
    })
    command = [
        _powershell_path(child_env), "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-Command", _PS_SCRIPT,
    ]
    kwargs = dict(
        text=True, encoding="utf-8", errors="replace", capture_output=True,
        timeout=15.0, check=False, env=child_env,
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        kwargs["creationflags"] = creationflags
    try:
        completed = runner(command, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            **contract, "supported": True, "action": action, "changed": False,
            "state": "error", "reason": f"{type(exc).__name__}: {str(exc)[:384]}",
        }
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:512]
        return {
            **contract, "supported": True, "action": action, "changed": False,
            "state": "error", "reason": f"PowerShell exit={completed.returncode}: {detail}",
        }
    try:
        result = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        return {
            **contract, "supported": True, "action": action, "changed": False,
            "state": "error", "reason": f"invalid shortcut JSON: {exc}",
        }
    if not isinstance(result, dict) or result.get("schema") != SCHEMA:
        return {
            **contract, "supported": True, "action": action, "changed": False,
            "state": "error", "reason": "unexpected shortcut result",
        }
    return {**contract, **result}

def inspect_shortcuts(root: Path, *, runner: Callable = subprocess.run, platform=None) -> dict:
    return {
        location: manage_shortcut(
            root, location=location, action="status", runner=runner, platform=platform
        )
        for location in LOCATIONS
    }
