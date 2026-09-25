"""Synthetic tests for disconnected Operator collection Run Plans."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from control_tower.collector_run_plan import (
    CollectionRunPlanStore,
    build_collection_run_plan,
    inspect_run_target,
)


def preflight(*, free_bytes=8 * 1024**3, local_status="PASS"):
    return {
        "schema": "operator_collector_preflight_v1",
        "status": "UNVERIFIED",
        "local_status": local_status,
        "observed_at_local": "2026-09-25T09:00:00+09:00",
        "storage_free_bytes": free_bytes,
        "checks": [
            {
                "id": "collector_runtime",
                "label": "collector 런타임",
                "status": local_status,
                "next_check": "실행 직전 다시 확인",
            },
            {
                "id": "market_session",
                "label": "실제 시장 날짜·장 구간",
                "status": "UNVERIFIED",
                "next_check": "공식 출처 확인",
            },
        ],
    }


def target(*, clean=True):
    return {
        "probe_ok": True,
        "revision": "a" * 40,
        "branch": "feat/operator-plan",
        "clean": clean,
        "dirty_entry_count": 0 if clean else 2,
    }


def make_plan(**changes):
    values = {
        "codes": ["005930"],
        "duration_seconds": 60,
        "server": "mock",
        "required_storage_mib": 512,
        "planned_market_date": "2026-09-28",
        "planned_market_segment": "승인된 구간 대조 필요",
        "preflight": preflight(),
        "run_target": target(),
    }
    values.update(changes)
    return build_collection_run_plan(**values)


def by_id(plan):
    return {item["id"]: item for item in plan["checks"]}


def test_plan_separates_observed_inputs_from_market_and_execution_approval():
    plan = make_plan()
    checks = by_id(plan)
    assert plan["status"] == "UNVERIFIED"
    assert checks["plan_parameters"]["status"] == "PASS"
    assert checks["required_storage"]["status"] == "PASS"
    assert checks["run_target"]["status"] == "PASS"
    assert checks["preflight_summary"]["status"] == "PASS"
    assert checks["market_session"]["status"] == "UNVERIFIED"
    assert checks["execution_approval"]["status"] == "UNVERIFIED"
    assert plan["state"] == "review_only"
    assert plan["execution_approved"] is False
    assert plan["launch_connected"] is False
    assert plan["managed_capture_activated"] is False
    assert plan["inputs"]["planned_market_date"] == "2026-09-28"


def test_missing_estimate_dirty_tree_and_insufficient_space_remain_distinct():
    missing = by_id(make_plan(required_storage_mib=None, run_target=target(clean=False)))
    assert missing["required_storage"]["status"] == "UNVERIFIED"
    assert missing["run_target"]["status"] == "WARN"

    blocked = make_plan(required_storage_mib=512, preflight=preflight(free_bytes=128 * 1024**2))
    assert blocked["status"] == "BLOCKED"
    assert by_id(blocked)["required_storage"]["status"] == "BLOCKED"


@pytest.mark.parametrize("changes", [
    {"codes": ["005930", "005930"]},
    {"codes": ["bad"]},
    {"duration_seconds": 301},
    {"server": "auto"},
    {"required_storage_mib": 0},
])
def test_plan_reuses_managed_capture_input_contract_and_bounds(changes):
    with pytest.raises(ValueError):
        make_plan(**changes)


def test_store_is_immutable_review_only_and_never_creates_an_executable_job(tmp_path):
    store = CollectionRunPlanStore(tmp_path)
    assert store.recent() == []
    assert not store.path.exists()
    plan = make_plan()
    plan_id = store.save(plan)
    plan["inputs"]["codes"] = ["000000"]
    saved = store.recent()[0]
    assert saved["id"] == plan_id
    assert saved["payload"]["inputs"]["codes"] == ["005930"]
    assert saved["payload"]["execution_approved"] is False
    assert not (tmp_path / "operations_state/jobs.sqlite3").exists()
    assert not (tmp_path / "operations_state/managed_captures.sqlite3").exists()

    activated = deepcopy(saved["payload"])
    activated["execution_approved"] = True
    with pytest.raises(ValueError):
        store.save(activated)


def test_git_target_probe_reads_revision_branch_and_dirty_count_without_writes(tmp_path):
    outputs = iter([
        SimpleNamespace(returncode=0, stdout="b" * 40 + "\n", stderr=""),
        SimpleNamespace(returncode=0, stdout=" M dashboard/operations.py\n?? fixture.txt\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="feat/fixture\n", stderr=""),
    ])
    commands = []

    def runner(command, **kwargs):
        commands.append((command, kwargs))
        return next(outputs)

    report = inspect_run_target(Path(tmp_path), runner=runner)
    assert report == {
        "probe_ok": True,
        "revision": "b" * 40,
        "branch": "feat/fixture",
        "clean": False,
        "dirty_entry_count": 2,
    }
    assert [command[3] for command, _ in commands] == ["rev-parse", "status", "symbolic-ref"]
    assert all(kwargs["check"] is False and kwargs["timeout"] == 5.0 for _, kwargs in commands)
    assert list(tmp_path.iterdir()) == []


def test_git_target_probe_failure_is_unverified_input():
    result = inspect_run_target(
        Path("C:/fixture"),
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=128, stdout="", stderr="not a repo"),
    )
    assert result["probe_ok"] is False
    assert "not a repo" in result["error"]
