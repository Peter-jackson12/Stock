"""Read-only admission check before one approved FID A-B-A Mock run.

The checker never instantiates OCX, logs in, subscribes, kills a process, deletes a
lock, opens raw data, or fetches Git refs. External market-day verification remains
an explicit operator/control-tower attestation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from collector.kiwoom.fid_read_ab_diagnostic import (
    DURATION_SECONDS,
    validate_cli_combination,
)
from collector.kiwoom.preflight import inspect_environment
from control_tower.storage_guard import MIN_FREE_BYTES

KST = timezone(timedelta(hours=9))
TARGET_START = dtime(9, 15)
TARGET_END = dtime(15, 15)

RUN_READY = "RUN_READY"
RUN_BLOCKED = "RUN_BLOCKED"
RUN_UNCERTAIN = "RUN_UNCERTAIN"

RUNTIME_TITLE_TERMS = (
    "runtime error",
    "openapi",
    "khopenapi",
    "kiwoom",
    "키움",
)
COLLECTOR_MARKER = "kiwoom_universe_logger.py"


@dataclass(frozen=True)
class AdmissionInputs:
    repo_root: str
    expected_revision: str
    official_market_date: str
    official_market_source_note: str
    execution_approved: bool


def _run_text(command, *, cwd: Path, timeout: float = 10.0) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise OSError(f"command failed ({completed.returncode}): {stderr[:512]}")
    return (completed.stdout or "").strip()


def inspect_git(repo_root: Path) -> dict:
    root = Path(repo_root).resolve()
    head = _run_text(["git", "rev-parse", "HEAD"], cwd=root)
    status = _run_text(
        ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
        cwd=root,
    )
    return {
        "head": head,
        "clean": not bool(status),
        "status_lines": [] if not status else status.splitlines()[:100],
        "status_truncated": bool(status and len(status.splitlines()) > 100),
        "network_used": False,
        "git_fetch_performed": False,
    }


def inspect_disk(repo_root: Path) -> dict:
    usage = shutil.disk_usage(Path(repo_root).resolve())
    return {
        "free_bytes": int(usage.free),
        "minimum_code_admission_bytes": MIN_FREE_BYTES,
        "meets_code_minimum": usage.free >= MIN_FREE_BYTES,
        "note": (
            "The 256 MiB threshold is the existing code admission floor only; "
            "passing it does not certify capacity for every possible capture."
        ),
    }


def _powershell_json(script: str, *, cwd: Path) -> dict:
    text = _run_text(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=cwd,
        timeout=15.0,
    )
    if not text:
        return {}
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("PowerShell process probe must return a JSON object")
    return value


def inspect_processes(repo_root: Path, *, own_pid: int | None = None) -> dict:
    """Read process/window metadata only. No handle mutation, kill, or window action."""
    root = Path(repo_root).resolve()
    own_pid = os.getpid() if own_pid is None else own_pid
    script = r"""
$ErrorActionPreference = 'Stop'
$py = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Select-Object ProcessId,Name,CommandLine,ExecutablePath)
$windows = @(Get-Process | Where-Object { $_.MainWindowTitle } |
  Select-Object Id,ProcessName,MainWindowTitle)
[pscustomobject]@{python_processes=$py; titled_windows=$windows} |
  ConvertTo-Json -Depth 5 -Compress
