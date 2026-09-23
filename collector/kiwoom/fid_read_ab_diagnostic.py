"""FID read A-B-A diagnostic helpers (Mock-only experiment prep).

Independent variable: number of GetCommRealData calls inside the callback.
Subscription (SetRealReg / REAL_FIDS / screens) must not change across phases.
Default collector path is unchanged when the diagnostic flag is off.
"""
from __future__ import annotations

from collector.kiwoom.live_capture import QUOTE_FIDS, TRADE_FIDS

# Phase labels used in telemetry / counters.
PHASE_PRE = "PRE_SUBSCRIPTION_FULL"
PHASE_A1 = "A1_FULL"
PHASE_B = "B_ESSENTIAL"
PHASE_A2 = "A2_FULL"

DURATION_SECONDS = 90
PHASE_A1_END = 30.0
PHASE_B_END = 60.0
PHASE_A2_END = 90.0

# Source fields actually required by tick_normalizer.normalize_tick for a
# successful parse of the fields it emits (verified against tick_normalizer.py).
TRADE_ESSENTIAL_FIDS = (20, 10, 15)
QUOTE_ESSENTIAL_FIDS = (21, 41, 51, *range(61, 81))

FEED_SCOPE_DIAGNOSTIC = "kiwoom_universe_fid_read_diagnostic"
SIDECAR_NAME = "fid_read_ab_test.json"
SIDECAR_SCHEMA = "fid_read_ab_test_v1"
RESOURCE_INTERVAL_SEC = 5.0


def essential_fids_for(real_type: str) -> tuple[int, ...]:
    if real_type == "주식체결":
        return TRADE_ESSENTIAL_FIDS
    if real_type == "주식호가잔량":
        return QUOTE_ESSENTIAL_FIDS
    raise ValueError(f"unsupported real_type for essential FIDs: {real_type!r}")


def full_fids_for(real_type: str) -> tuple[int, ...]:
    if real_type == "주식체결":
        return TRADE_FIDS
    if real_type == "주식호가잔량":
        return QUOTE_FIDS
    raise ValueError(f"unsupported real_type for full FIDs: {real_type!r}")


def phase_for_elapsed(elapsed_sec, *, subscribed: bool) -> str:
    """Map seconds since _subscribed_at to A-B-A phase.

    Pre-subscription callbacks stay on FULL. Boundaries use half-open intervals:
    [0,30) A1 FULL, [30,60) B ESSENTIAL, [60,90) A2 FULL.
    """
    if not subscribed:
        return PHASE_PRE
    if elapsed_sec < 0:
        return PHASE_PRE
    if elapsed_sec < PHASE_A1_END:
        return PHASE_A1
    if elapsed_sec < PHASE_B_END:
        return PHASE_B
    return PHASE_A2


def is_essential_phase(phase: str) -> bool:
    return phase == PHASE_B


def active_fids_for(real_type: str, phase: str) -> tuple[int, ...]:
    if is_essential_phase(phase):
        return essential_fids_for(real_type)
    return full_fids_for(real_type)


def read_fids_for_phase(real_type: str, phase: str, read_fid):
    """Build a full-key fids dict; unread FIDs are explicitly None (never invented)."""
    full = full_fids_for(real_type)
    active = set(active_fids_for(real_type, phase))
    fids = {}
    calls = 0
    for fid in full:
        key = str(fid)
        if fid in active:
            fids[key] = read_fid(fid)
            calls += 1
        else:
            fids[key] = None
    return fids, calls


