"""Create and verify a short-lived manual run plan for FID A-B-A.

The plan binds one RUN_READY admission observation to an exact revision and exact
collector command. It never launches the collector. Verification reruns the
read-only admission checks before revealing the manual command.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path

from collector.kiwoom.fid_read_ab_admission import (
    AdmissionInputs,
    KST,
    RUN_READY,
    collect_and_evaluate,
)

PLAN_SCHEMA = "fid_read_ab_run_plan_v1"
MAX_PLAN_BYTES = 1024 * 1024
MAX_ADMISSION_AGE_SECONDS = 60
PLAN_TTL_SECONDS = 300


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


def _parse_kst(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise FidReadRunPlanError(f"invalid {name}") from exc
    if parsed.utcoffset() != timedelta(hours=9):
        raise FidReadRunPlanError(f"{name} must use explicit UTC+09:00")
    return parsed


def _require_ready_admission(admission: dict, *, expected_revision: str, now_kst: datetime) -> None:
    if not isinstance(admission, dict) or admission.get("schema") != "fid_read_ab_admission_v1":
        raise FidReadRunPlanError("fid_read_ab_admission_v1 required")
    if admission.get("status") != RUN_READY:
        raise FidReadRunPlanError("RUN_READY admission required")
    if admission.get("blockers") != [] or admission.get("uncertain") != []:
        raise FidReadRunPlanError("RUN_READY admission must have no blockers or uncertainty")
    inputs = admission.get("inputs")
    checks = admission.get("checks")
    if not isinstance(inputs, dict) or not isinstance(checks, dict):
        raise FidReadRunPlanError("complete admission inputs/checks required")
    if inputs.get("expected_revision") != expected_revision:
        raise FidReadRunPlanError("admission expected revision mismatch")
    if inputs.get("execution_approved") is not True:
        raise FidReadRunPlanError("explicit execution approval missing from admission")
    git = checks.get("git")
    preflight = checks.get("preflight")
    if not isinstance(git, dict) or git.get("head") != expected_revision or git.get("clean") is not True:
        raise FidReadRunPlanError("admission git identity is not exact and clean")
    if not isinstance(preflight, dict) or preflight.get("executable") in (None, ""):
        raise FidReadRunPlanError("preflight executable missing")

    observed = _parse_kst(admission.get("observed_at_kst"), "admission observed_at_kst")
    age = (now_kst - observed).total_seconds()
    if age < 0:
        raise FidReadRunPlanError("admission observation is from the future")
    if age > MAX_ADMISSION_AGE_SECONDS:
        raise FidReadRunPlanError(
            f"admission observation is stale ({age:.3f}s > {MAX_ADMISSION_AGE_SECONDS}s)"
        )


def build_run_plan(admission: dict, *, expected_revision: str, now_kst: datetime) -> dict:
    if now_kst.utcoffset() != timedelta(hours=9):
        raise FidReadRunPlanError("now_kst must use explicit UTC+09:00")
    _require_ready_admission(admission, expected_revision=expected_revision, now_kst=now_kst)

    inputs = admission["inputs"]
    preflight = admission["checks"]["preflight"]
    repo_root = Path(inputs["repo_root"]).resolve()
    collector = repo_root / "collector" / "kiwoom" / "kiwoom_universe_logger.py"
    if not collector.is_file():
        raise FidReadRunPlanError(f"collector entrypoint missing: {collector}")

    python_executable = str(preflight["executable"])
    command = [
        python_executable,
        str(collector),
        "--storage", "raw-v2",
        "--capture-telemetry",
        "--fid-read-ab-test",
        "--duration-seconds", "90",
    ]
    created = now_kst
    expires = created + timedelta(seconds=PLAN_TTL_SECONDS)
    return {
        "schema": PLAN_SCHEMA,
        "created_at_kst": created.isoformat(),
        "expires_at_kst": expires.isoformat(),
        "ttl_seconds": PLAN_TTL_SECONDS,
        "expected_revision": expected_revision,
        "working_directory": str(repo_root),
        "admission_sha256": admission_sha256(admission),
        "admission": admission,
        "manual_command": command,
        "execution": {
            "automatic_execution": False,
            "requires_manual_review": True,
            "requires_fresh_verification": True,
        },
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
            "and does not certify server availability, feed delivery, native stability, or capture success."
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


def read_run_plan(path: Path) -> dict:
    target = Path(path).resolve()
    if not target.is_file():
        raise FidReadRunPlanError(f"run plan not found: {target}")
    size = target.stat().st_size
    if size > MAX_PLAN_BYTES:
        raise FidReadRunPlanError(f"run plan exceeds bounded size ({size} > {MAX_PLAN_BYTES})")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FidReadRunPlanError(f"invalid run plan JSON: {type(exc).__name__}") from exc
    if not isinstance(value, dict) or value.get("schema") != PLAN_SCHEMA:
        raise FidReadRunPlanError(f"{PLAN_SCHEMA} required")
    return value


def validate_plan_integrity(plan: dict, *, now_kst: datetime) -> None:
    if now_kst.utcoffset() != timedelta(hours=9):
        raise FidReadRunPlanError("now_kst must use explicit UTC+09:00")
    if plan.get("schema") != PLAN_SCHEMA:
        raise FidReadRunPlanError(f"{PLAN_SCHEMA} required")
    admission = plan.get("admission")
    if not isinstance(admission, dict):
        raise FidReadRunPlanError("embedded admission required")
    if plan.get("admission_sha256") != admission_sha256(admission):
        raise FidReadRunPlanError("embedded admission digest mismatch")
    expected = plan.get("expected_revision")
    if not isinstance(expected, str) or not expected:
        raise FidReadRunPlanError("expected revision required")
    if admission.get("inputs", {}).get("expected_revision") != expected:
        raise FidReadRunPlanError("plan/admission revision mismatch")
    if plan.get("ttl_seconds") != PLAN_TTL_SECONDS:
        raise FidReadRunPlanError("unexpected run-plan TTL")
    created = _parse_kst(plan.get("created_at_kst"), "plan created_at_kst")
    expires = _parse_kst(plan.get("expires_at_kst"), "plan expires_at_kst")
    if expires - created != timedelta(seconds=PLAN_TTL_SECONDS):
        raise FidReadRunPlanError("run-plan expiration contract mismatch")
    if now_kst < created:
        raise FidReadRunPlanError("run plan is from the future")
    if now_kst > expires:
        raise FidReadRunPlanError("run plan expired")
    execution = plan.get("execution")
    if not isinstance(execution, dict) or execution.get("automatic_execution") is not False:
        raise FidReadRunPlanError("automatic execution must remain disabled")
    if execution.get("requires_fresh_verification") is not True:
        raise FidReadRunPlanError("fresh verification contract required")


def verify_plan_for_manual_command(
    plan: dict,
    *,
    now_kst: datetime | None = None,
    admission_runner=collect_and_evaluate,
) -> dict:
    observed = datetime.now(KST) if now_kst is None else now_kst
    validate_plan_integrity(plan, now_kst=observed)

    embedded = plan["admission"]
    try:
        inputs = AdmissionInputs(**embedded["inputs"])
    except (KeyError, TypeError) as exc:
        raise FidReadRunPlanError("invalid embedded admission inputs") from exc

    fresh = admission_runner(inputs, now_kst=observed)
    if fresh.get("status") != RUN_READY:
        return {
            "schema": "fid_read_ab_run_plan_verification_v1",
            "status": "NOT_READY",
            "fresh_admission": fresh,
            "manual_command": None,
            "note": "Collector command is withheld because fresh admission is not RUN_READY.",
        }
    if fresh.get("inputs", {}).get("expected_revision") != plan.get("expected_revision"):
        raise FidReadRunPlanError("fresh admission revision mismatch")

    command = plan.get("manual_command")
    if not isinstance(command, list) or not all(isinstance(x, str) and x for x in command):
        raise FidReadRunPlanError("bounded manual command list required")
    expected_tail = [
        "--storage", "raw-v2",
        "--capture-telemetry",
        "--fid-read-ab-test",
        "--duration-seconds", "90",
    ]
    if command[2:] != expected_tail:
        raise FidReadRunPlanError("manual command contract mismatch")

    return {
        "schema": "fid_read_ab_run_plan_verification_v1",
        "status": "MANUAL_COMMAND_READY",
        "verified_at_kst": observed.isoformat(),
        "plan_admission_sha256": plan["admission_sha256"],
        "fresh_admission": fresh,
        "working_directory": plan["working_directory"],
        "manual_command": command,
        "automatic_execution": False,
        "note": (
            "The command is displayed for explicit manual execution only. "
            "Verification does not launch the collector."
        ),
    }
