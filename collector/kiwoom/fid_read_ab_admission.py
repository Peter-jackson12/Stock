"""Read-only admission check before one approved FID A-B-A Mock run.

The checker never instantiates OCX, logs in, subscribes, kills a process, deletes a
lock, opens raw data, or fetches Git refs. External market-day verification remains
an explicit operator/control-tower attestation.

The same pure evaluator decides a fresh observation and re-derives the decision
from a recorded admission (run-plan builder and verifier), so a READY label or a
valid/meets_minimum flag is never trusted on its own.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from collector.kiwoom.fid_read_ab_diagnostic import (
    DURATION_SECONDS,
    validate_cli_combination,
)
from collector.kiwoom.preflight import inspect_environment
from control_tower.storage_guard import MIN_FREE_BYTES

KST = timezone(timedelta(hours=9))
TARGET_START = dtime(9, 15)
TARGET_END = dtime(15, 15)
TARGET_WINDOW_TEXT = "09:15<=time<15:15"

ADMISSION_SCHEMA = "fid_read_ab_admission_v1"
RUN_READY = "RUN_READY"
RUN_BLOCKED = "RUN_BLOCKED"
RUN_UNCERTAIN = "RUN_UNCERTAIN"

# Evidence is valid for 0 <= age < MAX_ADMISSION_AGE_SECONDS, measured from the
# observation START (the oldest fact in the record), never from its completion.
MAX_ADMISSION_AGE_SECONDS = 60
# Allowed |wall elapsed - monotonic elapsed| during one observation.
CLOCK_TOLERANCE_SECONDS = 2.0

RUNTIME_TITLE_TERMS = (
    "runtime error",
    "openapi",
    "khopenapi",
    "kiwoom",
    "키움",
)
# Matches script (`...\kiwoom_universe_logger.py`) and module
# (`-m collector.kiwoom.kiwoom_universe_logger`) invocations in any interpreter.
COLLECTOR_MARKER = "kiwoom_universe_logger"
COLLECTOR_ENTRYPOINT = "collector/kiwoom/kiwoom_universe_logger.py"
COLLECTOR_ARGS = (
    "--storage", "raw-v2",
    "--capture-telemetry",
    "--fid-read-ab-test",
    "--duration-seconds", str(DURATION_SECONDS),
)
EXPECTED_CLI_CONTRACT = {
    "valid": True,
    "error": None,
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
PROCESS_LISTS = (
    "collector_processes",
    "same_python_runtime_other_processes",
    "runtime_or_openapi_windows",
    "unidentified_python_processes",
)
ADMISSION_KEYS = {
    "schema", "status", "observation_started_at_kst", "observed_at_kst",
    "observation_elapsed_monotonic_seconds", "inputs", "checks", "blockers",
    "uncertain", "non_actions", "note",
}
CHECK_KEYS = {"git", "preflight", "disk", "processes", "lease", "cli_contract", "market_attestation"}
# Repository-discovery overrides would let git describe a different repository.
GIT_DISCOVERY_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
)
_SHA1 = re.compile(r"[0-9a-f]{40}")
_DATETIME_TYPE = datetime  # stays the real class even if tests replace the module clock.


class AdmissionContractError(ValueError):
    pass


@dataclass(frozen=True)
class AdmissionInputs:
    repo_root: str
    expected_revision: str
    official_market_date: str
    official_market_source_note: str
    execution_approved: bool


def checker_source_root() -> Path:
    """Checkout that holds the checker code actually imported by this process."""
    return Path(__file__).resolve().parents[2]


def _run_text(command, *, cwd: Path, timeout: float = 10.0, env=None) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"command timed out after {timeout:g}s") from exc
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise OSError(f"command failed ({completed.returncode}): {stderr[:512]}")
    return (completed.stdout or "").strip()


def _git_env() -> dict:
    return {key: value for key, value in os.environ.items() if key not in GIT_DISCOVERY_ENV}


def _inspect_entrypoint(git, root: Path) -> dict:
    """Bind the actual collector file bytes to the tracked HEAD blob (read-only)."""
    result = {"path": COLLECTOR_ENTRYPOINT, "tracked": False}
    try:
        listing = git("ls-files", "-s", "--", COLLECTOR_ENTRYPOINT)
        lines = listing.splitlines()
        if len(lines) != 1:
            result["error"] = "entrypoint is not exactly one tracked index entry"
            return result
        meta, _, path = lines[0].partition("\t")
        mode, blob, stage = meta.split()
        result.update(index_mode=mode, index_blob=blob, index_stage=int(stage),
                      tracked=path == COLLECTOR_ENTRYPOINT)
        result["head_blob"] = git("rev-parse", "--verify", f"HEAD:./{COLLECTOR_ENTRYPOINT}")
        # hash-object without -w computes the blob id only; it writes nothing.
        result["worktree_blob"] = git("hash-object", "--", COLLECTOR_ENTRYPOINT)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:256]}"
    return result


def inspect_git(repo_root: Path) -> dict:
    root = Path(repo_root).resolve()
    env = _git_env()

    def git(*args):
        return _run_text(["git", *args], cwd=root, env=env)

    head = git("rev-parse", "--verify", "HEAD^{commit}")
    toplevel = str(Path(git("rev-parse", "--show-toplevel")).resolve())
    status = git("status", "--porcelain=v1", "--untracked-files=normal")
    # Lowercase tags (assume-unchanged) and S (skip-worktree) hide edits from status.
    hidden = [line[2:] for line in git("ls-files", "-v").splitlines()
              if line[:1].islower() or line[:1] == "S"]
    return {
        "head": head,
        "clean": not bool(status),
        "status_lines": [] if not status else status.splitlines()[:100],
        "status_truncated": bool(status and len(status.splitlines()) > 100),
        "execution_root": str(root),
        "toplevel": toplevel,
        "checker_source_root": str(checker_source_root()),
        "entrypoint": _inspect_entrypoint(git, root),
        "hidden_index_flag_count": len(hidden),
        "hidden_index_flag_paths": hidden[:20],
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


def _probe_gap(own_pid, reason: str) -> dict:
    return {
        "probe_ok": False,
        "error": reason,
        **{name: [] for name in PROCESS_LISTS},
        "own_pid_excluded": own_pid,
        "actions_taken": [],
    }


def _row_list(raw: dict, key: str, count_key: str):
    """Return the rows only when an explicit collection and its count agree."""
    if key not in raw or count_key not in raw:
        return None
    rows, count = raw[key], raw[count_key]
    if type(count) is not int or count < 0:
        return None
    if isinstance(rows, dict):  # ConvertTo-Json may unwrap a single element.
        rows = [rows]
    if not isinstance(rows, list) or len(rows) != count:
        return None
    if not all(isinstance(row, dict) for row in rows):
        return None
    return rows


def _command_tail(command_line) -> str | None:
    """Arguments after the first (executable) token of a Windows command line."""
    if not isinstance(command_line, str):
        return None
    text = command_line.strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        if end < 0:
            return None
        rest = text[end + 1:]
    else:
        parts = text.split(None, 1)
        rest = parts[1] if len(parts) == 2 else ""
    rest = rest.strip()
    return rest or None


def _is_own_launcher(item: dict, own_item, *, runtime_exes: set, norm, current_executable: str) -> bool:
    """A uv/venv ``python.exe`` launcher re-runs the same arguments in its child (the checker).

    Only that explicit link excuses the direct parent: its executable is the launcher
    path, both command lines are readable, and their argument tails are identical.
    Missing/empty/inaccessible command lines or an unrelated parent script never qualify.
    """
    executable = item.get("ExecutablePath")
    if not isinstance(executable, str) or norm(executable) != norm(current_executable):
        return False
    if not isinstance(own_item, dict):
        return False
    own_executable = own_item.get("ExecutablePath")
    if not isinstance(own_executable, str) or norm(own_executable) not in runtime_exes:
        return False
    parent_tail = _command_tail(item.get("CommandLine"))
    own_tail = _command_tail(own_item.get("CommandLine"))
    if parent_tail is None or own_tail is None or parent_tail != own_tail:
        return False
    return COLLECTOR_MARKER not in parent_tail.lower()


def classify_process_rows(raw, *, own_pid: int, current_executable: str,
                          base_executable: str | None = None, own_parent_pid: int | None = None) -> dict:
    """Pure classification of process/window rows. Missing evidence is never absence.

    The direct parent is excluded only when it is provably this checker's own venv
    launcher (see ``_is_own_launcher``); otherwise it is classified like any process.
    """
    if not isinstance(raw, dict):
        return _probe_gap(own_pid, "process probe did not return an object")
    py = _row_list(raw, "python_processes", "python_process_count")
    marked = _row_list(raw, "marker_processes", "marker_process_count")
    windows = _row_list(raw, "titled_windows", "titled_window_count")
    if py is None or marked is None or windows is None:
        return _probe_gap(own_pid, "process/window collections missing, null, truncated or malformed")

    def norm(path):
        return os.path.normcase(os.path.normpath(path))

    runtime_exes = {norm(current_executable)}
    if isinstance(base_executable, str) and base_executable:
        runtime_exes.add(norm(base_executable))
    own_launcher = None
    rows = {}
    for item in py + marked:
        pid = item.get("ProcessId")
        if type(pid) is not int or pid <= 0:
            return _probe_gap(own_pid, "process row without a valid ProcessId")
        rows.setdefault(pid, item)

    collector, same_runtime_other, unidentified = [], [], []
    for pid, item in sorted(rows.items()):
        if pid == own_pid:
            continue
        command_line = item.get("CommandLine")
        executable = item.get("ExecutablePath")
        if pid == own_parent_pid and _is_own_launcher(
                item, rows.get(own_pid), runtime_exes=runtime_exes, norm=norm,
                current_executable=current_executable):
            own_launcher = pid
            continue
        name = item.get("Name") if isinstance(item.get("Name"), str) else None
        command_readable = isinstance(command_line, str) and bool(command_line.strip())
        executable_readable = isinstance(executable, str) and bool(executable.strip())
        # The raw CommandLine may contain secrets; only derived booleans are reported.
        if command_readable and COLLECTOR_MARKER in command_line.lower():
            collector.append({
                "pid": pid,
                "name": name,
                "collector_marker_matched": True,
                "executable": executable if executable_readable else None,
            })
            continue
        if not command_readable or not executable_readable:
            unidentified.append({
                "pid": pid,
                "name": name,
                "command_line_readable": command_readable,
                "executable_readable": executable_readable,
            })
            continue
        if norm(executable) in runtime_exes:
            same_runtime_other.append({
                "pid": pid,
                "name": name,
                "command_line_present": True,
                "executable": executable,
            })

    runtime_windows = []
    for item in windows:
        title = item.get("MainWindowTitle")
        if not isinstance(title, str) or type(item.get("Id")) is not int:
            return _probe_gap(own_pid, "window row without a readable title/Id")
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
        "unidentified_python_processes": unidentified,
        "own_pid_excluded": own_pid,
        "own_launcher_pid_excluded": own_launcher,
        "actions_taken": [],
    }


def inspect_processes(repo_root: Path, *, own_pid: int | None = None) -> dict:
    """Read process/window metadata only. No handle mutation, kill, or window action."""
    root = Path(repo_root).resolve()
    own_pid = os.getpid() if own_pid is None else own_pid
    # This probe's own powershell.exe command line contains the marker text, so $PID is
    # excluded from the marker query. Any other process naming the collector is BLOCKED,
    # including non-collector tools (conservative).
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$py = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%.exe' OR Name='py.exe' OR Name='pyw.exe'" |
  Select-Object ProcessId,Name,CommandLine,ExecutablePath)
$marked = @(Get-CimInstance Win32_Process -Filter "CommandLine LIKE '%kiwoom_universe_logger%'" |
  Where-Object { $_.ProcessId -ne $PID } |
  Select-Object ProcessId,Name,CommandLine,ExecutablePath)
$windows = @(Get-Process | Where-Object { $_.MainWindowTitle } |
  Select-Object Id,ProcessName,MainWindowTitle)
[pscustomobject]@{
  python_processes=$py; python_process_count=$py.Count;
  marker_processes=$marked; marker_process_count=$marked.Count;
  titled_windows=$windows; titled_window_count=$windows.Count
} | ConvertTo-Json -Depth 5 -Compress
"""
    try:
        raw = _powershell_json(script, cwd=root)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return _probe_gap(own_pid, f"{type(exc).__name__}: {str(exc)[:256]}")
    return classify_process_rows(
        raw, own_pid=own_pid, current_executable=sys.executable,
        base_executable=getattr(sys, "_base_executable", None), own_parent_pid=os.getppid())


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
    return {**EXPECTED_CLI_CONTRACT, "valid": error is None, "error": error}


