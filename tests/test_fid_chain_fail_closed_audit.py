"""독립 fail-closed 감사. 정상이어야 할 계약을 assert하며 실패를 숨기지 않는다.

감사 원본은 PR #28 c156108c269ffec55fc1bf7c843397d56781f112 production에서 26개 중 23개가
실패했다(CI #321, run 35926442378). 이후 보강 커밋의 production은 같은 assertion을 통과해야 한다.
기존 fixture는 입력 구성에만 재사용한다. oracle는 이 파일의 독립 안전 조건이다.
OS/시장 관측은 대역, Git은 pytest 임시 저장소, PowerShell은 AST 파싱만 수행한다.
실제 collector/OCX/raw 데이터에는 접근하지 않는다.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from collector.kiwoom import fid_read_ab_admission as ad
from collector.kiwoom import fid_read_ab_run_plan as rp
from collector.kiwoom import fid_read_ab_analysis as an
from collector.kiwoom import fid_read_ab_assessment as assess
from tests.test_fid_read_ab_run_plan import ready_admission, REV
from tests.test_fid_read_ab_analysis import make_session
from tests.test_fid_read_ab_assessment import analysis_fixture

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=ad.KST)  # 합성 시각, 거래일 주장 아님.


@pytest.fixture(autouse=True)
def no_raw_connections(monkeypatch):
    import sqlite3
    def forbidden(*args, **kwargs):
        raise AssertionError("audit must not open any SQLite/raw database")
    monkeypatch.setattr(sqlite3, "connect", forbidden)


def fresh_at(admission, when):
    value = deepcopy(admission)
    value["observed_at_kst"] = when.isoformat()
    return value


def evaluate(value, *, processes=None, when=NOW):
    c = value["checks"]
    return ad.evaluate_admission(
        ad.AdmissionInputs(**value["inputs"]), now_kst=when,
        git=c["git"], preflight=c["preflight"], disk=c["disk"],
        processes=c["processes"] if processes is None else processes,
        lease=c["lease"], cli_contract=c["cli_contract"],
    )


def make_plan(tmp_path, when=NOW):
    admission = ready_admission(tmp_path, observed=when)
    return admission, rp.build_run_plan(admission, expected_revision=REV, now_kst=when)


def verify(plan, fresh, *, when=NOW, expected=REV):
    return rp.verify_plan_for_manual_command(
        plan, trusted_expected_revision=expected, execution_approved_now=True,
        now_kst=when, admission_runner=lambda *_a, **_k: fresh,
    )


def must_not_reveal(call, tag):
    try:
        result = call()
    except (ValueError, OSError) as exc:
        print("AUDIT", tag, "rejected", type(exc).__name__)
        return
    print("AUDIT", tag, "status", result.get("status"),
          "command_present", bool(result.get("manual_command")))
    assert result.get("status") != "MANUAL_COMMAND_READY", tag
    assert result.get("manual_command") is None, tag


# 정상/기존 차단이 동작하는지 먼저 확인: harness 자체의 붕괴와 구분한다.
def test_control_valid_plan_still_verifies(tmp_path):
    a, p = make_plan(tmp_path)
    r = verify(p, fresh_at(a, NOW + timedelta(seconds=1)), when=NOW + timedelta(seconds=1))
    assert r["status"] == "MANUAL_COMMAND_READY"
    assert r["automatic_execution"] is False


@pytest.mark.parametrize("status", [ad.RUN_BLOCKED, ad.RUN_UNCERTAIN])
def test_control_explicit_block_withholds_command(tmp_path, status):
    # 수정 전 harness는 plan에 공유된 입력 객체를 바꿔 digest mismatch로 먼저 거부됐다.
    # fresh 응답을 deepcopy로 분리해야 BLOCKED/UNCERTAIN 분기 자체를 검증한다.
    a, p = make_plan(tmp_path)
    fresh = deepcopy(a)
    fresh["status"] = status
    fresh["blockers" if status == ad.RUN_BLOCKED else "uncertain"] = [{"id": "fixture"}]
    must_not_reveal(lambda: verify(p, fresh), "explicit_" + status)
    result = verify(p, fresh)  # 예외가 아니라 fresh 비READY 분기에서 명령을 숨겨야 한다.
    assert result["status"] == "NOT_READY" and result["fresh_admission"]["status"] == status


def test_control_command_path_tamper_is_rejected(tmp_path):
    a, p = make_plan(tmp_path)
    p["manual_command"][0] = "C:/fixture/unexpected.exe"
    must_not_reveal(lambda: verify(p, a), "command_tamper")


@pytest.mark.parametrize("raw", [
    {},
    {"python_processes": None, "titled_windows": None},
    {"python_processes": [17], "titled_windows": []},
    {"python_processes": [{"ProcessId": 222, "Name": "python.exe",
                            "CommandLine": None, "ExecutablePath": None}],
     "titled_windows": []},
], ids=["missing_keys", "null_collections", "malformed_row", "inaccessible_identity"])
def test_process_probe_missing_evidence_is_not_clear(tmp_path, monkeypatch, raw):
    a, _ = make_plan(tmp_path)
    monkeypatch.setattr(ad, "_powershell_json", lambda *_a, **_k: raw)
    proc = ad.inspect_processes(tmp_path, own_pid=111)
    r = evaluate(a, processes=proc)
    print("AUDIT process_gap", r["status"], "probe_ok", proc.get("probe_ok"))
    assert r["status"] != ad.RUN_READY, "missing process evidence was treated as absence"


def test_module_invoked_collector_in_other_python_is_not_clear(tmp_path, monkeypatch):
    a, _ = make_plan(tmp_path)
    raw = {"python_processes": [{"ProcessId": 222, "Name": "python.exe",
            "CommandLine": "C:/other/python.exe -m collector.kiwoom.kiwoom_universe_logger",
            "ExecutablePath": "C:/other/python.exe"}], "titled_windows": []}
    monkeypatch.setattr(ad, "_powershell_json", lambda *_a, **_k: raw)
    proc = ad.inspect_processes(tmp_path, own_pid=111)
    r = evaluate(a, processes=proc)
    print("AUDIT module_collector", r["status"])
    assert r["status"] != ad.RUN_READY


def clock_class(box):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return box[0].astimezone(tz) if tz else box[0].replace(tzinfo=None)
    return Clock


def test_admission_rechecks_time_after_slow_probe(tmp_path, monkeypatch):
    start = NOW.replace(hour=15, minute=14, second=59)
    a = ready_admission(tmp_path, observed=start)
    c = a["checks"]
    clock = [start]
    monkeypatch.setattr(ad, "datetime", clock_class(clock))
    monkeypatch.setattr(ad, "inspect_git", lambda *_a: c["git"])
    monkeypatch.setattr(ad, "inspect_environment", lambda: c["preflight"])
    monkeypatch.setattr(ad, "inspect_disk", lambda *_a: c["disk"])
    monkeypatch.setattr(ad, "inspect_existing_lease", lambda *_a: c["lease"])
    monkeypatch.setattr(ad, "exact_diagnostic_cli_contract", lambda: c["cli_contract"])
    def slow_process(*args):
        clock[0] += timedelta(seconds=2)
        return c["processes"]
    r = ad.collect_and_evaluate(ad.AdmissionInputs(**a["inputs"]), process_probe=slow_process)
    print("AUDIT slow_admission", r["status"], "finished", clock[0].isoformat(),
          "reported", r["observed_at_kst"])
    assert r["status"] != ad.RUN_READY


@pytest.mark.parametrize("mutation", ["wrong_bits", "active_collector", "changed_head"])
def test_fresh_ready_label_cannot_override_internal_conflict(tmp_path, mutation):
    a, p = make_plan(tmp_path)
    fresh = deepcopy(a)
    if mutation == "wrong_bits":
        fresh["checks"]["preflight"]["python_bits"] = 64
    elif mutation == "active_collector":
        fresh["checks"]["processes"]["collector_processes"] = [{"pid": 222}]
    else:
        fresh["checks"]["git"]["head"] = "0" * 40
    must_not_reveal(lambda: verify(p, fresh), "fresh_" + mutation)


def test_verify_rechecks_expiration_after_slow_admission(tmp_path, monkeypatch):
    a, p = make_plan(tmp_path)
    clock = [NOW + timedelta(seconds=rp.PLAN_TTL_SECONDS - 1)]
    monkeypatch.setattr(rp, "datetime", clock_class(clock))
    def slow_fresh(*args, **kwargs):
        clock[0] += timedelta(seconds=2)
        return fresh_at(a, clock[0])
    must_not_reveal(lambda: rp.verify_plan_for_manual_command(
        p, trusted_expected_revision=REV, execution_approved_now=True,
        admission_runner=slow_fresh), "expired_during_verify")


def test_exact_expiry_is_not_a_valid_execution_instant(tmp_path):
    a, p = make_plan(tmp_path)
    end = NOW + timedelta(seconds=rp.PLAN_TTL_SECONDS)
    must_not_reveal(lambda: verify(p, fresh_at(a, end), when=end), "exact_expiry")


def test_retimestamped_plan_cannot_reuse_old_admission(tmp_path):
    a, p = make_plan(tmp_path)
    later = NOW + timedelta(minutes=10)
    p["created_at_kst"] = later.isoformat()
    p["expires_at_kst"] = (later + timedelta(seconds=rp.PLAN_TTL_SECONDS)).isoformat()
    # admission과 digest는 그대로: created 시점에 admission이 10분 오래된 모순이다.
    must_not_reveal(lambda: verify(p, fresh_at(a, later), when=later), "retimestamped_plan")


def test_ignored_subdirectory_cannot_borrow_parent_git_identity(tmp_path):
    root = tmp_path / "actual_repo"
    root.mkdir()
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (root / "marker.txt").write_text("tracked fixture\n", encoding="utf-8")
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, env=env, check=True,
                              text=True, capture_output=True, timeout=10).stdout.strip()
    git("init", "-q")
    git("add", ".gitignore", "marker.txt")
    git("-c", "user.name=Audit Fixture", "-c", "user.email=audit@example.invalid",
        "-c", "commit.gpgSign=false", "commit", "-qm", "fixture")
    child = root / "ignored"
    entry = child / "collector" / "kiwoom" / "kiwoom_universe_logger.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("# not tracked by the approved Git commit; never execute\n", encoding="utf-8")
    observed_git = ad.inspect_git(child)
    a, _ = make_plan(tmp_path / "fixture")
    a["inputs"]["repo_root"] = str(child.resolve())
    a["inputs"]["expected_revision"] = observed_git["head"]
    a["checks"]["git"] = observed_git
    def attempt():
        admission = evaluate(a)
        plan = rp.build_run_plan(admission, expected_revision=observed_git["head"], now_kst=NOW)
        return verify(plan, admission, expected=observed_git["head"])
    must_not_reveal(attempt, "ignored_child_borrows_parent_sha")


def test_powershell_display_is_a_parseable_invocation():
    # AST 검사만 수행: 아래 경로의 프로그램을 절대로 호출하지 않는다.
    command = rp.powershell_command(["C:/fixture/no program.exe", "C:/fixture/no script.py", "--flag"])
    script = ("$t=$null; $e=$null; "
              "[System.Management.Automation.Language.Parser]::ParseInput("
              "$env:FID_AUDIT_COMMAND,[ref]$t,[ref]$e) | Out-Null; "
              "Write-Output (@($e).Count)")
    env = dict(os.environ, FID_AUDIT_COMMAND=command)
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=env, check=True, capture_output=True, text=True, timeout=10)
    errors = int(r.stdout.strip())
    print("AUDIT powershell_parse_errors", errors)
    assert errors == 0
    assert command.lstrip().startswith("& "), "quoted executable needs an invocation operator"


def stale_size(path, monkeypatch):
    original = Path.stat
    def replacement(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self == path:
            fields = list(result)
            fields[6] = 0  # stat 후 증가/교체된 파일의 크기를 결정적으로 주입한다.
            return os.stat_result(fields)
        return result
    monkeypatch.setattr(Path, "stat", replacement)


def test_plan_reader_checks_actual_bytes_not_only_earlier_stat(tmp_path, monkeypatch):
    path = (tmp_path / "plan.json").resolve()
    path.write_text(json.dumps({"schema": rp.PLAN_SCHEMA, "padding": "x" * (rp.MAX_PLAN_BYTES + 1)}),
                    encoding="utf-8")
    stale_size(path, monkeypatch)
    with pytest.raises((ValueError, OSError)):
        rp.read_run_plan(path)


def test_analyzer_reader_checks_actual_bytes_not_only_earlier_stat(tmp_path, monkeypatch):
    path = (tmp_path / "small.json").resolve()
    path.write_text("x" * 64, encoding="utf-8")
    stale_size(path, monkeypatch)
    with pytest.raises((ValueError, OSError)):
        an._bounded_text(path, 32)


@pytest.mark.parametrize("mutation", ["nested_error", "invalid_finalization", "missing_process_identity", "bool_queue_count"])
def test_analyzer_does_not_certify_malformed_completion(tmp_path, mutation):
    session, raw = make_session(tmp_path)
    status_path = session / "status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    if mutation == "nested_error":
        status["snapshot"]["error"] = "synthetic writer failure"
    elif mutation == "invalid_finalization":
        status["snapshot"]["finalization"]["payload_sha256"] = "not-a-hash"
        status["snapshot"]["finalization"]["close_ns"] = -1
    elif mutation == "missing_process_identity":
        for key in ("pid", "executable", "started_at_utc"):
            status["identity"].pop(key, None)
    else:
        status["snapshot"]["pending_callbacks"] = False
        status["snapshot"]["queued"] = False
    status_path.write_text(json.dumps(status), encoding="utf-8")
    result = an.analyze_fid_read_ab_session(session)
    print("AUDIT malformed_completion", mutation, result["result"])
    assert result["result"] != an.RESULT_READY
    assert not raw.exists()


@pytest.mark.parametrize("bad", [-1, float("inf"), float("nan")], ids=["negative", "infinity", "nan"])
def test_assessment_invalid_duration_is_not_a_directional_observation(bad):
    report = analysis_fixture(trade_fid=(100, bad, 110), quote_fid=(200, bad, 220))
    result = assess.assess_fid_read_ab(report)
    print("AUDIT invalid_metric", repr(bad), result["assessment"])
    assert result["assessment"] == assess.OVERALL_NOT_ASSESSABLE
