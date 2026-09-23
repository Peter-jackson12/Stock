from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys

import pytest

from collector.kiwoom.fid_read_ab_admission import KST, RUN_BLOCKED, RUN_READY, RUN_UNCERTAIN
from collector.kiwoom.fid_read_ab_run_plan import (
    MAX_ADMISSION_AGE_SECONDS,
    PLAN_TTL_SECONDS,
    FidReadRunPlanError,
    admission_sha256,
    build_run_plan,
    powershell_command,
    read_run_plan,
    validate_plan_integrity,
    verify_plan_for_manual_command,
    write_new_plan,
)

REV = "337212fd7fd0d9c2bc6597d7bb80005374bff651"


def make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    collector = root / "collector" / "kiwoom" / "kiwoom_universe_logger.py"
    collector.parent.mkdir(parents=True)
    collector.write_text("# fixture collector\n", encoding="utf-8")
    return root


def ready_admission(tmp_path: Path, *, observed=None, status=RUN_READY):
    root = make_repo(tmp_path)
    observed = observed or datetime(2026, 9, 28, 10, 0, 0, tzinfo=KST)
    blockers = [] if status == RUN_READY else [{"id": "fixture", "reason": "blocked"}]
    uncertain = [] if status != RUN_UNCERTAIN else [{"id": "fixture", "reason": "uncertain"}]
    if status == RUN_UNCERTAIN:
        blockers = []
    return {
        "schema": "fid_read_ab_admission_v1",
        "status": status,
        "observed_at_kst": observed.isoformat(),
        "inputs": {
            "repo_root": str(root.resolve()),
            "expected_revision": REV,
            "official_market_date": "2026-09-28",
            "official_market_source_note": "KRX official page checked at 09:59 KST",
            "execution_approved": True,
        },
        "checks": {
            "git": {
                "head": REV,
                "clean": True,
                "status_lines": [],
                "network_used": False,
                "git_fetch_performed": False,
            },
            "preflight": {
                "python_bits": 32,
                "executable": sys.executable,
                "login_attempted": False,
                "ocx_instantiated": False,
                "ready": True,
                "ocx_registered": True,
                "ocx_file_exists": True,
            },
            "disk": {"free_bytes": 10**12, "meets_code_minimum": True},
            "processes": {
                "probe_ok": True,
                "collector_processes": [],
                "same_python_runtime_other_processes": [],
                "runtime_or_openapi_windows": [],
                "actions_taken": [],
            },
            "lease": {"state": "absent", "confirmed_free": True, "file_created": False},
            "cli_contract": {"valid": True, "error": None},
            "market_attestation": {
                "date": "2026-09-28",
                "source_note": "KRX official page checked at 09:59 KST",
                "external_attestation_only": True,
                "target_window_kst": "09:15<=time<15:15",
            },
        },
        "blockers": blockers,
        "uncertain": uncertain,
        "non_actions": ["no OCX instantiation/login/SetRealReg"],
    }


def test_build_plan_binds_ready_admission_exact_revision_and_manual_command(tmp_path):
    now = datetime(2026, 9, 28, 10, 0, 20, tzinfo=KST)
    admission = ready_admission(tmp_path, observed=now - timedelta(seconds=20))
    plan = build_run_plan(admission, expected_revision=REV, now_kst=now)
    assert plan["schema"] == "fid_read_ab_run_plan_v1"
    assert plan["expected_revision"] == REV
    assert plan["admission_sha256"] == admission_sha256(admission)
    assert plan["ttl_seconds"] == PLAN_TTL_SECONDS
    assert plan["execution"] == {
        "automatic_execution": False,
        "requires_manual_review": True,
        "requires_fresh_verification": True,
    }
    assert plan["manual_command"][0] == sys.executable
    assert plan["manual_command"][2:] == [
        "--storage", "raw-v2",
        "--capture-telemetry",
        "--fid-read-ab-test",
        "--duration-seconds", "90",
    ]
    assert Path(plan["manual_command"][1]).name == "kiwoom_universe_logger.py"


def test_non_ready_admission_cannot_create_plan(tmp_path):
    now = datetime(2026, 9, 28, 10, 0, 20, tzinfo=KST)
    admission = ready_admission(tmp_path, observed=now, status=RUN_BLOCKED)
    with pytest.raises(FidReadRunPlanError, match="RUN_READY"):
        build_run_plan(admission, expected_revision=REV, now_kst=now)


@pytest.mark.parametrize("age", [MAX_ADMISSION_AGE_SECONDS + 0.001, 120])
def test_stale_admission_cannot_create_plan(tmp_path, age):
    now = datetime(2026, 9, 28, 10, 0, 20, tzinfo=KST)
    admission = ready_admission(tmp_path, observed=now - timedelta(seconds=age))
    with pytest.raises(FidReadRunPlanError, match="stale"):
        build_run_plan(admission, expected_revision=REV, now_kst=now)