def _parse_market_date(value) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def parse_kst(value, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise AdmissionContractError(f"invalid {name}") from exc
    if parsed.utcoffset() != timedelta(hours=9):
        raise AdmissionContractError(f"{name} must use explicit UTC+09:00")
    return parsed


def require_kst(value, name: str) -> datetime:
    if not isinstance(value, _DATETIME_TYPE) or value.utcoffset() != timedelta(hours=9):
        raise ValueError(f"{name} must use explicit UTC+09:00")
    return value


def in_target_window(moment: datetime) -> bool:
    return TARGET_START <= moment.time().replace(tzinfo=None) < TARGET_END


def same_path(left, right) -> bool:
    """Pure comparison of recorded absolute paths (no filesystem access)."""
    if not isinstance(left, str) or not isinstance(right, str) or not left or not right:
        return False
    if not os.path.isabs(left) or not os.path.isabs(right):
        return False
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def _is_bool(value, expected: bool) -> bool:
    return type(value) is bool and value is expected


def _is_int(value, expected=None) -> bool:
    return type(value) is int and (expected is None or value == expected)


def _is_sha1(value) -> bool:
    return isinstance(value, str) and _SHA1.fullmatch(value) is not None


def _market_attestation(inputs: dict) -> dict:
    return {
        "date": inputs.get("official_market_date"),
        "source_note": inputs.get("official_market_source_note"),
        "external_attestation_only": True,
        "target_window_kst": TARGET_WINDOW_TEXT,
    }


def _assess(inputs: dict, checks: dict, *, started: datetime, completed: datetime,
            elapsed_monotonic) -> tuple[list[dict], list[dict]]:
    """Strict pure evaluation shared by fresh admission and every re-validation."""
    blockers: list[dict] = []
    uncertain: list[dict] = []

    def block(check_id, reason):
        blockers.append({"id": check_id, "reason": str(reason)[:256]})

    def doubt(check_id, reason):
        uncertain.append({"id": check_id, "reason": str(reason)[:256]})

    repo_root = inputs.get("repo_root")
    expected = inputs.get("expected_revision")
    if not isinstance(repo_root, str) or not os.path.isabs(repo_root):
        block("execution_root", "absolute repo_root required")
    if not _is_sha1(expected):
        block("revision", "exact 40-hex expected revision required")

    git = checks.get("git")
    if not isinstance(git, dict):
        block("git", "git evidence missing_or_not_object")
        git = {}
    if git.get("head") != expected or not _is_sha1(git.get("head")):
        block("revision", f"HEAD={git.get('head')}")
    if not _is_bool(git.get("clean"), True) or git.get("status_lines") != [] \
            or not _is_bool(git.get("status_truncated"), False):
        block("working_tree", "tracked/untracked changes present or status evidence inconsistent")
    if not same_path(git.get("toplevel"), repo_root):
        block("execution_root", "repo_root is not the Git --show-toplevel of the inspected checkout")
    if not same_path(git.get("checker_source_root"), repo_root):
        block("checker_source", "checker code was imported from a different checkout than repo_root")
    entry = git.get("entrypoint")
    if not isinstance(entry, dict) or entry.get("path") != COLLECTOR_ENTRYPOINT \
            or not _is_bool(entry.get("tracked"), True) or not _is_int(entry.get("index_stage"), 0) \
            or not _is_sha1(entry.get("head_blob")) \
            or entry.get("index_blob") != entry.get("head_blob") \
            or entry.get("worktree_blob") != entry.get("head_blob") or entry.get("error"):
        block("collector_entrypoint", "entrypoint bytes are not the tracked HEAD blob of the approved revision")
    if not _is_int(git.get("hidden_index_flag_count"), 0):
        block("index_flags", f"assume-unchanged/skip-worktree entries={git.get('hidden_index_flag_count')!r}")

    preflight = checks.get("preflight")
    if not isinstance(preflight, dict):
        block("preflight", "preflight evidence missing_or_not_object")
        preflight = {}
    python_bits = preflight.get("python_bits")
    if type(python_bits) is not int or python_bits != 32:
        block("preflight", f"python_bits={python_bits!r}, expected=32")
    for key, expected_value in (("login_attempted", False), ("ocx_instantiated", False), ("ready", True),
                                ("ocx_registered", True), ("ocx_file_exists", True)):
        if not _is_bool(preflight.get(key), expected_value):
            block("preflight", f"{key}={preflight.get(key)!r}, expected={expected_value!r}")
    executable = preflight.get("executable")
    if not isinstance(executable, str) or not os.path.isabs(executable):
        block("preflight", "absolute preflight executable required")
    if preflight.get("error") not in (None, ""):
        block("preflight", "preflight reported an error")

    disk = checks.get("disk")
    if not isinstance(disk, dict):
        disk = {}
    free = disk.get("free_bytes")
    free_ok = _is_int(free) and free >= MIN_FREE_BYTES
    if not free_ok or not _is_bool(disk.get("meets_code_minimum"), True) \
            or disk.get("minimum_code_admission_bytes", MIN_FREE_BYTES) != MIN_FREE_BYTES:
        block("disk", f"free_bytes={free!r}, flag={disk.get('meets_code_minimum')!r}")

    processes = checks.get("processes")
    if not isinstance(processes, dict):
        doubt("process_probe", "process evidence missing_or_not_object")
    elif not _is_bool(processes.get("probe_ok"), True) or processes.get("error") not in (None, ""):
        doubt("process_probe", processes.get("error"))
    elif not all(isinstance(processes.get(name), list) for name in PROCESS_LISTS):
        doubt("process_probe", "process evidence lists missing_or_malformed")
    else:
        if processes.get("collector_processes"):
            block("collector_process", "another collector process observed")
        if processes.get("runtime_or_openapi_windows"):
            block("runtime_window", "Runtime/OpenAPI/Kiwoom titled window observed")
        if processes.get("same_python_runtime_other_processes"):
            doubt("same_python_runtime", "another process uses the same 32-bit Python executable")
        if processes.get("unidentified_python_processes"):
            doubt("unidentified_python", "Python process whose command line/executable could not be read")
    if isinstance(processes, dict) and processes.get("actions_taken", []) != []:
        block("process_probe_actions", "process probe must not take actions")

    lease = checks.get("lease")
    if not isinstance(lease, dict) or not _is_bool(lease.get("confirmed_free"), True) \
            or lease.get("state") not in ("absent", "free") or not _is_bool(lease.get("file_created"), False):
        block("collector_lease", str(lease.get("state") if isinstance(lease, dict) else lease))

    cli_contract = checks.get("cli_contract")
    if not isinstance(cli_contract, dict) or any(
            cli_contract.get(key) != value or type(cli_contract.get(key)) is not type(value)
            for key, value in EXPECTED_CLI_CONTRACT.items()):
        reason = cli_contract.get("error") if isinstance(cli_contract, dict) else None
        block("cli_contract", reason or "exact diagnostic CLI contract mismatch")

    market_date = _parse_market_date(inputs.get("official_market_date"))
    if market_date is None:
        block("market_date", "valid official market date attestation required")
    elif market_date != started.date() or market_date != completed.date():
        block("market_date", f"attested={market_date.isoformat()}, "
                             f"observed={started.date().isoformat()}..{completed.date().isoformat()}")
    source_note = inputs.get("official_market_source_note")
    if not isinstance(source_note, str) or not source_note.strip():
        block("market_source", "official market source note required")

    for label, moment in (("start", started), ("completion", completed)):
        if not in_target_window(moment):
            block("target_window", f"{label}_kst={moment.isoformat()}, required={TARGET_WINDOW_TEXT}")

    if not _is_bool(inputs.get("execution_approved"), True):
        block("execution_approval", "explicit user approval required")

    wall = (completed - started).total_seconds()
    if wall < 0:
        doubt("clock_regression", f"wall clock moved backwards by {-wall:.3f}s during observation")
    if type(elapsed_monotonic) is not float or not math.isfinite(elapsed_monotonic) \
            or elapsed_monotonic < 0:
        doubt("clock_evidence", f"monotonic elapsed={elapsed_monotonic!r}")
    else:
        if abs(wall - elapsed_monotonic) > CLOCK_TOLERANCE_SECONDS:
            doubt("clock_consistency", f"wall={wall:.3f}s, monotonic={elapsed_monotonic:.3f}s")
        if elapsed_monotonic >= MAX_ADMISSION_AGE_SECONDS:
            block("observation_duration", f"observation took {elapsed_monotonic:.3f}s")
    return blockers, uncertain


def _report(inputs: dict, checks: dict, *, started, completed, elapsed, blockers, uncertain) -> dict:
    status = RUN_BLOCKED if blockers else RUN_UNCERTAIN if uncertain else RUN_READY
    return {
        "schema": ADMISSION_SCHEMA,
        "status": status,
        "observation_started_at_kst": started.isoformat(),
        "observed_at_kst": completed.isoformat(),
        "observation_elapsed_monotonic_seconds": elapsed,
        "inputs": inputs,
        "checks": {**checks, "market_attestation": _market_attestation(inputs)},
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
            "RUN_READY means only the pre-run admission contract held from observation start to "
            "completion. It does not certify server availability, feed delivery, native stability, "
            "capture success, or that nothing changes before a human runs a command."
        ),
    }


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
    observation_started_kst: datetime | None = None,
    elapsed_monotonic_seconds: float | None = None,
) -> dict:
    """Evaluate one observation. ``now_kst`` is the completion instant."""
    completed = require_kst(now_kst, "now_kst")
    started = completed if observation_started_kst is None else require_kst(
        observation_started_kst, "observation_started_kst")
    elapsed = 0.0 if elapsed_monotonic_seconds is None else elapsed_monotonic_seconds
    values = asdict(inputs)
    checks = {"git": git, "preflight": preflight, "disk": disk, "processes": processes,
              "lease": lease, "cli_contract": cli_contract}
    blockers, uncertain = _assess(values, checks, started=started, completed=completed,
                                  elapsed_monotonic=elapsed)
    return _report(values, checks, started=started, completed=completed, elapsed=elapsed,
                   blockers=blockers, uncertain=uncertain)


