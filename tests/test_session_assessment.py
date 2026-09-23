"""Pure control-tower evidence reducer tests; no process/OCX/raw access."""
from dataclasses import replace

import pytest

from control_tower.session_assessment import SessionAssessment, assess_session


def raw(state="closed"):
    return {
        "status": "stale",
        "payload": {
            "snapshot": {
                "state": state,
                "queued": 0,
                "in_flight": 0,
                "pending_callbacks": 0,
            }
        },
    }


def collector(status="stale"):
    return {"status": status, "heartbeat": None}


def test_closed_storage_does_not_prove_process_or_native_exit():
    view = assess_session(raw(), collector())
    assert view.storage == "closed"
    assert view.process == view.native_ui == view.lease == "unverified"
    assert view.termination == "unverified"


def test_free_lease_is_not_process_termination_evidence():
    view = assess_session(raw(), collector(), process="alive",
                          native_ui="runtime_error", lease="free")
    assert view.storage == "closed" and view.lease == "free"
    assert view.termination == "residual_native"


def test_verified_exit_requires_process_absence_and_native_clear():
    view = assess_session(raw(), collector(), process="absent",
                          native_ui="clear", lease="free")
    assert view.termination == "verified_exited"
    assert replace(view, native_ui="unverified").termination == "process_absent_native_unverified"


def test_queue_zero_or_recent_log_does_not_prove_source_freshness():
    stale = assess_session(raw("running"), collector("stale"), process="alive")
    assert stale.storage == "active"
    assert stale.activity == "stale"
    assert stale.source_freshness == "unverified"
    assert stale.termination == "process_alive"

    recent = assess_session(raw("running"), collector("recent"), process="alive")
    assert recent.activity == "recent"
    assert recent.source_freshness == "unverified"

    missing = assess_session(raw("running"), {"status": "no_heartbeat"}, process="alive")
    assert missing.activity == "unverified"
    assert missing.source_freshness == "unverified"


def test_research_eligibility_is_explicit_and_independent():
    diagnostic = assess_session(raw(), collector(), process="absent", native_ui="clear",
                                research="diagnostic_only")
    assert diagnostic.storage == "closed"
    assert diagnostic.termination == "verified_exited"
    assert diagnostic.research == "diagnostic_only"

    eligible = replace(diagnostic, research="eligible")
    assert eligible.research == "eligible"
    assert eligible.storage == diagnostic.storage


@pytest.mark.parametrize("field,value", [
    ("storage", "healthy"),
    ("process", "dead"),
    ("native_ui", "none"),
    ("lease", "released"),
    ("activity", "healthy"),
    ("source_freshness", "recent"),
    ("research", "passed"),
])
def test_unknown_axis_vocabulary_is_rejected(field, value):
    with pytest.raises(ValueError):
        replace(SessionAssessment(), **{field: value})


def test_describe_keeps_axes_separate():
    view = SessionAssessment(storage="closed", process="alive",
                             native_ui="runtime_error", lease="free",
                             activity="stale", source_freshness="lagging", research="unverified")
    assert view.describe() == {
        "storage": "closed",
        "process": "alive",
        "native_ui": "runtime_error",
        "lease": "free",
        "activity": "stale",
        "source_freshness": "lagging",
        "research": "unverified",
        "termination": "residual_native",
    }