"""
    raw = _powershell_json(script, cwd=root)
    py = raw.get("python_processes")
    windows = raw.get("titled_windows")
    py = py if isinstance(py, list) else ([] if py is None else [py])
    windows = windows if isinstance(windows, list) else ([] if windows is None else [windows])

    collector = []
    same_runtime_other = []
    current_exe = os.path.normcase(os.path.normpath(sys.executable))
    for item in py:
        if not isinstance(item, dict):
            continue
        pid = item.get("ProcessId")
        if pid == own_pid:
            continue
        command_line = item.get("CommandLine")
        executable = item.get("ExecutablePath")
        command_text = command_line if isinstance(command_line, str) else ""
        if COLLECTOR_MARKER.lower() in command_text.lower():
            collector.append({
                "pid": pid,
                "name": item.get("Name"),
                "command_line": command_text[:2048],
                "executable": executable,
            })
            continue
        if isinstance(executable, str):
            normalized = os.path.normcase(os.path.normpath(executable))
            if normalized == current_exe:
                same_runtime_other.append({
                    "pid": pid,
                    "name": item.get("Name"),
                    "command_line": command_text[:2048] if command_text else None,
                    "executable": executable,
                })

    runtime_windows = []
    for item in windows:
        if not isinstance(item, dict):
            continue
        title = item.get("MainWindowTitle")
        if not isinstance(title, str):
            continue
        lowered = title.lower()
        if any(term in lowered for term in RUNTIME_TITLE_TERMS):
            runtime_windows.append({
                "pid": item.get("Id"),
                "process_name": item.get("ProcessName"),
                "title": title[:512],
            })

    return {
        "probe_ok": True,
        "collector_processes": collector,
        "same_python_runtime_other_processes": same_runtime_other,
        "runtime_or_openapi_windows": runtime_windows,
        "own_pid_excluded": own_pid,
        "actions_taken": [],
    }


def inspect_existing_lease(repo_root: Path) -> dict:
    """Check an existing one-byte lease without creating or deleting it."""
    path = Path(repo_root).resolve() / "operations_state" / "kiwoom_collector.lock"
    if not path.exists():
        return {"path": str(path), "state": "absent", "confirmed_free": True, "file_created": False}
    if os.name != "nt":
        return {
            "path": str(path),
            "state": "probe_unavailable",
            "confirmed_free": False,
            "file_created": False,
            "error": "Windows lease probe required",
        }
    try:
        size = path.stat().st_size
        if size < 1:
            return {
                "path": str(path),
                "state": "malformed_existing_file",
                "confirmed_free": False,
                "file_created": False,
            }
        import msvcrt
        with path.open("r+b") as stream:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                return {
                    "path": str(path),
                    "state": "not_confirmed_free",
                    "confirmed_free": False,
                    "file_created": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return {"path": str(path), "state": "free", "confirmed_free": True, "file_created": False}
    except OSError as exc:
        return {
            "path": str(path),
            "state": "probe_error",
            "confirmed_free": False,
            "file_created": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def exact_diagnostic_cli_contract() -> dict:
    error = validate_cli_combination(
        enabled=True,
        storage="raw-v2",
        capture_telemetry=True,
        codes=None,
        plan=None,
        aftermarket_given=False,
        explicit_ocx_teardown=False,
        duration_seconds=DURATION_SECONDS,
        managed_launch=False,
    )
    return {
        "valid": error is None,
        "error": error,
        "storage": "raw-v2",
        "capture_telemetry": True,
        "fid_read_ab_test": True,
        "duration_seconds": DURATION_SECONDS,
        "full_universe": True,
        "nxt": False,
        "aftermarket": False,
        "managed_launch": False,
        "explicit_ocx_teardown": False,
    }


def _parse_market_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def evaluate_admission(
    inputs: AdmissionInputs,
    *,
    now_kst: datetime,
    git: dict,
    preflight: dict,
    disk: dict,
    processes: dict,
    lease: dict,
    cli_contract: dict,
) -> dict:
    if now_kst.utcoffset() != timedelta(hours=9):
        raise ValueError("now_kst must use explicit UTC+09:00")

    blockers = []
    uncertain = []

    if git.get("head") != inputs.expected_revision:
        blockers.append({"id": "revision", "reason": f"HEAD={git.get('head')}"})
    if git.get("clean") is not True:
        blockers.append({"id": "working_tree", "reason": "tracked/untracked changes present"})

    required_preflight = {
        "python_bits": 32,
        "login_attempted": False,
        "ocx_instantiated": False,
        "ready": True,
        "ocx_registered": True,
        "ocx_file_exists": True,
    }
    for key, expected in required_preflight.items():
        if preflight.get(key) != expected:
            blockers.append({
                "id": "preflight",
                "reason": f"{key}={preflight.get(key)!r}, expected={expected!r}",
            })

    if disk.get("meets_code_minimum") is not True:
        blockers.append({"id": "disk", "reason": f"free_bytes={disk.get('free_bytes')}"})

    if processes.get("probe_ok") is not True:
        uncertain.append({"id": "process_probe", "reason": str(processes.get("error"))[:256]})
    else:
        if processes.get("collector_processes"):
            blockers.append({"id": "collector_process", "reason": "another collector process observed"})
        if processes.get("runtime_or_openapi_windows"):
            blockers.append({"id": "runtime_window", "reason": "Runtime/OpenAPI/Kiwoom titled window observed"})
        if processes.get("same_python_runtime_other_processes"):
            uncertain.append({
                "id": "same_python_runtime",
                "reason": "another process uses the same 32-bit Python executable",
            })

    if lease.get("confirmed_free") is not True:
        blockers.append({"id": "collector_lease", "reason": str(lease.get("state"))})

    if cli_contract.get("valid") is not True:
        blockers.append({"id": "cli_contract", "reason": str(cli_contract.get("error"))})

    market_date = _parse_market_date(inputs.official_market_date)
    if market_date is None:
        blockers.append({"id": "market_date", "reason": "valid official market date attestation required"})
    elif market_date != now_kst.date():
        blockers.append({
            "id": "market_date",
            "reason": f"attested={market_date.isoformat()}, current={now_kst.date().isoformat()}",
        })
    if not isinstance(inputs.official_market_source_note, str) or not inputs.official_market_source_note.strip():
        blockers.append({"id": "market_source", "reason": "official market source note required"})

    if not (TARGET_START <= now_kst.time().replace(tzinfo=None) < TARGET_END):
        blockers.append({
            "id": "target_window",
            "reason": f"current_kst={now_kst.isoformat()}, required=09:15<=time<15:15",
        })

    if inputs.execution_approved is not True:
        blockers.append({"id": "execution_approval", "reason": "explicit user approval required"})

    status = RUN_BLOCKED if blockers else RUN_UNCERTAIN if uncertain else RUN_READY
    return {
        "schema": "fid_read_ab_admission_v1",
        "status": status,
        "observed_at_kst": now_kst.isoformat(),
        "inputs": asdict(inputs),
        "checks": {
            "git": git,
            "preflight": preflight,
            "disk": disk,
            "processes": processes,
            "lease": lease,
            "cli_contract": cli_contract,
            "market_attestation": {
                "date": inputs.official_market_date,
                "source_note": inputs.official_market_source_note,
                "external_attestation_only": True,
                "target_window_kst": "09:15<=time<15:15",
            },
        },
        "blockers": blockers,
        "uncertain": uncertain,
        "non_actions": [
            "no git fetch/reset/stash/clean",
            "no OCX instantiation/login/SetRealReg",
            "no process kill/restart/relogin/window close",
            "no lock deletion",
            "no raw database open/scan/count/hash",
        ],
        "note": (
            "RUN_READY means only the pre-run admission contract is satisfied at this observation time. "
            "It does not certify server availability, feed delivery, native stability, or capture success."
        ),
    }


def collect_and_evaluate(
    inputs: AdmissionInputs,
    *,
    now_kst: datetime | None = None,
    process_probe=inspect_processes,
) -> dict:
    root = Path(inputs.repo_root).resolve()
    observed = datetime.now(KST) if now_kst is None else now_kst
    git = inspect_git(root)
    preflight = inspect_environment()
    disk = inspect_disk(root)
    try:
        processes = process_probe(root)
    except Exception as exc:
        processes = {
            "probe_ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "collector_processes": [],
            "same_python_runtime_other_processes": [],
            "runtime_or_openapi_windows": [],
            "actions_taken": [],
        }
    lease = inspect_existing_lease(root)
    cli = exact_diagnostic_cli_contract()
    return evaluate_admission(
        inputs,
        now_kst=observed,
        git=git,
        preflight=preflight,
        disk=disk,
        processes=processes,
        lease=lease,
        cli_contract=cli,
    )