def require_ready_admission(admission, *, expected_revision: str, now_kst: datetime) -> dict:
    """Re-derive READY from a recorded admission; raise unless it holds at ``now_kst``.

    Checks, in order: exact shape, the same strict evaluator over the recorded evidence,
    label consistency, trusted revision, and freshness ``0 <= now - start < 60s`` with
    ``completion <= now`` and ``now`` inside the target window.
    """
    now = require_kst(now_kst, "now_kst")
    if not isinstance(admission, dict) or admission.get("schema") != ADMISSION_SCHEMA:
        raise AdmissionContractError(f"{ADMISSION_SCHEMA} required")
    missing = ADMISSION_KEYS - set(admission)
    if missing:
        raise AdmissionContractError(f"admission fields missing: {sorted(missing)}")
    if admission.get("status") != RUN_READY:
        raise AdmissionContractError("RUN_READY admission required")
    if admission.get("blockers") != [] or admission.get("uncertain") != []:
        raise AdmissionContractError("RUN_READY admission must have no blockers or uncertainty")
    inputs = admission.get("inputs")
    checks = admission.get("checks")
    if not isinstance(inputs, dict) or not isinstance(checks, dict):
        raise AdmissionContractError("complete admission inputs/checks required")
    try:
        AdmissionInputs(**inputs)
    except TypeError as exc:
        raise AdmissionContractError("admission inputs do not match the schema") from exc
    if set(checks) != CHECK_KEYS:
        raise AdmissionContractError("admission checks do not match the schema")
    if checks.get("market_attestation") != _market_attestation(inputs):
        raise AdmissionContractError("market attestation does not match admission inputs")
    started = parse_kst(admission.get("observation_started_at_kst"), "admission observation start")
    completed = parse_kst(admission.get("observed_at_kst"), "admission observed_at_kst")
    blockers, uncertain = _assess(inputs, checks, started=started, completed=completed,
                                  elapsed_monotonic=admission.get("observation_elapsed_monotonic_seconds"))
    if blockers or uncertain:
        ids = sorted({item["id"] for item in blockers + uncertain})
        raise AdmissionContractError(f"recorded evidence contradicts RUN_READY: {ids}")
    if not _is_sha1(expected_revision) or inputs.get("expected_revision") != expected_revision:
        raise AdmissionContractError("admission expected revision mismatch")
    if completed > now:
        raise AdmissionContractError("admission observation is from the future")
    age = (now - started).total_seconds()
    if age < 0:
        raise AdmissionContractError("admission observation is from the future")
    if age >= MAX_ADMISSION_AGE_SECONDS:
        raise AdmissionContractError(
            f"admission observation is stale ({age:.3f}s >= {MAX_ADMISSION_AGE_SECONDS}s)")
    if not in_target_window(now):
        raise AdmissionContractError(f"{now.isoformat()} is outside {TARGET_WINDOW_TEXT}")
    return {"observation_started": started, "observation_completed": completed, "age_seconds": age}


