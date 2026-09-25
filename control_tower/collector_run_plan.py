"""Review-only collection Run Plans for the Operator UI.

The plan deliberately has no queue, worker, launch token, or approval transition.
It snapshots user intent and read-only observations so a human can finish the
real execution-specific admission immediately before a separately approved run.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Callable
from uuid import uuid4

from control_tower.collector_preflight import BLOCKED, PASS, UNVERIFIED, WARN
from control_tower.jobs import bounded_json
from control_tower.managed_capture import validate_plan


SCHEMA = "operator_collection_run_plan_v1"
STATE = "review_only"


def _axis(check_id: str, label: str, status: str, detail: str, next_check: str) -> dict:
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "detail": detail,
        "next_check": next_check,
    }


def _status(axes: list[dict]) -> str:
    statuses = {axis["status"] for axis in axes}
    if BLOCKED in statuses:
        return BLOCKED
    if UNVERIFIED in statuses:
        return UNVERIFIED
    if WARN in statuses:
        return WARN
    return PASS


def _bounded_optional(value, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    value = value.strip()
    if len(value) > 128 or any(character in value for character in "\r\n\0"):
        raise ValueError(f"{label} is too long or contains control characters")
    return value


def summarize_preflight(report: dict) -> dict:
    """Keep only the bounded decision fields needed by a saved plan."""
    if not isinstance(report, dict):
        report = {}
    allowed = {PASS, WARN, BLOCKED, UNVERIFIED}
    overall = report.get("status") if report.get("status") in allowed else UNVERIFIED
    local = report.get("local_status") if report.get("local_status") in allowed else UNVERIFIED
    checks = []
    for item in report.get("checks", []):
        if not isinstance(item, dict) or len(checks) >= 24:
            continue
        checks.append({
            "id": str(item.get("id", "unknown"))[:64],
            "label": str(item.get("label", "이름 없는 점검"))[:128],
            "status": item.get("status") if item.get("status") in allowed else UNVERIFIED,
            "next_check": str(item.get("next_check", "실행 직전에 다시 확인하세요."))[:512],
        })
    return {
        "schema": report.get("schema", "unavailable"),
        "status": overall,
        "local_status": local,
        "observed_at_local": report.get("observed_at_local"),
        "checks": checks,
    }


def inspect_run_target(root: Path, *, runner: Callable = subprocess.run) -> dict:
    """Read HEAD and worktree cleanliness without fetching or changing Git state."""
    root = Path(root).resolve()

    def run(*arguments):
        try:
            completed = runner(
                ["git", "-C", str(root), *arguments],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"{type(exc).__name__}: {str(exc)[:384]}"
        if completed.returncode != 0:
            return None, (completed.stderr or completed.stdout or "git probe failed").strip()[:384]
        if len((completed.stdout or "").encode("utf-8")) > 64 * 1024:
            return None, "git probe output exceeded 64 KiB"
        return (completed.stdout or "").strip(), None

    revision, error = run("rev-parse", "--verify", "HEAD")
    if error or not revision:
        return {"probe_ok": False, "error": error or "HEAD unavailable"}
    worktree, error = run("status", "--porcelain=v1", "--untracked-files=normal")
    if error or worktree is None:
        return {"probe_ok": False, "revision": revision, "error": error or "worktree unavailable"}
    branch, branch_error = run("symbolic-ref", "--short", "-q", "HEAD")
    entries = [line for line in worktree.splitlines() if line]
    return {
        "probe_ok": True,
        "revision": revision,
        "branch": None if branch_error else branch,
        "clean": not entries,
        "dirty_entry_count": len(entries),
    }


def build_collection_run_plan(
    *,
    codes: list[str],
    duration_seconds: int,
    server: str,
    required_storage_mib: int | None,
    planned_market_date: str,
    planned_market_segment: str,
    preflight: dict,
    run_target: dict,
) -> dict:
    """Build a reviewable plan from user intent and already-observed facts."""
    validate_plan(codes, duration_seconds, server)
    if required_storage_mib is not None and (
        type(required_storage_mib) is not int or not 1 <= required_storage_mib <= 1024 * 1024
    ):
        raise ValueError("required storage must be 1..1048576 MiB or left unverified")
    market_date = _bounded_optional(planned_market_date, "planned market date")
    market_segment = _bounded_optional(planned_market_segment, "planned market segment")
    preflight_summary = summarize_preflight(preflight)

    axes = [_axis(
        "plan_parameters",
        "계획 입력",
        PASS,
        f"server={server}, 종목 {len(codes)}개, 수집 {duration_seconds}초를 검토 대상으로 고정했습니다.",
        "실행 직전에 승인된 CLI/collector 계약과 값이 같은지 다시 대조하세요.",
    )]

    free_bytes = preflight.get("storage_free_bytes") if isinstance(preflight, dict) else None
    required_bytes = required_storage_mib * 1024 * 1024 if required_storage_mib is not None else None
    if required_bytes is None:
        storage_status = UNVERIFIED
        storage_detail = "예상 필요 저장공간을 아직 입력하지 않았습니다."
    elif type(free_bytes) is not int:
        storage_status = UNVERIFIED
        storage_detail = f"예상 필요량 {required_storage_mib:,} MiB를 저장 여유와 대조하지 못했습니다."
    elif free_bytes < required_bytes:
        storage_status = BLOCKED
        storage_detail = (
            f"예상 필요량 {required_storage_mib:,} MiB가 관측 여유 {free_bytes / (1024 ** 2):,.0f} MiB보다 큽니다."
        )
    else:
        storage_status = PASS
        storage_detail = (
            f"예상 필요량 {required_storage_mib:,} MiB가 관측 여유 {free_bytes / (1024 ** 2):,.0f} MiB 이내입니다."
        )
    axes.append(_axis(
        "required_storage",
        "예상/필요 저장공간",
        storage_status,
        storage_detail,
        "코드 admission 하한과 실행별 예상량은 다릅니다. 실행 직전에 예상량·여유·보존 예산을 다시 확인하세요.",
    ))

    if not isinstance(run_target, dict) or run_target.get("probe_ok") is not True:
        target_status = UNVERIFIED
        target_detail = str((run_target or {}).get("error") or "Git revision/clean tree를 읽지 못했습니다.")[:512]
    elif run_target.get("clean") is True:
        target_status = PASS
        target_detail = (
            f"revision={run_target.get('revision')}, branch={run_target.get('branch') or 'detached'}, clean tree를 관측했습니다."
        )
    else:
        target_status = WARN
        target_detail = (
            f"revision={run_target.get('revision')}, dirty entries={run_target.get('dirty_entry_count', 'unknown')}입니다."
        )
    axes.append(_axis(
        "run_target",
        "실행 대상 revision · clean tree",
        target_status,
        target_detail,
        "이 값은 계획 시점의 로컬 관측입니다. 실행 직전 admission에서 HEAD와 clean tree를 새로 확인하세요.",
    ))

    axes.append(_axis(
        "preflight_summary",
        "수집 전 preflight 요약",
        preflight_summary["local_status"],
        f"전체 {preflight_summary['status']} · 로컬 관측 {preflight_summary['local_status']} 스냅샷입니다.",
        "저장된 스냅샷을 현재 상태로 간주하지 말고 실행 직전에 preflight를 새로 수행하세요.",
    ))
    intent = ", ".join(value for value in (market_date, market_segment) if value) or "계획값도 아직 미입력"
    axes.extend((
        _axis(
            "market_session",
            "실제 시장 날짜 · 장 구간",
            UNVERIFIED,
            f"{intent}. 외부 공식 시장 일정과 실제 구간은 조회하거나 검증하지 않았습니다.",
            "공식 출처와 수집 의사결정 계약으로 거래일·목표 구간·준비 완료 시각을 확인하세요.",
        ),
        _axis(
            "execution_approval",
            "수집 실행 승인",
            UNVERIFIED,
            "계획 검토/저장은 실행 승인을 만들지 않으며 어떤 시작 경로에도 연결되지 않습니다.",
            "모든 최신 근거와 실행별 admission을 확인한 뒤 사람이 별도로 실행 여부를 결정하세요.",
        ),
    ))

    return {
        "schema": SCHEMA,
        "state": STATE,
        "status": _status(axes),
        "inputs": {
            "server": server,
            "codes": list(codes),
            "duration_seconds": duration_seconds,
            "required_storage_mib": required_storage_mib,
            "planned_market_date": market_date or None,
            "planned_market_segment": market_segment or None,
        },
        "run_target": dict(run_target) if isinstance(run_target, dict) else {"probe_ok": False},
        "preflight_summary": preflight_summary,
        "checks": axes,
        "execution_approved": False,
        "launch_connected": False,
        "managed_capture_activated": False,
        "non_actions": [
            "no login/OCX/SetRealReg/collector start",
            "no kill/restart/lock deletion",
            "no market query",
            "no raw database access",
        ],
    }


class CollectionRunPlanStore:
    """Immutable review-only plans; intentionally separate from executable jobs."""

    def __init__(self, root):
        self.path = Path(root).resolve() / "operations_state" / "collector_run_plans.sqlite3"

    @staticmethod
    def _validate(plan: dict) -> str:
        if (
            not isinstance(plan, dict)
            or plan.get("schema") != SCHEMA
            or plan.get("state") != STATE
            or plan.get("execution_approved") is not False
            or plan.get("launch_connected") is not False
            or plan.get("managed_capture_activated") is not False
        ):
            raise ValueError("only a disconnected review-only collection plan can be saved")
        return bounded_json(plan)

    def save(self, plan: dict) -> str:
        encoded = self._validate(plan)
        plan_id = uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=2)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO plans VALUES (?,?,?)", (plan_id, encoded, created_at))
            conn.commit()
        finally:
            conn.close()
        return plan_id

    def recent(self, limit: int = 5) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("bounded plan listing required")
        if not self.path.exists():
            return []
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id,payload,created_at FROM plans ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
            return [
                {"id": row["id"], "created_at": row["created_at"], "payload": json.loads(row["payload"])}
                for row in rows
            ]
        finally:
            conn.close()
