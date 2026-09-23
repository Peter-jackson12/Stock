"""Orthogonal evidence axes for the operations control tower.

The reducer deliberately refuses to collapse storage, OS-process, native-window,
lease, callback-activity, source-freshness, and research-eligibility evidence into one "healthy"
boolean.  A clean raw-v2 close proves only the storage path.  A free lease does
not prove process exit, and an empty Python queue does not prove upstream feed
freshness.
"""
from dataclasses import asdict, dataclass


STORAGE_STATES = {
    "unverified", "active", "draining", "closed", "interrupted", "failed",
}
PROCESS_STATES = {"unverified", "alive", "absent", "access_denied", "mismatch"}
NATIVE_UI_STATES = {"unverified", "clear", "runtime_error", "ocx_window_present"}
LEASE_STATES = {"unverified", "held", "free"}
ACTIVITY_STATES = {"unverified", "recent", "stale", "clock_ahead", "stopped"}
SOURCE_FRESHNESS_STATES = {"unverified", "fresh", "lagging"}
RESEARCH_STATES = {"unverified", "diagnostic_only", "ineligible", "eligible"}


def _choice(value, name, allowed):
    if value not in allowed:
        raise ValueError(f"invalid {name}: {value!r}")
    return value


@dataclass(frozen=True)
class SessionAssessment:
    storage: str = "unverified"
    process: str = "unverified"
    native_ui: str = "unverified"
    lease: str = "unverified"
    activity: str = "unverified"
    source_freshness: str = "unverified"
    research: str = "unverified"

    def __post_init__(self):
        _choice(self.storage, "storage state", STORAGE_STATES)
        _choice(self.process, "process state", PROCESS_STATES)
        _choice(self.native_ui, "native UI state", NATIVE_UI_STATES)
        _choice(self.lease, "lease state", LEASE_STATES)
        _choice(self.activity, "callback activity state", ACTIVITY_STATES)
        _choice(self.source_freshness, "source freshness state", SOURCE_FRESHNESS_STATES)
        _choice(self.research, "research state", RESEARCH_STATES)

    @property
    def termination(self):
        """Derived OS/native termination evidence. Lease state is intentionally ignored."""
        if self.process == "alive":
            if self.native_ui in ("runtime_error", "ocx_window_present"):
                return "residual_native"
            return "process_alive"
        if self.process == "absent":
            if self.native_ui == "clear":
                return "verified_exited"
            if self.native_ui in ("runtime_error", "ocx_window_present"):
                return "contradictory"
            return "process_absent_native_unverified"
        return "unverified"

    def describe(self):
        return asdict(self) | {"termination": self.termination}


def assess_session(raw_observation=None, collector_observation=None, *,
                   process="unverified", native_ui="unverified",
                   lease="unverified", activity=None,
                   source_freshness="unverified", research="unverified"):
    """Build a read-only assessment from already bounded observations.

    raw_observation is expected to be the validated output of
    control_tower.status.observe_raw_capture(). collector_observation is the
    bounded log-tail output of observe_collector() and only informs callback
    activity, never source freshness. Missing evidence stays unverified; this function performs no process scan, window scan, lease probe,
    raw-file inspection, qualification, or automatic recovery.
    """
    storage = "unverified"
    if isinstance(raw_observation, dict) and isinstance(raw_observation.get("payload"), dict):
        snapshot = raw_observation["payload"].get("snapshot")
        if isinstance(snapshot, dict):
            storage = {
                "starting": "active",
                "running": "active",
                "draining": "draining",
                "closed": "closed",
                "interrupted": "interrupted",
                "failed": "failed",
            }.get(snapshot.get("state"), "unverified")

    observed_activity = "unverified" if activity is None else activity
    if activity is None and isinstance(collector_observation, dict):
        observed_activity = {
            "recent": "recent",
            "stale": "stale",
            "clock_ahead": "clock_ahead",
        }.get(collector_observation.get("status"), "unverified")

    return SessionAssessment(
        storage=storage,
        process=process,
        native_ui=native_ui,
        lease=lease,
        activity=observed_activity,
        source_freshness=source_freshness,
        research=research,
    )
