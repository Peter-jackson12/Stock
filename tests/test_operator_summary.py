"""Operator summary는 저장 근거를 현재 정상/중지 판정으로 승격하지 않는다."""
from copy import deepcopy

import pytest

from control_tower.operator_summary import summarize_operator_state


def test_recent_evidence_stays_unverified():
    observation = {"status": "recent", "heartbeat": {"trades": 1}}
    raw = {"status": "recent", "payload": {"snapshot": {"state": "running"}}}
    result = summarize_operator_state(observation, raw)
    assert result["raw_evidence"] == "최근"
    assert result["heartbeat_evidence"] == "최근"
    assert result["collector_now"] == "미확인"
    assert result["producer_state"] == "running"
    assert result["tone"] == "info"
    assert "인증이 아닙니다" in result["guidance"]
    assert result["execution_approved"] is False
    assert result["data_quality"] == "미확인"


def test_stale_closed_does_not_become_stopped():
    result = summarize_operator_state(
        {"status": "unavailable"},
        {"status": "stale", "payload": {"snapshot": {"state": "closed"}}},
    )
    assert result["producer_state"] == "closed"
    assert result["collector_now"] == "미확인"
    assert result["tone"] == "warning"
    assert "오래" in result["headline"]
    assert "중지됐는지는 미확인" in result["guidance"]


@pytest.mark.parametrize("state", ["failed", "interrupted"])
def test_failure_claim_is_prominent_without_claiming_current_liveness(state):
    result = summarize_operator_state(
        {"status": "recent"},
        {"status": "recent", "payload": {"snapshot": {"state": state}}},
    )
    assert result["tone"] == "error"
    assert state == result["producer_state"]
    assert result["collector_now"] == "미확인"
    assert "현재 실행 여부" in result["guidance"]


@pytest.mark.parametrize(
    "observation_status,raw_status",
    [("clock_ahead", "recent"), ("recent", "clock_ahead")],
)
def test_clock_ahead_has_priority(observation_status, raw_status):
    result = summarize_operator_state(
        {"status": observation_status},
        {"status": raw_status},
    )
    assert result["tone"] == "warning"
    assert "시각" in result["headline"]
    assert result["collector_now"] == "미확인"


@pytest.mark.parametrize(
    "observation_status,raw_status",
    [("unavailable", "unavailable"), ("no_heartbeat", "unavailable")],
)
def test_missing_evidence_is_not_stopped(observation_status, raw_status):
    result = summarize_operator_state(
        {"status": observation_status},
        {"status": raw_status},
    )
    assert result["raw_evidence"] == "없음"
    assert result["heartbeat_evidence"] == "없음"
    assert result["collector_now"] == "미확인"
    assert "중지로 단정하지" in result["guidance"]


def test_unexpected_payload_is_safe_and_inputs_are_not_mutated():
    observation = {"status": "mystery", "recent_messages": ["x"]}
    raw = {"status": "mystery", "payload": ["not", "a", "dict"]}
    before = (deepcopy(observation), deepcopy(raw))
    result = summarize_operator_state(observation, raw)
    assert result["raw_evidence"] == "미확인"
    assert result["heartbeat_evidence"] == "미확인"
    assert result["producer_state"] is None
    assert (observation, raw) == before
