"""PR #29 감사 반례 보강 회귀: 새 분기의 정확한 판정과 정상 경로 보존을 고정한다.

프로세스 입력은 합성 대역, Git 쓰기는 pytest 임시 저장소, PowerShell은 무해한 python -c
dummy만 실행한다. 실제 collector/OCX/운영 raw/operations_state에는 접근하지 않는다.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from collector.kiwoom import fid_read_ab_admission as ad
from collector.kiwoom import fid_read_ab_run_plan as rp
from collector.kiwoom import fid_read_ab_analysis as an
from collector.kiwoom import fid_read_ab_assessment as assess
from tests.test_fid_read_ab_run_plan import REV, ready_admission
from tests.test_fid_read_ab_analysis import make_session, _write_json
from tests.test_fid_read_ab_assessment import analysis_fixture, metric

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=ad.KST)  # 합성 시각, 거래일 주장 아님.
GIT_IDS = {"revision", "working_tree", "execution_root", "checker_source", "collector_entrypoint", "index_flags"}


@pytest.fixture(autouse=True)
def no_raw_connections(monkeypatch):
    import sqlite3

    def forbidden(*args, **kwargs):
        raise AssertionError("regression must not open any SQLite/raw database")
    monkeypatch.setattr(sqlite3, "connect", forbidden)


def evaluate(value, *, when=NOW, started=None, elapsed=None, **overrides):
    checks = {**value["checks"], **overrides}
    return ad.evaluate_admission(
        ad.AdmissionInputs(**value["inputs"]), now_kst=when,
        git=checks["git"], preflight=checks["preflight"], disk=checks["disk"],
        processes=checks["processes"], lease=checks["lease"], cli_contract=checks["cli_contract"],
        observation_started_kst=started, elapsed_monotonic_seconds=elapsed,
    )


def ids(report):
    return {item["id"] for item in report["blockers"] + report["uncertain"]}


def raw_rows(py=(), marked=(), windows=()):
    return {"python_processes": list(py), "python_process_count": len(py),
            "marker_processes": list(marked), "marker_process_count": len(marked),
            "titled_windows": list(windows), "titled_window_count": len(windows)}


def classify(raw):
    return ad.classify_process_rows(raw, own_pid=111, current_executable="C:/fixture/python32/python.exe")


# --- A. 프로세스 근거 -------------------------------------------------------------

def test_explicit_empty_process_and_window_lists_remain_ready(tmp_path):
    a = ready_admission(tmp_path)
    proc = classify(raw_rows())
    assert proc["probe_ok"] is True and all(proc[name] == [] for name in ad.PROCESS_LISTS)
    assert evaluate(a, processes=proc)["status"] == ad.RUN_READY


@pytest.mark.parametrize("raw", [
    {**raw_rows(), "python_process_count": 1},  # 목록 1개가 잘렸다
    {**raw_rows(), "marker_processes": None},
    {k: v for k, v in raw_rows().items() if k != "titled_window_count"},
    raw_rows(py=[{"ProcessId": True, "Name": "python.exe"}]),
    raw_rows(windows=[{"Id": 5, "ProcessName": "x", "MainWindowTitle": None}]),
    [],
])
def test_truncated_or_malformed_probe_is_uncertain_not_clear(tmp_path, raw):
    proc = classify(raw)
    assert proc["probe_ok"] is False
    report = evaluate(ready_admission(tmp_path), processes=proc)
    assert report["status"] == ad.RUN_UNCERTAIN and ids(report) == {"process_probe"}


@pytest.mark.parametrize("command,name", [
    ("C:/other/python.exe -m collector.kiwoom.kiwoom_universe_logger --storage raw-v2", "python.exe"),
    ("C:/other/python3.10.exe -mcollector.kiwoom.kiwoom_universe_logger", "python3.10.exe"),
    ("C:/tools/uv.exe run collector/kiwoom/kiwoom_universe_logger.py", "uv.exe"),
])
def test_collector_in_any_interpreter_or_module_form_blocks(tmp_path, command, name):
    row = {"ProcessId": 222, "Name": name, "CommandLine": command, "ExecutablePath": "C:/other/" + name}
    proc = classify(raw_rows(marked=[row]) if name == "uv.exe" else raw_rows(py=[row], marked=[row]))
    assert [p["pid"] for p in proc["collector_processes"]] == [222]
    assert evaluate(ready_admission(tmp_path), processes=proc)["status"] == ad.RUN_BLOCKED


def test_inaccessible_python_identity_is_uncertain_and_command_line_is_not_echoed(tmp_path):
    secret = "--password=do-not-print-this"
    proc = classify(raw_rows(py=[
        {"ProcessId": 222, "Name": "python.exe", "CommandLine": None, "ExecutablePath": None},
        {"ProcessId": 333, "Name": "python.exe", "CommandLine": f"python tool.py {secret}",
         "ExecutablePath": "C:/fixture/python32/python.exe"},
        {"ProcessId": 444, "Name": "python.exe",
         "CommandLine": f"python collector/kiwoom/kiwoom_universe_logger.py {secret}",
         "ExecutablePath": "C:/elsewhere/python.exe"},
    ]))
    assert [p["pid"] for p in proc["unidentified_python_processes"]] == [222]
    assert [p["pid"] for p in proc["same_python_runtime_other_processes"]] == [333]
    assert [p["pid"] for p in proc["collector_processes"]] == [444]
    assert secret not in json.dumps(proc)
    only_unidentified = classify(raw_rows(py=[
        {"ProcessId": 222, "Name": "python.exe", "CommandLine": "python x.py", "ExecutablePath": None}]))
    report = evaluate(ready_admission(tmp_path), processes=only_unidentified)
    assert report["status"] == ad.RUN_UNCERTAIN and ids(report) == {"unidentified_python"}


def test_probe_script_excludes_its_own_host_and_parses_without_running(monkeypatch, tmp_path):
    seen = []

    def capture(script, **_kwargs):
        seen.append(script)
        return raw_rows()
    monkeypatch.setattr(ad, "_powershell_json", capture)
    assert ad.inspect_processes(tmp_path, own_pid=111)["probe_ok"] is True
    marker_query = seen[0].split("$marked", 1)[1].split("$windows", 1)[0]
    assert "-ne $PID" in marker_query  # the probe's own command line names the marker.
    parse = ("$t=$null; $e=$null; [System.Management.Automation.Language.Parser]::ParseInput("
             "$env:FID_PROBE_SCRIPT,[ref]$t,[ref]$e) | Out-Null; Write-Output (@($e).Count)")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", parse],
                            env=dict(os.environ, FID_PROBE_SCRIPT=seen[0]), check=True,
                            capture_output=True, text=True, timeout=20)
    assert int(result.stdout.strip()) == 0


def test_own_venv_launcher_parent_is_not_another_runtime_process(tmp_path):
    # uv/venv python.exe는 launcher이고 checker는 그 자식이다(재검토 반례).
    venv, base = "C:/Stock/.venv32/Scripts/python.exe", "C:/Python310-32/python.exe"
    rows = raw_rows(py=[
        {"ProcessId": 500, "Name": "python.exe", "CommandLine": "python scripts/check.py", "ExecutablePath": venv},
        {"ProcessId": 501, "Name": "python.exe", "CommandLine": "python scripts/check.py", "ExecutablePath": base},
        {"ProcessId": 777, "Name": "python.exe", "CommandLine": "python other.py", "ExecutablePath": base},
    ])
    proc = ad.classify_process_rows(rows, own_pid=501, current_executable=venv,
                                    base_executable=base, own_parent_pid=500)
    assert proc["own_launcher_pid_excluded"] == 500
    assert [p["pid"] for p in proc["same_python_runtime_other_processes"]] == [777]
    alone = ad.classify_process_rows(raw_rows(py=rows["python_processes"][:2]), own_pid=501,
                                     current_executable=venv, base_executable=base, own_parent_pid=500)
    assert evaluate(ready_admission(tmp_path), processes=alone)["status"] == ad.RUN_READY
    # A parent that is itself a collector is never excused.
    collector_parent = raw_rows(py=[{"ProcessId": 500, "Name": "python.exe", "ExecutablePath": venv,
                                     "CommandLine": "python collector/kiwoom/kiwoom_universe_logger.py"}])
    blocked = ad.classify_process_rows(collector_parent, own_pid=501, current_executable=venv,
                                       base_executable=base, own_parent_pid=500)
    assert [p["pid"] for p in blocked["collector_processes"]] == [500]


def test_git_timeout_is_an_oserror_for_the_cli(monkeypatch, tmp_path):
    def slow(*_a, **_k):
        raise subprocess.TimeoutExpired("git", 10)
    monkeypatch.setattr(ad.subprocess, "run", slow)
    with pytest.raises(OSError, match="timed out"):
        ad.inspect_git(tmp_path)


def test_probe_command_failure_is_reported_as_gap(monkeypatch, tmp_path):
    def fail(*_a, **_k):
        raise OSError("command failed (1): access denied")
    monkeypatch.setattr(ad, "_powershell_json", fail)
    proc = ad.inspect_processes(tmp_path, own_pid=111)
    assert proc["probe_ok"] is False and proc["actions_taken"] == []


# --- B. 시간과 freshness -----------------------------------------------------------

@pytest.mark.parametrize("start,end", [
    (NOW.replace(hour=15, minute=14, second=58), NOW.replace(hour=15, minute=15, second=0)),
    (NOW.replace(hour=9, minute=14, second=59), NOW.replace(hour=9, minute=15, second=1)),
])
def test_observation_start_and_completion_must_both_be_in_window(tmp_path, start, end):
    report = evaluate(ready_admission(tmp_path), when=end, started=start,
                      elapsed=(end - start).total_seconds())
    assert report["status"] == ad.RUN_BLOCKED and "target_window" in ids(report)
    assert report["observation_started_at_kst"] == start.isoformat()
    assert report["observed_at_kst"] == end.isoformat()


@pytest.mark.parametrize("started,elapsed,expected", [
    (NOW + timedelta(seconds=5), 0.0, "clock_regression"),
    (NOW - timedelta(seconds=10), 0.5, "clock_consistency"),
    (NOW, float("nan"), "clock_evidence"),
])
def test_wall_clock_regression_or_monotonic_mismatch_is_uncertain(tmp_path, started, elapsed, expected):
    report = evaluate(ready_admission(tmp_path), when=NOW, started=started, elapsed=elapsed)
    assert report["status"] == ad.RUN_UNCERTAIN and expected in ids(report)


def test_pinned_start_still_advances_completion_by_monotonic_elapsed(tmp_path, monkeypatch):
    a = ready_admission(tmp_path)
    c = a["checks"]
    monkeypatch.setattr(ad, "inspect_git", lambda *_a: c["git"])
    monkeypatch.setattr(ad, "inspect_environment", lambda: c["preflight"])
    monkeypatch.setattr(ad, "inspect_disk", lambda *_a: c["disk"])
    monkeypatch.setattr(ad, "inspect_existing_lease", lambda *_a: c["lease"])
    ticks = iter([100.0, 105.0])
    ok = ad.collect_and_evaluate(ad.AdmissionInputs(**a["inputs"]), now_kst=NOW,
                                 process_probe=lambda *_a: c["processes"], monotonic=lambda: next(ticks))
    assert ok["status"] == ad.RUN_READY
    assert ok["observed_at_kst"] == (NOW + timedelta(seconds=5)).isoformat()
    late = NOW.replace(hour=15, minute=14, second=58)
    ticks = iter([100.0, 105.0])
    blocked = ad.collect_and_evaluate(ad.AdmissionInputs(**a["inputs"]), now_kst=late,
                                      process_probe=lambda *_a: c["processes"], monotonic=lambda: next(ticks))
    assert blocked["status"] == ad.RUN_BLOCKED and "target_window" in ids(blocked)


def test_admission_age_boundary_is_exclusive_at_sixty_seconds(tmp_path):
    a = ready_admission(tmp_path, observed=NOW)
    plan = rp.build_run_plan(a, expected_revision=REV, now_kst=NOW + timedelta(seconds=59, microseconds=999999))
    assert plan["created_at_kst"].startswith("2026-09-28T10:00:59")
    with pytest.raises(rp.FidReadRunPlanError, match="stale"):
        rp.build_run_plan(a, expected_revision=REV, now_kst=NOW + timedelta(seconds=ad.MAX_ADMISSION_AGE_SECONDS))


def test_plan_ttl_boundary_is_exclusive_at_expiry(tmp_path):
    a = ready_admission(tmp_path, observed=NOW)
    plan = rp.build_run_plan(a, expected_revision=REV, now_kst=NOW)
    end = NOW + timedelta(seconds=rp.PLAN_TTL_SECONDS)
    rp.validate_plan_integrity(plan, now_kst=end - timedelta(microseconds=1))
    with pytest.raises(rp.FidReadRunPlanError, match="expired"):
        rp.validate_plan_integrity(plan, now_kst=end)


def clock_class(box):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return box[0].astimezone(tz) if tz else box[0].replace(tzinfo=None)
    return Clock


def test_verify_withholds_command_when_wall_clock_regresses(tmp_path, monkeypatch):
    a = ready_admission(tmp_path, observed=NOW)
    plan = rp.build_run_plan(a, expected_revision=REV, now_kst=NOW)
    clock = [NOW + timedelta(seconds=20)]
    monkeypatch.setattr(rp, "datetime", clock_class(clock))

    def runner(_inputs):
        fresh = deepcopy(a)
        fresh["observation_started_at_kst"] = fresh["observed_at_kst"] = clock[0].isoformat()
        clock[0] -= timedelta(seconds=15)
        return fresh
    result = rp.verify_plan_for_manual_command(plan, trusted_expected_revision=REV,
                                               execution_approved_now=True, admission_runner=runner)
    assert result["status"] == "NOT_READY" and result["manual_command"] is None


def test_verify_withholds_command_when_monotonic_time_passes_expiry(tmp_path, monkeypatch):
    a = ready_admission(tmp_path, observed=NOW)
    plan = rp.build_run_plan(a, expected_revision=REV, now_kst=NOW)
    clock = [NOW + timedelta(seconds=20)]
    monkeypatch.setattr(rp, "datetime", clock_class(clock))  # 벽시계는 멈춰 있다.
    ticks = iter([0.0, 0.0])  # reader start, verification start; every later read is 400s later.
    fresh = deepcopy(a)
    fresh["observation_started_at_kst"] = fresh["observed_at_kst"] = clock[0].isoformat()
    try:
        result = rp.verify_plan_for_manual_command(
            plan, trusted_expected_revision=REV, execution_approved_now=True,
            admission_runner=lambda _i: fresh, monotonic=lambda: next(ticks, 400.0))
    except rp.FidReadRunPlanError:
        return
    assert result["status"] == "NOT_READY" and result["manual_command"] is None


def test_prepare_and_verify_cli_never_pin_the_start_instant(tmp_path, monkeypatch, capsys):
    import scripts.prepare_fid_read_ab_run as prepare
    import scripts.verify_fid_read_ab_run_plan as verify_cli

    seen = {}

    def admission_kwargs(_inputs, **kwargs):
        seen["prepare"] = kwargs
        return {"schema": ad.ADMISSION_SCHEMA, "status": ad.RUN_BLOCKED}
    monkeypatch.setattr(prepare, "collect_and_evaluate", admission_kwargs)
    root = tmp_path / "repo"
    prepare.main(["--repo-root", str(root), "--expected-revision", REV,
                  "--official-market-date", "2026-09-28", "--official-market-source-note", "KRX",
                  "--execution-approved",
                  "--output", str(root / "operations_state" / "fid_read_ab_run_plans" / "p.json")])
    assert seen["prepare"] == {}

    def verify_kwargs(_plan, **kwargs):
        seen["verify"] = kwargs
        return {"status": "NOT_READY", "manual_command": None}
    monkeypatch.setattr(verify_cli, "read_run_plan", lambda _p: {})
    monkeypatch.setattr(verify_cli, "verify_plan_for_manual_command", verify_kwargs)
    verify_cli.main(["--plan", str(tmp_path / "p.json"), "--expected-revision", REV, "--execution-approved"])
    assert "now_kst" not in seen["verify"]
    capsys.readouterr()


# --- C. builder/verifier 공통 READY 재검증 ------------------------------------------

def _drop_start(a):
    del a["observation_started_at_kst"]


MUTATIONS = {
    "missing_start": _drop_start,
    "float_bits": lambda a: a["checks"]["preflight"].__setitem__("python_bits", 32.0),
    "disk_flag_without_space": lambda a: a["checks"]["disk"].update(free_bytes=1, meets_code_minimum=True),
    "cli_internal_conflict": lambda a: a["checks"]["cli_contract"].__setitem__("nxt", True),
    "clean_flag_with_status": lambda a: a["checks"]["git"].__setitem__("status_lines", ["?? x"]),
    "truthy_approval": lambda a: a["inputs"].__setitem__("execution_approved", 1),
    "missing_process_list": lambda a: a["checks"]["processes"].pop("unidentified_python_processes"),
    "string_probe_ok": lambda a: a["checks"]["processes"].__setitem__("probe_ok", "true"),
    "lease_created": lambda a: a["checks"]["lease"].__setitem__("file_created", True),
    "market_mismatch": lambda a: a["checks"]["market_attestation"].__setitem__("source_note", "other"),
    "short_revision": lambda a: a["checks"]["git"].__setitem__("head", REV[:12]),
    "bool_index_stage": lambda a: a["checks"]["git"]["entrypoint"].__setitem__("index_stage", False),
    "float_hidden_count": lambda a: a["checks"]["git"].__setitem__("hidden_index_flag_count", 0.0),
    "missing_status_truncated": lambda a: a["checks"]["git"].pop("status_truncated"),
    "probe_error_with_ok": lambda a: a["checks"]["processes"].__setitem__("error", "probe failed"),
    "preflight_error_with_ready": lambda a: a["checks"]["preflight"].__setitem__("error", "registry"),
    "int_monotonic_elapsed": lambda a: a.__setitem__("observation_elapsed_monotonic_seconds", 10**400),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_builder_and_verifier_share_strict_ready_validation(tmp_path, name):
    a = ready_admission(tmp_path, observed=NOW)
    plan = rp.build_run_plan(a, expected_revision=REV, now_kst=NOW)
    bad = deepcopy(a)
    MUTATIONS[name](bad)
    with pytest.raises(rp.FidReadRunPlanError):
        rp.build_run_plan(bad, expected_revision=REV, now_kst=NOW)
    try:
        result = rp.verify_plan_for_manual_command(
            plan, trusted_expected_revision=REV, execution_approved_now=True,
            now_kst=NOW + timedelta(seconds=1), admission_runner=lambda _i: bad)
    except rp.FidReadRunPlanError:
        return
    assert result["status"] == "NOT_READY" and result["manual_command"] is None


def test_retimestamped_plan_with_recomputed_digest_is_still_rejected(tmp_path):
    a = ready_admission(tmp_path, observed=NOW)
    plan = rp.build_run_plan(a, expected_revision=REV, now_kst=NOW)
    later = NOW + timedelta(minutes=10)
    plan["created_at_kst"] = later.isoformat()
    plan["expires_at_kst"] = (later + timedelta(seconds=rp.PLAN_TTL_SECONDS)).isoformat()
    plan["admission_sha256"] = rp.admission_sha256(plan["admission"])  # digest는 서명이 아니다.
    with pytest.raises(rp.FidReadRunPlanError, match="not valid at plan creation"):
        rp.validate_plan_integrity(plan, now_kst=later)


def test_executable_path_case_difference_is_not_a_command_change(tmp_path):
    a = ready_admission(tmp_path, observed=NOW)
    plan = rp.build_run_plan(a, expected_revision=REV, now_kst=NOW)
    fresh = deepcopy(a)
    fresh["checks"]["preflight"]["executable"] = a["checks"]["preflight"]["executable"].upper()
    result = rp.verify_plan_for_manual_command(
        plan, trusted_expected_revision=REV, execution_approved_now=True,
        now_kst=NOW + timedelta(seconds=1), admission_runner=lambda _i: fresh)
    assert result["status"] == "MANUAL_COMMAND_READY"
    tampered = deepcopy(plan)
    tampered["manual_command"][-1] = "91"  # arguments stay case/exact sensitive.
    with pytest.raises(rp.FidReadRunPlanError, match="manual command changed"):
        rp.validate_plan_integrity(tampered, now_kst=NOW)


def test_plan_embeds_a_copy_of_the_admission(tmp_path):
    a = ready_admission(tmp_path, observed=NOW)
    plan = rp.build_run_plan(a, expected_revision=REV, now_kst=NOW)
    a["status"] = ad.RUN_BLOCKED
    rp.validate_plan_integrity(plan, now_kst=NOW)
    assert plan["admission"]["status"] == ad.RUN_READY


# --- D. Git identity와 실행 경로 (임시 fixture 저장소만) ----------------------------

GIT_ENV = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, text=True,
                          capture_output=True, timeout=20).stdout.strip()


def fixture_repo(tmp_path) -> Path:
    root = tmp_path / "origin"
    entry = root / "collector" / "kiwoom" / "kiwoom_universe_logger.py"
    entry.parent.mkdir(parents=True)
    entry.write_bytes(b"# fixture entrypoint; never executed\n")
    (root / ".gitignore").write_bytes(b"ignored/\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgSign=false", "commit", "-qm", "fixture")
    return root.resolve()


def evaluate_git(tmp_path, root, observed):
    a = ready_admission(tmp_path / "facts")
    a["inputs"]["repo_root"] = str(root)
    a["inputs"]["expected_revision"] = observed["head"]
    return evaluate(a, git=observed)


def test_sibling_worktree_with_tracked_entrypoint_is_ready(tmp_path, monkeypatch):
    root = fixture_repo(tmp_path)
    sibling = (tmp_path / "sibling").resolve()
    git(root, "worktree", "add", "-q", "--detach", str(sibling), "HEAD")
    monkeypatch.setattr(ad, "checker_source_root", lambda: sibling)
    observed = ad.inspect_git(sibling)
    assert observed["toplevel"] == str(sibling)
    assert observed["entrypoint"]["worktree_blob"] == observed["entrypoint"]["head_blob"]
    assert evaluate_git(tmp_path, sibling, observed)["status"] == ad.RUN_READY


def test_skip_worktree_edit_hidden_from_status_is_blocked(tmp_path, monkeypatch):
    root = fixture_repo(tmp_path)
    monkeypatch.setattr(ad, "checker_source_root", lambda: root)
    git(root, "update-index", "--skip-worktree", "collector/kiwoom/kiwoom_universe_logger.py")
    (root / "collector" / "kiwoom" / "kiwoom_universe_logger.py").write_bytes(b"# edited\n")
    observed = ad.inspect_git(root)
    assert observed["clean"] is True  # status alone would have accepted this tree.
    report = evaluate_git(tmp_path, root, observed)
    assert report["status"] == ad.RUN_BLOCKED
    assert {"collector_entrypoint", "index_flags"} <= ids(report)


def test_untracked_entrypoint_and_foreign_checker_source_are_blocked(tmp_path):
    root = tmp_path / "plain"
    (root / "collector" / "kiwoom").mkdir(parents=True)
    (root / "marker.txt").write_bytes(b"tracked\n")
    git(root, "init", "-q")
    git(root, "add", "marker.txt")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgSign=false", "commit", "-qm", "fixture")
    (root / ".git" / "info" / "exclude").write_bytes(b"collector/\n")
    (root / "collector" / "kiwoom" / "kiwoom_universe_logger.py").write_bytes(b"# never executed\n")
    observed = ad.inspect_git(root.resolve())  # checker code lives in the test checkout.
    report = evaluate_git(tmp_path, root.resolve(), observed)
    assert {"collector_entrypoint", "checker_source"} <= ids(report) & GIT_IDS


# --- F. PowerShell 표시 --------------------------------------------------------------

def test_powershell_display_runs_harmless_dummy_with_exact_arguments():
    args = ["a b", "o'hare", "x\u2019y", "$env:PATH", "--flag"]
    command = rp.powershell_command([sys.executable, "-c", "import sys; print(ascii(sys.argv[1:]))", *args])
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == ascii(args)


@pytest.mark.parametrize("token", ['C:/a"b.exe', "C:/a\nb.exe", ""])
def test_powershell_display_rejects_unsafe_tokens(token):
    with pytest.raises(rp.FidReadRunPlanError):
        rp.powershell_command(["C:/python.exe", token])


# --- G. bounded reader ---------------------------------------------------------------

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("content,limits", [
    (b'{"a": 1}\n{"a": 2}', dict(max_bytes=10**6, max_lines=10)),  # 잘린 마지막 줄
    (b'{"a": NaN}\n', dict(max_bytes=10**6, max_lines=10)),
    (b'{"a": 1, "a": 2}\n', dict(max_bytes=10**6, max_lines=10)),
    (b'{"a": "' + b"x" * (an.MAX_JSONL_LINE_BYTES + 1) + b'"}\n', dict(max_bytes=10**7, max_lines=10)),
], ids=["truncated_last_line", "nan_constant", "duplicate_key", "line_too_long"])
def test_jsonl_reader_rejects_truncated_nonfinite_duplicate_or_long_lines(tmp_path, content, limits):
    path = tmp_path / "rows.jsonl"
    path.write_bytes(content)
    before = digest(path)
    with pytest.raises(an.FidReadAnalysisError):
        an._read_jsonl(path, required=True, **limits)
    assert digest(path) == before


def test_jsonl_cumulative_bound_uses_bytes_read_not_stat(tmp_path, monkeypatch):
    path = (tmp_path / "rows.jsonl").resolve()
    path.write_bytes(b'{"padding": "xxxxxxxxxxxxxxxxxxxx"}\n' * 5)
    original = Path.stat

    def stale(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if self == path:
            fields = list(result)
            fields[6] = 0
            return os.stat_result(fields)
        return result
    monkeypatch.setattr(Path, "stat", stale)
    with pytest.raises(an.FidReadAnalysisError, match="bytes read"):
        an._read_jsonl(path, max_bytes=100, max_lines=10, required=True)


def test_jsonl_reader_accepts_windows_text_mode_lines(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_bytes(b'{"a": 1}\r\n{"a": 2}\r\n')
    assert an._read_jsonl(path, max_bytes=100, max_lines=10, required=True) == [{"a": 1}, {"a": 2}]


@pytest.mark.parametrize("text", ['{"schema": "fid_read_ab_run_plan_v1", "x": NaN}',
                                  '{"schema": "fid_read_ab_run_plan_v1", "schema": "fid_read_ab_run_plan_v1"}'])
def test_run_plan_reader_rejects_nonfinite_and_duplicate_keys(tmp_path, text):
    path = tmp_path / "plan.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(rp.FidReadRunPlanError):
        rp.read_run_plan(path)


# --- H. 완료 근거 ---------------------------------------------------------------------

def _status_mutation(status, name):
    snapshot = status["snapshot"]
    if name == "identity_not_utc":
        status["identity"]["started_at_utc"] = "2026-09-28T09:59:00+09:00"
    elif name == "short_revision":
        status["identity"]["code_revision"] = "b40f8c3"
    elif name == "observed_not_utc":
        status["observed_at_utc"] = "2026-09-28T10:00:00"
    elif name == "last_commit_not_utc":
        snapshot["last_commit_at_utc"] = "2026-09-28T01:00:00"
    elif name == "close_before_last_event":
        snapshot["finalization"]["close_ns"] = snapshot["last_event_ns"]
    elif name == "non_string_nested_error":
        snapshot["error"] = {"code": 1}
    elif name == "bool_accepted_count":
        snapshot["dropped_callbacks"] = False


@pytest.mark.parametrize("name,issue", [
    ("identity_not_utc", "process_identity"),
    ("short_revision", "code_revision"),
    ("observed_not_utc", "status_observed_at_utc"),
    ("last_commit_not_utc", "last_commit_at_utc"),
    ("close_before_last_event", "finalization_close_ns"),
    ("non_string_nested_error", "snapshot_error_type"),
    ("bool_accepted_count", "snapshot_counters"),
])
def test_malformed_completion_evidence_is_not_analysis_ready(tmp_path, name, issue):
    session, raw = make_session(tmp_path)
    status = json.loads((session / "status.json").read_text(encoding="utf-8"))
    _status_mutation(status, name)
    _write_json(session / "status.json", status)
    report = an.analyze_fid_read_ab_session(session)
    assert report["result"] != an.RESULT_READY
    assert issue in {item["id"] for item in report["issues"]}
    assert not raw.exists()


@pytest.mark.parametrize("field,value", [("fid_read_ns", -5), ("processing_ns", -1), ("fid_call_count", True)])
def test_invalid_telemetry_sample_value_is_evidence_error(tmp_path, field, value):
    session, _ = make_session(tmp_path)
    path = session / "capture_telemetry.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[1]["samples"][0][field] = value
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    report = an.analyze_fid_read_ab_session(session)
    assert report["result"] == an.RESULT_INVALID
    assert "telemetry_metric_value" in {item["id"] for item in report["issues"]}


@pytest.mark.parametrize("name", ["snapshot_null", "a2_meta_null", "huge_int_metric", "overflow_float"])
def test_malformed_session_is_reported_not_crashed(tmp_path, capsys, name):
    from scripts.assess_fid_read_ab import main
    session, _ = make_session(tmp_path)
    if name in ("snapshot_null", "a2_meta_null"):
        target = session / ("status.json" if name == "snapshot_null" else "fid_read_ab_test.json")
        value = json.loads(target.read_text(encoding="utf-8"))
        if name == "snapshot_null":
            value["snapshot"] = None
        else:
            value["phases"]["A2_FULL"] = None
        _write_json(target, value)
    else:
        path = session / "capture_telemetry.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines]
        rows[1]["samples"][0]["fid_read_ns"] = 10**400 if name == "huge_int_metric" else 1.5
        text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        if name == "overflow_float":
            text = text.replace('"fid_read_ns": 1.5', '"fid_read_ns": 1e999', 1)
        path.write_text(text, encoding="utf-8")
    code = main(["--session-dir", str(session)])
    captured = capsys.readouterr()
    if code == 0:
        payload = json.loads(captured.out)
        if name != "huge_int_metric":
            assert payload["analysis"]["result"] != an.RESULT_READY
    else:
        assert json.loads(captured.err)["status"] == "unavailable"


def test_analysis_ready_does_not_claim_quality_or_process_exit(tmp_path):
    session, _ = make_session(tmp_path)
    report = an.analyze_fid_read_ab_session(session)
    assert report["result"] == an.RESULT_READY
    assert any("not a raw quality pass" in note for note in report["notes"])
    assert report["diagnostic"]["research_eligible"] is False


# --- I. assessment ------------------------------------------------------------------

@pytest.mark.parametrize("summary", [
    {"count": True, "min": 1, "median": 1, "max": 1},
    {"count": "3", "min": 1, "median": 1, "max": 1},
    {"count": 3, "min": 5, "median": 1, "max": 9},
    {"count": 3, "min": 1, "median": 1, "max": float("inf")},
    {"count": 0, "min": None, "median": 5, "max": None},
    None,
])
def test_malformed_phase_summary_is_invalid_not_directional(summary):
    report = analysis_fixture()
    typed = report["telemetry"]["by_phase_real_type"]["B_ESSENTIAL"]["주식체결"]
    if summary is None:
        del typed["fid_read_ns"]
    else:
        typed["fid_read_ns"] = summary
    result = assess.assess_fid_read_ab(report)
    assert result["assessment"] == assess.OVERALL_NOT_ASSESSABLE
    assert result["primary_by_real_type"]["주식체결"]["pattern"] == assess.PATTERN_INVALID
    assert result["metric_evidence_errors"]


def test_empty_summary_stays_insufficient_and_preregistration_is_unchanged():
    report = analysis_fixture()
    report["telemetry"]["by_phase_real_type"]["B_ESSENTIAL"]["주식호가잔량"]["fid_read_ns"] = metric(0, None)
    result = assess.assess_fid_read_ab(report)
    assert result["primary_by_real_type"]["주식호가잔량"]["pattern"] == assess.PATTERN_INSUFFICIENT
    assert assess.MIN_PHASE_METRIC_SAMPLES == 3
    clean = assess.assess_fid_read_ab(analysis_fixture())
    assert clean["interpretation_contract"]["effect_size_threshold"] is None
    assert clean["interpretation_contract"]["p_value_or_significance_test"] is None
    assert clean["interpretation_contract"]["trade_and_quote_are_never_pooled"] is True


def test_ready_label_with_issues_is_not_assessed():
    report = analysis_fixture()
    report["issues"] = [{"severity": "invalid", "id": "fixture"}]
    result = assess.assess_fid_read_ab(report)
    assert result["assessment"] == assess.OVERALL_NOT_ASSESSABLE
    assert result["blocked_by_analysis_result"] is True
