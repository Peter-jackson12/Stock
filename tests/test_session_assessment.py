"""Pure evidence and bounded temporary-status regressions; no OS/OCX/raw DB."""
import copy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from control_tower.lifecycle import ProcessIdentity
from control_tower.session_assessment import (
    DIAGNOSTIC_SCOPE, RuntimeObservation, SessionAssessment, assess_session,
)
from control_tower.status import observe_collector, observe_raw_capture

NOW = datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc)
IDENTITY = ProcessIdentity("fixture-a", 123, "2026-09-23T00:00:00Z",
    "C:/fixture/python.exe", "fixture-revision", 32, "fixture", "synthetic", "C:/fixture/a.db")


def raw(state="closed", *, age=0, identity=IDENTITY):
    payload = dict(status_schema="raw_capture_status_v1", control_heartbeat=False,
        observed_at_utc=(NOW - timedelta(seconds=age)).isoformat(), identity=asdict(identity), error=None,
        snapshot=dict(session_id=identity.session_id, dataset_path=identity.dataset_path,
            feed_scope=identity.feed_scope, state=state, error=None,
            accepted_callbacks=3, committed_callbacks=3, committed_seq=4,
            queued=0, in_flight=0, pending_callbacks=0, dropped_callbacks=0,
            writer_closed=state == "closed", finalization=(dict(final_seq=4, close_ns=100,
                payload_sha256="a" * 64) if state == "closed" else None)))
    return dict(status="recent", payload=payload)


def runtime(*, age=0, identity=IDENTITY, **axes):
    return RuntimeObservation(identity, (NOW - timedelta(seconds=age)).isoformat(), **axes)


def test_closed_status_does_not_prove_current_process_or_research():
    view = assess_session(raw(age=1000), now=NOW)
    assert view.storage == "closed" and view.status_recency == "stale"
    assert view.process == view.native_ui == view.lease == "unverified"
    assert view.activity == view.source_freshness == view.research == "unverified"
    assert view.termination == "unverified"


@pytest.mark.parametrize("state", ["starting", "running", "draining"])
def test_stale_active_report_is_retained_but_not_current(state):
    view = assess_session(raw(state, age=31), now=NOW)
    assert view.storage == "unverified" and view.reported_storage == state
    assert "stale_active_status" in view.issues


@pytest.mark.parametrize("state", ["closed", "running"])
def test_future_status_cannot_certify_storage(state):
    view = assess_session(raw(state, age=-6), now=NOW)
    assert view.status_recency == "clock_ahead" and view.storage == "unverified"


@pytest.mark.parametrize("label", ["recent", "stale", "clock_ahead", "no_heartbeat"])
def test_daily_log_is_not_session_activity_or_source_freshness(label):
    log = dict(status=label, heartbeat=dict(trades=999, quotes=999, counts_kind="callbacks"))
    view = assess_session(raw("running"), log, now=NOW)
    assert view.activity == view.source_freshness == "unverified"
    assert view.log_recency == (label if label != "no_heartbeat" else "unverified")


def test_unchanged_counters_and_cross_session_log_do_not_certify_callbacks(tmp_path):
    path = tmp_path / "logs/kiwoom_universe_20260923.log"
    path.parent.mkdir()
    path.write_text("2026-09-23 14:59:00 수신 콜백 체결: 7건 | 호가: 8건 (대기큐: 0)\n"
                    "2026-09-23 15:00:00 수신 콜백 체결: 7건 | 호가: 8건 (대기큐: 0)\n", encoding="utf-8")
    log = observe_collector(tmp_path, now=NOW)
    assert log["status"] == "recent"
    for identity in (IDENTITY, replace(IDENTITY, session_id="other-session")):
        view = assess_session(raw(identity=identity), log, now=NOW)
        assert view.activity == "unverified" and view.source_freshness == "unverified"


@pytest.mark.parametrize("lease", ["held", "free", "unverified"])
def test_ordinary_live_ocx_window_is_not_a_shutdown_failure(lease):
    view = assess_session(raw("running"), runtime_observation=runtime(
        process="alive", native_ui="ocx_window_present", lease=lease), now=NOW)
    assert view.termination == "process_alive"


def test_closed_storage_free_lease_and_live_runtime_window_is_residual():
    view = assess_session(raw(), runtime_observation=runtime(
        process="alive", native_ui="runtime_error", lease="free"), now=NOW)
    assert view.storage == "closed" and view.termination == "residual_native"


def test_active_runtime_error_is_not_mislabeled_as_teardown():
    view = assess_session(raw("running"), runtime_observation=runtime(
        process="alive", native_ui="runtime_error"), now=NOW)
    assert view.termination == "native_error"


