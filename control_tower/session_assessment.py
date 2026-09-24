"""Read-only presentation of independent, identity-scoped evidence.

No OS scan, lease probe, raw-file access, qualification or start permission.
Daily log recency is not callback progress and cannot identify a session.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from control_tower.lifecycle import ProcessIdentity
from control_tower.status import validate_raw_capture_payload

STORAGE_STATES = {"unverified", "active", "draining", "closed", "interrupted", "failed"}
PROCESS_STATES = {"unverified", "alive", "absent", "access_denied", "mismatch"}
NATIVE_UI_STATES = {"unverified", "clear", "runtime_error", "ocx_window_present"}
LEASE_STATES = {"unverified", "held", "free"}
RECENCY_STATES = {"unverified", "recent", "stale", "clock_ahead"}
ACTIVITY_STATES = {"unverified", "recent", "stale", "stopped"}
SOURCE_FRESHNESS_STATES = {"unverified", "fresh", "lagging"}
RESEARCH_STATES = {"unverified", "diagnostic_only", "ineligible"}
DIAGNOSTIC_SCOPE = "kiwoom_universe_fid_read_diagnostic"
RUNTIME_MAX_AGE_SECONDS = 30


def _choice(value, name, allowed):
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"invalid {name}: {value!r}")
    return value


def _utc(value):
    if not isinstance(value, str):
        raise ValueError("explicit UTC observation required")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() != timedelta(0):
        raise ValueError("explicit UTC observation required")
    return stamp


@dataclass(frozen=True)
class RuntimeObservation:
    """Caller-supplied observation, not an OS query or an authentication token.

    The caller must have observed all supplied axes for this process identity.
    The reducer checks identity and recency; it cannot authenticate the caller.
    """
    identity: ProcessIdentity
    observed_at_utc: str
    process: str = "unverified"
    native_ui: str = "unverified"
    lease: str = "unverified"

    def __post_init__(self):
        if not isinstance(self.identity, ProcessIdentity):
            raise ValueError("full process/session identity required")
        _utc(self.observed_at_utc)
        _choice(self.process, "process", PROCESS_STATES)
        _choice(self.native_ui, "native UI", NATIVE_UI_STATES)
        _choice(self.lease, "lease", LEASE_STATES)


@dataclass(frozen=True)
class SessionAssessment:
    """Display model only. None of its fields authorizes a control action."""
    storage: str = "unverified"
    process: str = "unverified"
    native_ui: str = "unverified"
    lease: str = "unverified"
    activity: str = "unverified"
    source_freshness: str = "unverified"
    research: str = "unverified"
    status_recency: str = "unverified"
    log_recency: str = "unverified"
    session_id: str | None = None
    reported_at_utc: str | None = None
    reported_storage: str | None = None
    runtime_observed_at_utc: str | None = None
    issues: tuple[str, ...] = ()

    def __post_init__(self):
        for name, allowed in (("storage", STORAGE_STATES), ("process", PROCESS_STATES),
                ("native_ui", NATIVE_UI_STATES), ("lease", LEASE_STATES),
                ("activity", ACTIVITY_STATES), ("source_freshness", SOURCE_FRESHNESS_STATES),
                ("research", RESEARCH_STATES), ("status_recency", RECENCY_STATES),
                ("log_recency", RECENCY_STATES)):
            _choice(getattr(self, name), name, allowed)

    @property
    def termination(self):
        """Lease is not used. Ordinary live OCX windows are not residual faults."""
        if self.process == "alive":
            if self.storage == "closed" and self.native_ui in ("runtime_error", "ocx_window_present"):
                return "residual_native"
            if self.native_ui == "runtime_error":
                return "native_error"
            return "process_alive"
        if self.process == "absent":
            if self.native_ui == "clear":
                return "exit_observed"
            if self.native_ui in ("runtime_error", "ocx_window_present"):
                return "contradictory"
            return "process_absent_native_unverified"
        return "unverified"

    def describe(self):
        return asdict(self) | {"termination": self.termination}


def assess_session(raw_observation=None, collector_observation=None, *,
                   runtime_observation=None, now=None):
    """Project validated claims; missing/old/unbound evidence never becomes health.

    Only the bounded status reader's payload is used for the session. Daily log
    freshness is displayed separately and never joined as session activity.
    No source-clock/qualification adapter exists in this phase, so positive
    freshness and research eligibility are not inferred or caller-overridden.
    """
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("timezone-aware assessment time required")
    values, issues, identity = {}, [], None
    if isinstance(collector_observation, dict):
        recency = collector_observation.get("status")
        if isinstance(recency, str) and recency in RECENCY_STATES:
            values["log_recency"] = recency
    if isinstance(raw_observation, dict) and "payload" in raw_observation:
        try:
            if raw_observation.get("status") not in ("recent", "stale", "clock_ahead"):
                raise ValueError("invalid observation envelope")
            payload = raw_observation["payload"]
            identity, stamp = validate_raw_capture_payload(payload)
            snap = payload["snapshot"]
            age = (now - stamp).total_seconds()
            recency = "clock_ahead" if age < -5 else "recent" if age <= 30 else "stale"
            values.update(session_id=identity.session_id, reported_at_utc=stamp.isoformat(),
                          reported_storage=snap["state"], status_recency=recency)
            if identity.feed_scope == DIAGNOSTIC_SCOPE:
                values["research"] = "diagnostic_only"
            if payload["error"] is not None or snap["error"] is not None:
                issues.append("status_contains_error")
            elif recency == "clock_ahead":
                issues.append("future_status")
            elif snap["state"] in ("closed", "interrupted", "failed"):
                # Preserve a historical terminal report, never current liveness.
                values["storage"] = snap["state"]
            elif recency == "recent":
                values["storage"] = "draining" if snap["state"] == "draining" else "active"
            else:
                issues.append("stale_active_status")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            identity = None
            values = {key: value for key, value in values.items() if key == "log_recency"}
            issues.append(f"invalid_status:{type(exc).__name__}")
    if runtime_observation is not None:
        if not isinstance(runtime_observation, RuntimeObservation):
            issues.append("invalid_runtime_observation")
        elif identity is None or runtime_observation.identity != identity:
            issues.append("runtime_identity_mismatch")
        else:
            age = (now - _utc(runtime_observation.observed_at_utc)).total_seconds()
            if not 0 <= age <= RUNTIME_MAX_AGE_SECONDS:
                issues.append("runtime_not_current")
            else:
                values.update(process=runtime_observation.process, native_ui=runtime_observation.native_ui,
                              lease=runtime_observation.lease,
                              runtime_observed_at_utc=runtime_observation.observed_at_utc)
    return SessionAssessment(**values, issues=tuple(issues))
