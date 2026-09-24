"""Mock 전용 FID 읽기 정책 A-B-A 진단.

구독은 고정하지만 COM 호출·반환 문자열·JSON/저장 비용은 함께 변한다.
순수 COM 비용의 단일변수 실험이 아니다. 기본 OFF 경로는 유지한다.
"""
from __future__ import annotations

from collector.kiwoom.live_capture import QUOTE_FIDS, TRADE_FIDS
from collector.research_input_policy import FID_READ_DIAGNOSTIC_SCOPE

PHASE_PRE = "PRE_SUBSCRIPTION_FULL"
PHASE_A1 = "A1_FULL"
PHASE_B = "B_ESSENTIAL"
PHASE_A2 = "A2_FULL"
PHASE_POST = "POST_90S_FULL"

DURATION_SECONDS = 90
PHASE_A1_END = 30.0
PHASE_B_END = 60.0
PHASE_A2_END = 90.0

TRADE_ESSENTIAL_FIDS = (20, 10, 15)
QUOTE_ESSENTIAL_FIDS = (21, 41, 51, *range(61, 81))

FEED_SCOPE_DIAGNOSTIC = FID_READ_DIAGNOSTIC_SCOPE
SIDECAR_NAME = "fid_read_ab_test.json"
SIDECAR_SCHEMA = "fid_read_ab_test_v2"
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
    """읽기 정책: [0,30) FULL, [30,60) ESSENTIAL, [60,90) FULL, 이후 POST FULL.

    90초 이후에도 종료 요청 처리 전까지 FID 읽기는 FULL을 유지하되,
    분석 phase는 POST_90S_FULL로 분리해 A2 누적치에 종료 꼬리를 섞지 않는다.
    """
    if not subscribed:
        return PHASE_PRE
    if elapsed_sec < 0:
        return PHASE_PRE
    if elapsed_sec < PHASE_A1_END:
        return PHASE_A1
    if elapsed_sec < PHASE_B_END:
        return PHASE_B
    if elapsed_sec < PHASE_A2_END:
        return PHASE_A2
    return PHASE_POST


def is_essential_phase(phase: str) -> bool:
    return phase == PHASE_B


def active_fids_for(real_type: str, phase: str) -> tuple[int, ...]:
    if is_essential_phase(phase):
        return essential_fids_for(real_type)
    return full_fids_for(real_type)


def read_fids_for_phase(real_type: str, phase: str, read_fid, *, fids=None, progress=None):
    """미조회 값은 None. 예외까지 읽은 원문과 호출 시도/완료 계수는 caller에 남긴다.

    오류 뒤 아직 방문하지 않은 key는 만들지 않는다. None은 의도적으로
    생략한 FID이지 성공적인 COM 반환이나 이전 callback 값이 아니다.
    """
    full = full_fids_for(real_type)
    active = set(active_fids_for(real_type, phase))
    if fids is None:
        fids = {}
    if progress is None:
        progress = {"attempted": 0, "completed": 0}
    for fid in full:
        key = str(fid)
        if fid in active:
            progress["attempted"] += 1
            fids[key] = read_fid(fid)
            progress["completed"] += 1
        else:
            fids[key] = None
    return fids, progress["completed"]