class FidReadAbController:
    """Tiny in-memory phase counters + phase clock. No disk I/O on the hot path."""

    def __init__(self, *, monotonic=None):
        self._monotonic = monotonic
        self.subscribed_at = None
        self.trade_by_phase = {PHASE_PRE: 0, PHASE_A1: 0, PHASE_B: 0, PHASE_A2: 0}
        self.quote_by_phase = {PHASE_PRE: 0, PHASE_A1: 0, PHASE_B: 0, PHASE_A2: 0}
        self.fid_calls_by_phase = {PHASE_PRE: 0, PHASE_A1: 0, PHASE_B: 0, PHASE_A2: 0}

    def mark_subscribed(self, at):
        self.subscribed_at = at

    def current_phase(self, now=None) -> str:
        if self.subscribed_at is None:
            return PHASE_PRE
        clock = self._monotonic
        if now is None:
            if clock is None:
                raise RuntimeError("monotonic clock required")
            now = clock()
        return phase_for_elapsed(now - self.subscribed_at, subscribed=True)

    def note_callback(self, real_type: str, phase: str, fid_calls: int) -> None:
        if real_type == "주식체결":
            self.trade_by_phase[phase] = self.trade_by_phase.get(phase, 0) + 1
        elif real_type == "주식호가잔량":
            self.quote_by_phase[phase] = self.quote_by_phase.get(phase, 0) + 1
        self.fid_calls_by_phase[phase] = self.fid_calls_by_phase.get(phase, 0) + int(fid_calls)

    def snapshot(self) -> dict:
        return {
            "trade_callbacks_by_phase": dict(self.trade_by_phase),
            "quote_callbacks_by_phase": dict(self.quote_by_phase),
            "fid_calls_by_phase": dict(self.fid_calls_by_phase),
            "subscribed_at_set": self.subscribed_at is not None,
        }


def sidecar_payload(*, code_revision: str, intended_server: str = "mock") -> dict:
    return {
        "schema": SIDECAR_SCHEMA,
        "diagnostic_only": True,
        "research_eligible": False,
        "intended_server": intended_server,
        "subscription_unchanged": True,
        "duration_seconds": DURATION_SECONDS,
        "phases": {
            PHASE_PRE: {"fid_set": "FULL", "note": "callbacks before _subscribed_at"},
            PHASE_A1: {"elapsed": [0, PHASE_A1_END], "fid_set": "FULL"},
            PHASE_B: {"elapsed": [PHASE_A1_END, PHASE_B_END], "fid_set": "ESSENTIAL"},
            PHASE_A2: {"elapsed": [PHASE_B_END, PHASE_A2_END], "fid_set": "FULL"},
        },
        "full_fids": {"trade": list(TRADE_FIDS), "quote": list(QUOTE_FIDS)},
        "essential_fids": {
            "trade": list(TRADE_ESSENTIAL_FIDS),
            "quote": list(QUOTE_ESSENTIAL_FIDS),
        },
        "feed_scope": FEED_SCOPE_DIAGNOSTIC,
        "code_revision": code_revision,
        "resource_sample_interval_sec": RESOURCE_INTERVAL_SEC,
        "independent_variable": "GetCommRealData_call_count",
    }


def validate_cli_combination(
    *,
    enabled: bool,
    storage: str,
    capture_telemetry: bool,
    codes,
    plan,
    aftermarket_given: bool,
    explicit_ocx_teardown: bool,
    duration_seconds,
    managed_launch: bool,
) -> str | None:
    """Return an error message if the diagnostic flag is used with an invalid combo."""
    if not enabled:
        return None
    if storage != "raw-v2":
        return "fid-read-ab-test requires --storage raw-v2"
    if not capture_telemetry:
        return "fid-read-ab-test requires --capture-telemetry"
    if codes is not None:
        return "fid-read-ab-test requires full-universe (no --codes)"
    if plan is not None:
        return "fid-read-ab-test does not support NXT plans"
    if aftermarket_given:
        return "fid-read-ab-test does not support aftermarket transition"
    if explicit_ocx_teardown:
        return "fid-read-ab-test requires explicit OCX teardown OFF"
    if duration_seconds != DURATION_SECONDS:
        return f"fid-read-ab-test requires --duration-seconds {DURATION_SECONDS}"
    if managed_launch:
        return "fid-read-ab-test does not support managed launch"
    return None