def collect_and_evaluate(
    inputs: AdmissionInputs,
    *,
    now_kst: datetime | None = None,
    process_probe=inspect_processes,
    monotonic=time.monotonic,
) -> dict:
    """Observe and evaluate. Start and completion instants are both evaluated.

    ``now_kst`` only pins the START for deterministic tests; the completion is then
    start + elapsed monotonic time, so a fixed start cannot hide a slow observation.
    """
    root = Path(inputs.repo_root).resolve()
    mono_start = monotonic()
    started = datetime.now(KST) if now_kst is None else require_kst(now_kst, "now_kst")
    git = inspect_git(root)
    preflight = inspect_environment()
    disk = inspect_disk(root)
    try:
        processes = process_probe(root)
    except Exception as exc:
        processes = _probe_gap(None, f"{type(exc).__name__}: {str(exc)[:256]}")
    lease = inspect_existing_lease(root)
    cli = exact_diagnostic_cli_contract()
    elapsed = monotonic() - mono_start
    if now_kst is None:
        completed = datetime.now(KST)
    else:
        completed = started + timedelta(seconds=max(elapsed, 0.0))
    return evaluate_admission(
        inputs,
        now_kst=completed,
        git=git,
        preflight=preflight,
        disk=disk,
        processes=processes,
        lease=lease,
        cli_contract=cli,
        observation_started_kst=started,
        elapsed_monotonic_seconds=elapsed,
    )