@pytest.mark.parametrize("lease", ["held", "free", "unverified"])
def test_termination_uses_current_process_and_window_evidence_not_lease(lease):
    view = assess_session(raw(), runtime_observation=runtime(
        process="absent", native_ui="clear", lease=lease), now=NOW)
    assert view.termination == "exit_observed"
    assert "can_start" not in view.describe()


@pytest.mark.parametrize("age", [-1, 31, 3600])
def test_old_or_future_runtime_evidence_is_not_current(age):
    view = assess_session(raw(), runtime_observation=runtime(age=age,
        process="absent", native_ui="clear", lease="free"), now=NOW)
    assert view.termination == view.process == view.lease == "unverified"
    assert "runtime_not_current" in view.issues


@pytest.mark.parametrize("change", [dict(session_id="other"), dict(pid=456),
    dict(started_at_utc="2026-09-23T00:01:00Z"), dict(executable="C:/other/python.exe"),
    dict(code_revision="other"), dict(server="mock"), dict(dataset_path="C:/fixture/b.db"),
    dict(python_bits=64), dict(feed_scope="other")])
def test_runtime_identity_must_match_every_field(change):
    view = assess_session(raw(), runtime_observation=runtime(identity=replace(IDENTITY, **change),
        process="absent", native_ui="clear"), now=NOW)
    assert view.termination == "unverified" and "runtime_identity_mismatch" in view.issues


@pytest.mark.parametrize("axes,expected", [
    (dict(process="access_denied", native_ui="clear"), "unverified"),
    (dict(process="absent", native_ui="unverified"), "process_absent_native_unverified"),
    (dict(process="absent", native_ui="runtime_error"), "contradictory"),
])
def test_missing_or_conflicting_runtime_evidence(axes, expected):
    assert assess_session(raw(), runtime_observation=runtime(**axes), now=NOW).termination == expected


def test_runtime_without_session_identity_is_unverified():
    view = assess_session(runtime_observation=runtime(process="absent", native_ui="clear"), now=NOW)
    assert view.termination == "unverified"


@pytest.mark.parametrize("age", [0, 3600])
def test_diagnostic_scope_is_never_promoted_to_research(age):
    view = assess_session(raw(age=age, identity=replace(IDENTITY, feed_scope=DIAGNOSTIC_SCOPE)), now=NOW)
    assert view.research == "diagnostic_only"
    with pytest.raises(ValueError):
        replace(view, research="eligible")


@pytest.mark.parametrize("mutation", ["schema", "identity", "counter", "final", "type", "envelope"])
def test_unvalidated_or_malformed_payload_is_fail_closed(mutation):
    value = raw()
    if mutation == "schema": value["payload"]["status_schema"] = "future"
    if mutation == "identity": value["payload"]["snapshot"]["session_id"] = "wrong"
    if mutation == "counter": value["payload"]["snapshot"]["committed_callbacks"] = 4
    if mutation == "final": value["payload"]["snapshot"]["finalization"]["final_seq"] = 9
    if mutation == "type": value["payload"]["snapshot"]["accepted_callbacks"] = True
    if mutation == "envelope": value["status"] = "unavailable"
    view = assess_session(value, now=NOW)
    assert view.storage == "unverified" and view.session_id is None
    assert view.issues[0].startswith("invalid_status:")


def test_status_error_does_not_become_clean_storage():
    value = raw()
    value["payload"]["error"] = "late diagnostic failure"
    view = assess_session(value, now=NOW)
    assert view.storage == "unverified" and view.reported_storage == "closed"
    assert "status_contains_error" in view.issues


@pytest.mark.parametrize("value", [[], {}, None, True, 1, "healthy"])
def test_invalid_enum_raises_value_error_not_type_error(value):
    with pytest.raises(ValueError):
        SessionAssessment(process=value)


def test_missing_observations_and_input_immutability():
    value = raw()
    before = copy.deepcopy(value)
    assert assess_session(value, now=NOW).session_id == IDENTITY.session_id
    assert value == before
    assert assess_session(now=NOW).storage == "unverified"
    with pytest.raises(ValueError):
        assess_session(now=datetime(2026, 9, 23))


def test_bounded_status_reader_and_reducer_share_validation(tmp_path):
    path = tmp_path / "operations_state/capture_status.json"
    path.parent.mkdir()
    data = json.dumps(raw()["payload"]).encode()
    path.write_bytes(data)
    view = assess_session(observe_raw_capture(tmp_path, now=NOW), now=NOW)
    assert view.storage == "closed" and path.read_bytes() == data
    path.write_bytes(b"x" * 65537)
    assert observe_raw_capture(tmp_path, now=NOW)["status"] == "unavailable"
