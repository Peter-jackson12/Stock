"""Create and verify a short-lived manual run plan for FID A-B-A.

The plan binds one RUN_READY admission observation to an exact revision and exact
collector command. It never launches the collector. Verification reruns the
read-only admission checks before revealing the manual command.

Time contract: an instant ``t`` is valid for a plan when ``created <= t < expires``.
Verification evaluates its START and its COMPLETION (after the fresh admission and
command recomputation). Passing verification says nothing about the interval between
completion and a human actually running the command.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from collector.kiwoom.fid_read_ab_admission import (
    CLOCK_TOLERANCE_SECONDS,
    COLLECTOR_ARGS,
    COLLECTOR_ENTRYPOINT,
    MAX_ADMISSION_AGE_SECONDS,
    AdmissionContractError,
    AdmissionInputs,
    KST,
    RUN_READY,
    TARGET_WINDOW_TEXT,
    collect_and_evaluate,
    in_target_window,
    parse_kst,
    require_kst,
    require_ready_admission,
)

PLAN_SCHEMA = "fid_read_ab_run_plan_v1"
VERIFICATION_SCHEMA = "fid_read_ab_run_plan_verification_v1"
MAX_PLAN_BYTES = 1024 * 1024
PLAN_TTL_SECONDS = 300
EXECUTION_CONTRACT = {
    "automatic_execution": False,
    "requires_manual_review": True,
    "requires_fresh_verification": True,
}
PLAN_KEYS = {
    "schema", "created_at_kst", "expires_at_kst", "ttl_seconds", "expected_revision",
    "working_directory", "admission_sha256", "admission", "manual_command", "execution",
    "forbidden_automatic_actions", "note",
}
_SHA256 = re.compile(r"[0-9a-f]{64}")
# PowerShell treats these as single quotes too; each is escaped by doubling.
_PS_SINGLE_QUOTES = "'\u2018\u2019\u201a\u201b"


class FidReadRunPlanError(ValueError):
    pass


def _canonical_bytes(value: dict) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise FidReadRunPlanError(f"non-canonical plan payload: {type(exc).__name__}") from exc
    return text.encode("utf-8")


def admission_sha256(admission: dict) -> str:
    return hashlib.sha256(_canonical_bytes(admission)).hexdigest()


def _parse_kst(value, name: str) -> datetime:
    try:
        return parse_kst(value, name)
    except AdmissionContractError as exc:
        raise FidReadRunPlanError(str(exc)) from exc


def _require_ready(admission, *, expected_revision: str, now_kst: datetime, context: str) -> dict:
    try:
        return require_ready_admission(admission, expected_revision=expected_revision, now_kst=now_kst)
    except AdmissionContractError as exc:
        raise FidReadRunPlanError(f"{context}: {exc}") from exc


def _manual_command_from_admission(admission: dict) -> tuple[str, list[str]]:
    inputs = admission.get("inputs")
    checks = admission.get("checks")
    if not isinstance(inputs, dict) or not isinstance(checks, dict):
        raise FidReadRunPlanError("complete admission inputs/checks required")
    preflight = checks.get("preflight")
    if not isinstance(preflight, dict):
        raise FidReadRunPlanError("preflight evidence required")
    python_executable = preflight.get("executable")
    if not isinstance(python_executable, str) or not os.path.isabs(python_executable):
        raise FidReadRunPlanError("absolute preflight executable missing")
    repo_root = inputs.get("repo_root")
    if not isinstance(repo_root, str) or not os.path.isabs(repo_root):
        raise FidReadRunPlanError("absolute repo_root required")
    root = Path(repo_root).resolve()
    collector = root.joinpath(*COLLECTOR_ENTRYPOINT.split("/"))
    if not collector.is_file():
        raise FidReadRunPlanError(f"collector entrypoint missing: {collector}")
    return str(root), [python_executable, str(collector), *COLLECTOR_ARGS]


def build_run_plan(admission: dict, *, expected_revision: str, now_kst: datetime) -> dict:
    try:
        created = require_kst(now_kst, "now_kst")
    except ValueError as exc:
        raise FidReadRunPlanError(str(exc)) from exc
    _require_ready(admission, expected_revision=expected_revision, now_kst=created,
                   context="admission is not valid for a new plan")
    embedded = deepcopy(admission)  # later caller mutation cannot alter the plan.
    working_directory, command = _manual_command_from_admission(embedded)
    expires = created + timedelta(seconds=PLAN_TTL_SECONDS)
    return {
        "schema": PLAN_SCHEMA,
        "created_at_kst": created.isoformat(),
        "expires_at_kst": expires.isoformat(),
        "ttl_seconds": PLAN_TTL_SECONDS,
        "expected_revision": expected_revision,
        "working_directory": working_directory,
        "admission_sha256": admission_sha256(embedded),
        "admission": embedded,
        "manual_command": command,
        "execution": dict(EXECUTION_CONTRACT),
        "forbidden_automatic_actions": [
            "collector launch",
            "OCX login",
            "SetRealReg",
            "process kill/restart/relogin",
            "Runtime/OpenAPI window close",
            "lock deletion",
            "raw database scan/count/hash",
        ],
        "note": (
            "This plan is short-lived evidence binding only. It does not launch the collector "
            "and does not certify server availability, feed delivery, native stability, or capture success. "
            "The admission SHA-256 is an integrity check, not a signature or user authentication."
        ),
    }


def write_new_plan(path: Path, plan: dict) -> Path:
    target = Path(path).resolve()
    encoded = (json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_PLAN_BYTES:
        raise FidReadRunPlanError("run plan exceeds bounded size")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise FidReadRunPlanError(f"run plan already exists: {target}") from exc
    return target


def _reject_constant(token):
    raise ValueError(f"non-finite JSON constant {token}")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def _command_key(working_directory, command):
    """Compare path tokens case-insensitively (Windows) and the arguments exactly."""
    if not isinstance(working_directory, str) or not isinstance(command, list) or len(command) < 2 \
            or not all(isinstance(item, str) for item in command):
        return None

    def norm(path):
        return os.path.normcase(os.path.normpath(path))
    return norm(working_directory), norm(command[0]), norm(command[1]), tuple(command[2:])


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def read_run_plan(path: Path) -> dict:
    """Read at most MAX_PLAN_BYTES + 1 bytes; the earlier stat is only a fast path.

    The byte bound limits memory, not the time the OS may take to serve the read.
    """
    target = Path(path).resolve()
    if not target.is_file():
        raise FidReadRunPlanError(f"run plan not found: {target}")
    size = target.stat().st_size
    if size > MAX_PLAN_BYTES:
        raise FidReadRunPlanError(f"run plan exceeds bounded size ({size} > {MAX_PLAN_BYTES})")
    try:
        with target.open("rb") as stream:
            data = stream.read(MAX_PLAN_BYTES + 1)
    except OSError as exc:
        raise FidReadRunPlanError(f"run plan unreadable: {type(exc).__name__}") from exc
    if len(data) > MAX_PLAN_BYTES:
        raise FidReadRunPlanError(f"run plan exceeds bounded size (>{MAX_PLAN_BYTES} bytes read)")
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_reject_constant,
                           parse_float=_finite_float, object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError) as exc:
        raise FidReadRunPlanError(f"invalid run plan JSON: {type(exc).__name__}") from exc
    if not isinstance(value, dict) or value.get("schema") != PLAN_SCHEMA:
        raise FidReadRunPlanError(f"{PLAN_SCHEMA} required")
    return value


def validate_plan_integrity(plan: dict, *, now_kst: datetime) -> None:
    """Check the plan at ``now_kst`` and that its admission was valid when it was created."""
    try:
        require_kst(now_kst, "now_kst")
    except ValueError as exc:
        raise FidReadRunPlanError(str(exc)) from exc
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise FidReadRunPlanError(f"{PLAN_SCHEMA} required")
    if set(plan) != PLAN_KEYS:
        raise FidReadRunPlanError("run plan fields do not match the schema")
    admission = plan.get("admission")
    if not isinstance(admission, dict):
        raise FidReadRunPlanError("embedded admission required")
    digest = plan.get("admission_sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or digest != admission_sha256(admission):
        raise FidReadRunPlanError("embedded admission digest mismatch")
    expected = plan.get("expected_revision")
    if not isinstance(expected, str) or not expected:
        raise FidReadRunPlanError("expected revision required")
    inputs = admission.get("inputs")
    if not isinstance(inputs, dict) or inputs.get("expected_revision") != expected:
        raise FidReadRunPlanError("plan/admission revision mismatch")
    if type(plan.get("ttl_seconds")) is not int or plan.get("ttl_seconds") != PLAN_TTL_SECONDS:
        raise FidReadRunPlanError("unexpected run-plan TTL")
    created = _parse_kst(plan.get("created_at_kst"), "plan created_at_kst")
    expires = _parse_kst(plan.get("expires_at_kst"), "plan expires_at_kst")
    if expires - created != timedelta(seconds=PLAN_TTL_SECONDS):
        raise FidReadRunPlanError("run-plan expiration contract mismatch")
    if now_kst < created:
        raise FidReadRunPlanError("run plan is from the future")
    if now_kst >= expires:
        raise FidReadRunPlanError("run plan expired (valid for created <= t < expires)")
    if plan.get("execution") != EXECUTION_CONTRACT:
        raise FidReadRunPlanError("automatic execution must remain disabled with fresh verification")
    # A plan re-timestamped later cannot reuse an admission that was stale at that time.
    _require_ready(admission, expected_revision=expected, now_kst=created,
                   context="embedded admission was not valid at plan creation")
    working_directory, command = _manual_command_from_admission(admission)
    stored = _command_key(plan.get("working_directory"), plan.get("manual_command"))
    expected_key = _command_key(working_directory, command)
    if stored is None or stored[0] != expected_key[0]:
        raise FidReadRunPlanError("working directory changed since plan creation")
    if stored != expected_key:
        raise FidReadRunPlanError("manual command changed or no longer matches the embedded admission")


def powershell_command(command: list[str]) -> str:
    """Display-only PowerShell invocation: call operator plus single-quoted tokens."""
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise FidReadRunPlanError("nonempty string command list required")
    for item in command:
        if '"' in item or any(ord(ch) < 32 or ord(ch) == 127 for ch in item):
            raise FidReadRunPlanError("command token contains a double quote or control character")

    def quote(value: str) -> str:
        escaped = "".join(ch * 2 if ch in _PS_SINGLE_QUOTES else ch for ch in value)
        return "'" + escaped + "'"

    return "& " + " ".join(quote(item) for item in command)


def _not_ready(reason: str, *, fresh=None, **extra) -> dict:
    return {
        "schema": VERIFICATION_SCHEMA,
        "status": "NOT_READY",
        "fresh_admission": fresh,
        "manual_command": None,
        "automatic_execution": False,
        "note": reason,
        **extra,
    }


def _clock_reader(now_kst, monotonic):
    """Return read() -> (wall instant, monotonic elapsed since start).

    ``now_kst`` pins only the start (tests); later instants advance with the
    monotonic clock, so a fixed start cannot disable the completion checks.
    """
    mono_start = monotonic()
    if now_kst is None:
        def read():
            return datetime.now(KST), monotonic() - mono_start
    else:
        start = require_kst(now_kst, "now_kst")

        def read():
            elapsed = monotonic() - mono_start
            return start + timedelta(seconds=max(elapsed, 0.0)), elapsed
    return read


def verify_plan_for_manual_command(
    plan: dict,
    *,
    trusted_expected_revision: str,
    execution_approved_now: bool,
    now_kst: datetime | None = None,
    admission_runner=collect_and_evaluate,
    monotonic=time.monotonic,
) -> dict:
    read_clock = _clock_reader(now_kst, monotonic)
    started, _ = read_clock()
    validate_plan_integrity(plan, now_kst=started)
    if not isinstance(trusted_expected_revision, str) or not trusted_expected_revision:
        raise FidReadRunPlanError("trusted expected revision required")
    if plan.get("expected_revision") != trusted_expected_revision:
        raise FidReadRunPlanError("plan revision does not match trusted expected revision")
    if execution_approved_now is not True:
        return _not_ready("Fresh explicit execution approval is required before revealing the command.")

    embedded = plan["admission"]
    try:
        inputs = AdmissionInputs(**embedded["inputs"])
    except (KeyError, TypeError) as exc:
        raise FidReadRunPlanError("invalid embedded admission inputs") from exc

    fresh = admission_runner(inputs)
    if not isinstance(fresh, dict) or fresh.get("status") != RUN_READY:
        return _not_ready("Collector command is withheld because fresh admission is not RUN_READY.",
                          fresh=fresh)
    if fresh.get("inputs") != embedded["inputs"]:
        return _not_ready("Collector command is withheld because fresh admission inputs changed.",
                          fresh=fresh)
    try:
        fresh_working_directory, fresh_command = _manual_command_from_admission(fresh)
    except FidReadRunPlanError as exc:
        return _not_ready(f"Collector command is withheld: {exc}", fresh=fresh)
    stored = _command_key(plan.get("working_directory"), plan.get("manual_command"))
    fresh_key = _command_key(fresh_working_directory, fresh_command)
    if stored is None or stored[0] != fresh_key[0]:
        raise FidReadRunPlanError("working directory changed since plan creation")
    if stored != fresh_key:
        raise FidReadRunPlanError("manual command changed or no longer matches fresh admission")

    # Completion instant: every time-dependent condition is re-evaluated here.
    completed, elapsed = read_clock()
    wall = (completed - started).total_seconds()
    if wall < 0 or not math.isfinite(float(elapsed)) or elapsed < 0 \
            or abs(wall - elapsed) > CLOCK_TOLERANCE_SECONDS:
        return _not_ready(f"Clock inconsistency during verification (wall={wall:.3f}s, "
                          f"monotonic={elapsed:.3f}s); command withheld.", fresh=fresh)
    validate_plan_integrity(plan, now_kst=completed)
    expires = _parse_kst(plan["expires_at_kst"], "plan expires_at_kst")
    if started + timedelta(seconds=elapsed) >= expires:
        raise FidReadRunPlanError("run plan expired by monotonic elapsed time during verification")
    if not in_target_window(completed):
        return _not_ready(f"Verification completed outside {TARGET_WINDOW_TEXT}.", fresh=fresh)
    created = _parse_kst(plan["created_at_kst"], "plan created_at_kst")
    try:
        freshness = require_ready_admission(fresh, expected_revision=trusted_expected_revision,
                                            now_kst=completed)
    except AdmissionContractError as exc:
        return _not_ready(f"Fresh admission contract failed: {exc}", fresh=fresh)
    if freshness["observation_started"] < created:
        return _not_ready("Fresh admission started before the plan was created.", fresh=fresh)

    return {
        "schema": VERIFICATION_SCHEMA,
        "status": "MANUAL_COMMAND_READY",
        "verification_started_at_kst": started.isoformat(),
        "verified_at_kst": completed.isoformat(),
        "verification_elapsed_monotonic_seconds": elapsed,
        "plan_expires_at_kst": plan["expires_at_kst"],
        "plan_admission_sha256": plan["admission_sha256"],
        "fresh_admission": fresh,
        "working_directory": plan["working_directory"],
        "manual_command": fresh_command,
        "manual_command_powershell": powershell_command(fresh_command),
        "automatic_execution": False,
        "note": (
            "The command is displayed for explicit manual execution only. Verification does not "
            "launch the collector, and conditions may change after verified_at_kst."
        ),
    }