def test_ready_label_is_revalidated_for_process_and_market_contracts(tmp_path):
    now = datetime(2026, 9, 28, 10, 0, 20, tzinfo=KST)
    admission = ready_admission(tmp_path, observed=now)
    admission["checks"]["processes"]["collector_processes"] = [{"pid": 999}]
    with pytest.raises(FidReadRunPlanError, match="process state"):
        build_run_plan(admission, expected_revision=REV, now_kst=now)

    admission = ready_admission(tmp_path / "market", observed=now)
    admission["inputs"]["official_market_date"] = "2026-09-27"
    admission["checks"]["market_attestation"]["date"] = "2026-09-27"
    with pytest.raises(FidReadRunPlanError, match="market date"):
        build_run_plan(admission, expected_revision=REV, now_kst=now)


def test_plan_is_create_only_and_round_trips(tmp_path):
    now = datetime(2026, 9, 28, 10, 0, 20, tzinfo=KST)
    admission = ready_admission(tmp_path, observed=now)
    plan = build_run_plan(admission, expected_revision=REV, now_kst=now)
    target = tmp_path / "plans" / "one.json"
    written = write_new_plan(target, plan)
    assert written == target.resolve()
    assert read_run_plan(target) == plan
    with pytest.raises(FidReadRunPlanError, match="already exists"):
        write_new_plan(target, plan)


def test_embedded_admission_tamper_is_detected(tmp_path):
    now = datetime(2026, 9, 28, 10, 0, 20, tzinfo=KST)
    admission = ready_admission(tmp_path, observed=now)
    plan = build_run_plan(admission, expected_revision=REV, now_kst=now)
    plan["admission"]["inputs"]["official_market_source_note"] = "tampered"
    with pytest.raises(FidReadRunPlanError, match="digest mismatch"):
        validate_plan_integrity(plan, now_kst=now)


def test_expired_plan_is_rejected(tmp_path):
    now = datetime(2026, 9, 28, 10, 0, 20, tzinfo=KST)
    admission = ready_admission(tmp_path, observed=now)
    plan = build_run_plan(admission, expected_revision=REV, now_kst=now)
    with pytest.raises(FidReadRunPlanError, match="expired"):
        validate_plan_integrity(
            plan,
            now_kst=now + timedelta(seconds=PLAN_TTL_SECONDS, microseconds=1),
        )


def test_fresh_ready_admission_recomputes_and_reveals_manual_command(tmp_path):
    now = datetime(2026, 9, 28, 10, 0, 20, tzinfo=KST)
    admission = ready_admission(tmp_path, observed=now)
    plan = build_run_plan(admission, expected_revision=REV, now_kst=now)

    fresh = deepcopy(admission)
    fresh["observed_at_kst"] = (now + timedelta(seconds=10)).isoformat()
    seen = []
    def runner(inputs, *, now_kst):
        seen.append((inputs, now_kst))
        return fresh

    result = verify_plan_for_manual_command(
        plan,
        trusted_expected_revision=REV,
        execution_approved_now=True,
        now_kst=now + timedelta(seconds=10),
        admission_runner=runner,
    )
    assert result["status"] == "MANUAL_COMMAND_READY"
    assert result["manual_command"] == plan["manual_command"]
    assert result["automatic_execution"] is False
    assert result["manual_command_powershell"].startswith("'")
    assert len(seen) == 1


@pytest.mark.parametrize("fresh_status", [RUN_BLOCKED, RUN_UNCERTAIN])
def test_fresh_non_ready_admission_withholds_manual_command(tmp_path, fresh_status):
    now = datetime(2026, 9, 28, 10, 0, 20, tzinfo=KST)
    admission = ready_admission(tmp_path, observed=now)
    plan = build_run_plan(admission, expected_revision=REV, now_kst=now)
    fresh = deepcopy(admission)
    fresh["status"] = fresh_status
    fresh["blockers"] = [{"id": "fixture"}] if fresh_status == RUN_BLOCKED else []
    fresh["uncertain"] = [{"id": "fixture"}] if fresh_status == RUN_UNCERTAIN else []

    result = verify_plan_for_manual_command(
        plan,
        trusted_expected_revision=REV,
        execution_approved_now=True,
        now_kst=now + timedelta(seconds=1),
        admission_runner=lambda *_a, **_k: fresh,
    )
    assert result["status"] == "NOT_READY"
    assert result["manual_command"] is None


def test_tampered_manual_executable_is_rejected_even_when_admission_digest_is_valid(tmp_path):
    now = datetime(2026, 9, 28, 10, 0, 20, tzinfo=KST)
    admission = ready_admission(tmp_path, observed=now)
    plan = build_run_plan(admission, expected_revision=REV, now_kst=now)
    plan["manual_command"][0] = "C:/unexpected/python.exe"

    fresh = deepcopy(admission)
    fresh["observed_at_kst"] = (now + timedelta(seconds=1)).isoformat()
    with pytest.raises(FidReadRunPlanError, match="manual command changed"):
        verify_plan_for_manual_command(
            plan,
            trusted_expected_revision=REV,
            execution_approved_now=True,
            now_kst=now + timedelta(seconds=1),
            admission_runner=lambda *_a, **_k: fresh,
        )