class FidReadAbController:
    """진단 정책 시계와 제한된 누적 계수. hot path 파일 I/O는 없다."""

    def __init__(self, *, monotonic=None):
        self._monotonic = monotonic
        self.subscribed_at = None
        self.diagnostic_error = None
        phases = (PHASE_PRE, PHASE_A1, PHASE_B, PHASE_A2, PHASE_POST)
        self.trade_by_phase = dict.fromkeys(phases, 0)
        self.quote_by_phase = dict.fromkeys(phases, 0)
        self.fid_calls_by_phase = dict.fromkeys(phases, 0)
        self.fid_attempts_by_phase = dict.fromkeys(phases, 0)
        self.read_failures_by_phase = dict.fromkeys(phases, 0)

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

    def disable(self, reason):
        if self.diagnostic_error is None:
            self.diagnostic_error = str(reason)[:256]

    def note_callback(self, real_type: str, phase: str, fid_calls: int, *,
                      attempted_calls=None, read_failed=False) -> None:
        if self.diagnostic_error is not None:
            return
        if real_type == "주식체결":
            self.trade_by_phase[phase] = self.trade_by_phase.get(phase, 0) + 1
        elif real_type == "주식호가잔량":
            self.quote_by_phase[phase] = self.quote_by_phase.get(phase, 0) + 1
        self.fid_calls_by_phase[phase] = self.fid_calls_by_phase.get(phase, 0) + int(fid_calls)
        attempts = fid_calls if attempted_calls is None else attempted_calls
        self.fid_attempts_by_phase[phase] = self.fid_attempts_by_phase.get(phase, 0) + int(attempts)
        if read_failed:
            self.read_failures_by_phase[phase] = self.read_failures_by_phase.get(phase, 0) + 1

    def snapshot(self) -> dict:
        return {
            "trade_callbacks_by_phase": dict(self.trade_by_phase),
            "quote_callbacks_by_phase": dict(self.quote_by_phase),
            "fid_calls_by_phase": dict(self.fid_calls_by_phase),
            "fid_attempts_by_phase": dict(self.fid_attempts_by_phase),
            "fid_read_failures_by_phase": dict(self.read_failures_by_phase),
            "subscribed_at_set": self.subscribed_at is not None,
            "diagnostic_error": self.diagnostic_error,
            "counter_scope": "callback_read_attempts_not_accepted_or_committed",
            "a2_includes_shutdown_tail": False,
            "post_90s_phase": PHASE_POST,
        }


def sidecar_payload(*, code_revision: str, intended_server: str = "mock") -> dict:
    return {
        "schema": SIDECAR_SCHEMA,
        "diagnostic_only": True,
        "research_eligible": False,
        "intended_server": intended_server,
        "subscription_unchanged": True,
        "subscription_unchanged_is_design_claim": True,
        "duration_seconds": DURATION_SECONDS,
        "duration_is_shutdown_request_not_hard_cutoff": True,
        "phases": {
            PHASE_PRE: {"fid_set": "FULL", "analysis_window": False,
                        "note": "callbacks before _subscribed_at"},
            PHASE_A1: {"elapsed": [0, PHASE_A1_END], "fid_set": "FULL",
                       "analysis_window": True},
            PHASE_B: {"elapsed": [PHASE_A1_END, PHASE_B_END], "fid_set": "ESSENTIAL",
                      "analysis_window": True},
            PHASE_A2: {"elapsed": [PHASE_B_END, PHASE_A2_END], "fid_set": "FULL",
                       "analysis_window": True,
                       "includes_shutdown_tail_after_nominal_end": False},
            PHASE_POST: {"elapsed": [PHASE_A2_END, None], "fid_set": "FULL",
                         "analysis_window": False,
                         "note": "callbacks after nominal 90s until input stop"},
        },
        "strict_phase_windows": True,
        "post_90s_callbacks_separated": True,
        "full_fids": {"trade": list(TRADE_FIDS), "quote": list(QUOTE_FIDS)},
        "essential_fids": {
            "trade": list(TRADE_ESSENTIAL_FIDS),
            "quote": list(QUOTE_ESSENTIAL_FIDS),
        },
        "feed_scope": FEED_SCOPE_DIAGNOSTIC,
        "code_revision": code_revision,
        "resource_sample_interval_sec": RESOURCE_INTERVAL_SEC,
        "independent_variable": "GetCommRealData_call_count",
        "independent_variable_is_nominal_treatment": True,
        "pure_com_cost_experiment": False,
        "co_varying_costs": ["source_string_allocation", "python_fid_dict_work",
                             "json_serialization", "stored_payload_bytes", "worker_processing"],
        "phase_clock": "time.monotonic_once_per_callback_before_fid_reads",
        "backlog_reset_between_phases": False,
        "sample_design": "first_callback_per_real_type_per_5_seconds_not_per_symbol",
        "timing_scopes": {
            "fid_read_ns": "callback_entry_through_fid_read_and_diagnostic_bookkeeping",
            "queue_submit_ns": "post_read_clock_to_submit_return_including_python_overhead",
            "processing_ns": "callback_entry_to_queue_submit_return_not_storage_drain",
            "fid_call_count": "successfully_returned_reads_not_attempted_reads",
        },
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