def test_changed_working_directory_is_rejected(tmp_path):
    now = datetime(2026, 9, 28, 10, 0, 20, tzinfo=KST)
    admission = ready_admission(tmp_path, observed=now)
    plan = build_run_plan(admission, expected_revision=REV, now_kst=now)
    plan["working_directory"] = str(tmp_path / "other")

    fresh = deepcopy(admission)
    fresh["observed_at_kst"] = (now + timedelta(seconds=1)).isoformat()
    with pytest.raises(FidReadRunPlanError, match="working directory changed"):
        verify_plan_for_manual_command(
            plan,
            trusted_expected_revision=REV,
            execution_approved_now=True,
            now_kst=now + timedelta(seconds=1),
            admission_runner=lambda *_a, **_k: fresh,
        )


def test_plan_revision_must_match_fresh_trusted_revision(tmp_path):
    now = datetime(2026, 9, 28, 10, 0, 20, tzinfo=KST)
    admission = ready_admission(tmp_path, observed=now)
    plan = build_run_plan(admission, expected_revision=REV, now_kst=now)
    with pytest.raises(FidReadRunPlanError, match="trusted expected revision"):
        verify_plan_for_manual_command(
            plan,
            trusted_expected_revision="other",
            execution_approved_now=True,
            now_kst=now + timedelta(seconds=1),
            admission_runner=lambda *_a, **_k: admission,
        )


def test_fresh_execution_approval_is_required_to_reveal_command(tmp_path):
    now = datetime(2026, 9, 28, 10, 0, 20, tzinfo=KST)
    admission = ready_admission(tmp_path, observed=now)
    plan = build_run_plan(admission, expected_revision=REV, now_kst=now)
    result = verify_plan_for_manual_command(
        plan,
        trusted_expected_revision=REV,
        execution_approved_now=False,
        now_kst=now + timedelta(seconds=1),
        admission_runner=lambda *_a, **_k: pytest.fail("fresh admission should not run without approval"),
    )
    assert result["status"] == "NOT_READY"
    assert result["manual_command"] is None
    assert result["fresh_admission"] is None


def test_powershell_command_quotes_single_quotes_without_shell_execution():
    command = ["C:/Program Files/Python/python.exe", "C:/repo/o'hare/script.py", "--flag"]
    text = powershell_command(command)
    assert text == "'C:/Program Files/Python/python.exe' 'C:/repo/o''hare/script.py' '--flag'"


def test_prepare_cli_creates_plan_only_for_ready_admission(tmp_path, monkeypatch, capsys):
    import scripts.prepare_fid_read_ab_run as script

    root = make_repo(tmp_path)
    now = datetime(2026, 9, 28, 10, 0, 20, tzinfo=KST)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)
    monkeypatch.setattr(script, "datetime", FixedDatetime)
    admission = ready_admission(tmp_path / "fixture", observed=now)
    admission["inputs"]["repo_root"] = str(root.resolve())
    admission["checks"]["preflight"]["executable"] = sys.executable
    admission["checks"]["git"]["head"] = REV
    monkeypatch.setattr(script, "collect_and_evaluate", lambda *_a, **_k: admission)

    output = tmp_path / "plan.json"
    code = script.main([
        "--repo-root", str(root),
        "--expected-revision", REV,
        "--official-market-date", now.date().isoformat(),
        "--official-market-source-note", "KRX checked",
        "--execution-approved",
        "--output", str(output),
    ])
    assert code == 0 and output.is_file()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PLAN_CREATED"
    assert payload["automatic_execution"] is False


def test_prepare_cli_does_not_create_plan_when_admission_is_blocked(tmp_path, monkeypatch, capsys):
    import scripts.prepare_fid_read_ab_run as script

    root = make_repo(tmp_path)
    blocked = ready_admission(tmp_path / "fixture", observed=datetime.now(KST), status=RUN_BLOCKED)
    monkeypatch.setattr(script, "collect_and_evaluate", lambda *_a, **_k: blocked)
    output = tmp_path / "plan.json"
    code = script.main([
        "--repo-root", str(root),
        "--expected-revision", REV,
        "--official-market-date", "2026-09-28",
        "--official-market-source-note", "KRX checked",
        "--execution-approved",
        "--output", str(output),
    ])
    assert code == 2 and not output.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "PLAN_NOT_CREATED"


def test_verify_cli_never_launches_collector_and_returns_verification(monkeypatch, tmp_path, capsys):
    import scripts.verify_fid_read_ab_run_plan as script

    plan_file = tmp_path / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(script, "read_run_plan", lambda _p: {"schema": "fixture"})
    monkeypatch.setattr(script, "verify_plan_for_manual_command", lambda *_a, **_k: {
        "schema": "fid_read_ab_run_plan_verification_v1",
        "status": "MANUAL_COMMAND_READY",
        "manual_command": ["python", "collector.py"],
        "automatic_execution": False,
    })
    code = script.main([
        "--plan", str(plan_file),
        "--expected-revision", REV,
        "--execution-approved",
    ])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["automatic_execution"] is False
