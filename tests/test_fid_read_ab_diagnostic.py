"""Synthetic tests for Mock-only FID read A-B-A diagnostic mode.

No OCX / live login. Verifies default path unchanged when flag OFF, and that
diagnostic mode changes only GetCommRealData call counts across A-B-A phases.
"""
from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory

from collector.kiwoom.capture_telemetry import CaptureTelemetry
from collector.kiwoom.fid_read_ab_diagnostic import (
    DURATION_SECONDS,
    FEED_SCOPE_DIAGNOSTIC,
    PHASE_A1,
    PHASE_A2,
    PHASE_POST,
    PHASE_B,
    PHASE_PRE,
    QUOTE_ESSENTIAL_FIDS,
    TRADE_ESSENTIAL_FIDS,
    FidReadAbController,
    active_fids_for,
    full_fids_for,
    phase_for_elapsed,
    read_fids_for_phase,
    sidecar_payload,
    validate_cli_combination,
)
from collector.kiwoom.live_capture import QUOTE_FIDS, TRADE_FIDS
from collector.kiwoom.tick_normalizer import normalize_tick


def _load_parse_collector_args():
    """Return parse_collector_args without constructing real Qt/OCX widgets."""
    import sys
    from types import ModuleType, SimpleNamespace

    def ensure_mod(name):
        if name not in sys.modules:
            sys.modules[name] = ModuleType(name)
        return sys.modules[name]

    ensure_mod("PyQt5")
    widgets = ensure_mod("PyQt5.QtWidgets")
    qax = ensure_mod("PyQt5.QAxContainer")
    core = ensure_mod("PyQt5.QtCore")

    class _App:
        def __init__(self, *a, **k):
            pass

        @staticmethod
        def instance():
            return None

    class _Widget:
        def __init__(self, *a, **k):
            pass

    class _Timer:
        def __init__(self, *a, **k):
            pass

        def timeout(self):
            return SimpleNamespace(connect=lambda *a, **k: None)

        def start(self, *a, **k):
            pass

        def stop(self, *a, **k):
            pass

    widgets.QApplication = _App
    qax.QAxWidget = _Widget
    core.QTimer = _Timer
    sys.modules.pop("collector.kiwoom.kiwoom_universe_logger", None)
    from collector.kiwoom.kiwoom_universe_logger import parse_collector_args
    return parse_collector_args


class PhaseBoundaryTests(unittest.TestCase):
    def test_pre_subscription(self):
        self.assertEqual(phase_for_elapsed(0, subscribed=False), PHASE_PRE)
        self.assertEqual(phase_for_elapsed(100, subscribed=False), PHASE_PRE)

    def test_boundaries(self):
        cases = [
            (0.0, PHASE_A1),
            (29.999999, PHASE_A1),
            (30.0, PHASE_B),
            (59.999999, PHASE_B),
            (60.0, PHASE_A2),
            (89.999999, PHASE_A2),
            (90.0, PHASE_POST),
            (120.0, PHASE_POST),
        ]
        for elapsed, expected in cases:
            with self.subTest(elapsed=elapsed):
                self.assertEqual(phase_for_elapsed(elapsed, subscribed=True), expected)


class FidSelectionTests(unittest.TestCase):
    def test_full_matches_production_order(self):
        self.assertEqual(full_fids_for("주식체결"), TRADE_FIDS)
        self.assertEqual(full_fids_for("주식호가잔량"), QUOTE_FIDS)
        self.assertEqual(list(TRADE_FIDS), [20, 10, 15, 14, 27, 28])
        self.assertEqual(list(QUOTE_FIDS), [21, *range(41, 81)])
        self.assertEqual(len(TRADE_FIDS), 6)
        self.assertEqual(len(QUOTE_FIDS), 41)

    def test_essential_matches_normalizer_needs(self):
        self.assertEqual(TRADE_ESSENTIAL_FIDS, (20, 10, 15))
        self.assertEqual(QUOTE_ESSENTIAL_FIDS, (21, 41, 51, *range(61, 81)))
        self.assertEqual(len(TRADE_ESSENTIAL_FIDS), 3)
        self.assertEqual(len(QUOTE_ESSENTIAL_FIDS), 23)

    def test_a1_full_call_sequence(self):
        calls = []
        fids, n = read_fids_for_phase("주식체결", PHASE_A1, lambda f: calls.append(f) or f"v{f}")
        self.assertEqual(calls, list(TRADE_FIDS))
        self.assertEqual(n, 6)
        self.assertEqual([fids[str(f)] for f in TRADE_FIDS], [f"v{f}" for f in TRADE_FIDS])

        calls = []
        fids, n = read_fids_for_phase("주식호가잔량", PHASE_A1, lambda f: calls.append(f) or f"v{f}")
        self.assertEqual(calls, list(QUOTE_FIDS))
        self.assertEqual(n, 41)

    def test_b_essential_skips_as_none(self):
        calls = []
        fids, n = read_fids_for_phase("주식체결", PHASE_B, lambda f: calls.append(f) or f"v{f}")
        self.assertEqual(calls, [20, 10, 15])
        self.assertEqual(n, 3)
        self.assertEqual(fids["20"], "v20")
        self.assertEqual(fids["10"], "v10")
        self.assertEqual(fids["15"], "v15")
        self.assertIsNone(fids["14"])
        self.assertIsNone(fids["27"])
        self.assertIsNone(fids["28"])
        self.assertEqual(set(fids), {str(f) for f in TRADE_FIDS})

        calls = []
        fids, n = read_fids_for_phase("주식호가잔량", PHASE_B, lambda f: calls.append(f) or f"v{f}")
        self.assertEqual(calls, list(QUOTE_ESSENTIAL_FIDS))
        self.assertEqual(n, 23)
        for fid in QUOTE_FIDS:
            key = str(fid)
            if fid in QUOTE_ESSENTIAL_FIDS:
                self.assertEqual(fids[key], f"v{fid}")
            else:
                self.assertIsNone(fids[key])

    def test_a2_restores_full(self):
        calls = []
        _, n = read_fids_for_phase("주식체결", PHASE_A2, lambda f: calls.append(f) or "x")
        self.assertEqual(calls, list(TRADE_FIDS))
        self.assertEqual(n, 6)

    def test_post_90s_keeps_full_policy(self):
        calls = []
        _, n = read_fids_for_phase("주식체결", PHASE_POST, lambda f: calls.append(f) or "x")
        self.assertEqual(calls, list(TRADE_FIDS))
        self.assertEqual(n, 6)

    def test_pre_subscription_full(self):
        self.assertEqual(active_fids_for("주식체결", PHASE_PRE), TRADE_FIDS)
        self.assertEqual(active_fids_for("주식호가잔량", PHASE_PRE), QUOTE_FIDS)

    def test_no_invented_stale_values(self):
        """Unread FIDs must be None, never a previous callback value."""
        prev = {str(f): "STALE" for f in TRADE_FIDS}
        fids, _ = read_fids_for_phase("주식체결", PHASE_B, lambda f: "NEW")
        self.assertIsNone(fids["14"])
        self.assertNotEqual(fids["14"], prev["14"])


class NormalizerNoneFidTests(unittest.TestCase):
    def test_essential_trade_does_not_hard_fail_on_optional_nones(self):
        fids = {str(f): None for f in TRADE_FIDS}
        fids["20"], fids["10"], fids["15"] = "090000", "70000", "+10"
        result = normalize_tick(
            code="005930", venue="unknown", real_type="주식체결", fids=fids,
            received_ns=1, received_at_utc="2026-09-23T00:00:00+00:00",
            source="kiwoom", session_id="t", seq=1,
            price_policy="signed_magnitude", direction_policy="signed_volume")
        self.assertIsNotNone(result.event)
        self.assertEqual(result.event.price, 70000)
        self.assertEqual(result.event.volume, 10)
        self.assertIsNone(result.raw_fields["fids"]["14"])
        self.assertIsNone(result.raw_fields["fids"]["27"])

    def test_essential_quote_with_none_skips(self):
        fids = {str(f): None for f in QUOTE_FIDS}
        fids["21"] = "090000"
        fids["41"] = "70100"
        fids["51"] = "70000"
        for i, fid in enumerate(range(61, 81)):
            fids[str(fid)] = str(i + 1)
        result = normalize_tick(
            code="005930", venue="unknown", real_type="주식호가잔량", fids=fids,
            received_ns=1, received_at_utc="2026-09-23T00:00:00+00:00",
            source="kiwoom", session_id="t", seq=1,
            price_policy="signed_magnitude", direction_policy="signed_volume")
        self.assertIsNotNone(result.event)
        self.assertEqual(result.event.ask, 70100)
        self.assertEqual(result.event.bid, 70000)
        self.assertIsNone(result.raw_fields["fids"]["42"])
        self.assertIsNone(result.raw_fields["fids"]["52"])


class CliGuardTests(unittest.TestCase):
    def test_validate_rejects_invalid_combos(self):
        base = dict(enabled=True, storage="raw-v2", capture_telemetry=True, codes=None,
                    plan=None, aftermarket_given=False, explicit_ocx_teardown=False,
                    duration_seconds=90, managed_launch=False)
        self.assertIsNone(validate_cli_combination(**base))
        self.assertIsNotNone(validate_cli_combination(**{**base, "capture_telemetry": False}))
        self.assertIsNotNone(validate_cli_combination(**{**base, "storage": "raw-v1"}))
        self.assertIsNotNone(validate_cli_combination(**{**base, "codes": ["005930"]}))
        self.assertIsNotNone(validate_cli_combination(**{**base, "plan": object()}))
        self.assertIsNotNone(validate_cli_combination(**{**base, "aftermarket_given": True}))
        self.assertIsNotNone(validate_cli_combination(**{**base, "explicit_ocx_teardown": True}))
        self.assertIsNotNone(validate_cli_combination(**{**base, "duration_seconds": 60}))
        self.assertIsNotNone(validate_cli_combination(**{**base, "managed_launch": True}))

    def test_parse_rejects_before_qt(self):
        parse = _load_parse_collector_args()
        with self.assertRaises(SystemExit):
            parse(["--fid-read-ab-test", "--capture-telemetry",
                   "--duration-seconds", "90", "--storage", "raw-v1"])
        with self.assertRaises(SystemExit):
            parse(["--fid-read-ab-test", "--duration-seconds", "90"])
        with self.assertRaises(SystemExit):
            parse(["--fid-read-ab-test", "--capture-telemetry",
                   "--duration-seconds", "60"])
        with self.assertRaises(SystemExit):
            parse(["--fid-read-ab-test", "--capture-telemetry",
                   "--duration-seconds", "90", "--codes", "005930"])
        with self.assertRaises(SystemExit):
            parse(["--fid-read-ab-test", "--capture-telemetry",
                   "--duration-seconds", "90", "--explicit-ocx-teardown"])

    def test_parse_accepts_valid_diagnostic(self):
        parse = _load_parse_collector_args()
        args, codes, plan, *_ = parse([
            "--fid-read-ab-test", "--capture-telemetry", "--duration-seconds", "90"])
        self.assertTrue(args.fid_read_ab_test)
        self.assertTrue(args.capture_telemetry)
        self.assertEqual(args.duration_seconds, DURATION_SECONDS)
        self.assertIsNone(codes)
        self.assertIsNone(plan)


class SidecarAndScopeTests(unittest.TestCase):
    def test_sidecar_marks_research_ineligible(self):
        payload = sidecar_payload(code_revision="abc", intended_server="mock")
        self.assertTrue(payload["diagnostic_only"])
        self.assertFalse(payload["research_eligible"])
        self.assertTrue(payload["subscription_unchanged"])
        self.assertEqual(payload["feed_scope"], FEED_SCOPE_DIAGNOSTIC)
        self.assertEqual(payload["intended_server"], "mock")
        self.assertNotEqual(FEED_SCOPE_DIAGNOSTIC, "kiwoom_universe_venue_unverified")


class TelemetryDiagFieldsTests(unittest.TestCase):
    def test_default_sample_omits_diag_keys(self):
        with TemporaryDirectory() as tmp:
            probe = CaptureTelemetry(clock_ns=lambda: 1_000)
            try:
                self.assertTrue(probe.bind(tmp, session_id="s", code_revision="r"))
                probe.callback_sample(
                    code="005930", real_type="주식체결", fids={"20": "090000"},
                    received_ns=0, received_at_utc="2026-09-23T00:00:00+00:00",
                    accepted=True, finished_ns=100)
                row = probe._samples[0]
                self.assertIn("processing_ns", row)
                self.assertNotIn("diagnostic_phase", row)
                self.assertNotIn("fid_call_count", row)
                self.assertNotIn("fid_read_ns", row)
                self.assertNotIn("queue_submit_ns", row)
            finally:
                if probe.stream is not None:
                    probe.stream.close()
                    probe.stream = None

    def test_diag_sample_timing_consistency(self):
        with TemporaryDirectory() as tmp:
            probe = CaptureTelemetry(clock_ns=lambda: 1_000)
            try:
                self.assertTrue(probe.bind(tmp, session_id="s", code_revision="r"))
                probe.callback_sample(
                    code="005930", real_type="주식체결", fids={"20": "090000"},
                    received_ns=0, received_at_utc="2026-09-23T00:00:00+00:00",
                    accepted=True, finished_ns=500,
                    diagnostic_phase=PHASE_B, fid_call_count=3,
                    fid_read_ns=200, queue_submit_ns=300)
                row = probe._samples[0]
                self.assertEqual(row["processing_ns"], 500)
                self.assertEqual(row["fid_read_ns"], 200)
                self.assertEqual(row["queue_submit_ns"], 300)
                self.assertGreaterEqual(row["processing_ns"], row["fid_read_ns"])
                self.assertGreaterEqual(row["processing_ns"], row["queue_submit_ns"])
                self.assertEqual(row["diagnostic_phase"], PHASE_B)
                self.assertEqual(row["fid_call_count"], 3)
            finally:
                if probe.stream is not None:
                    probe.stream.close()
                    probe.stream = None

    def test_observer_failure_does_not_break_capture(self):
        from collector.kiwoom.capture_telemetry import observe

        class Boom:
            def callback_sample(self, **kwargs):
                raise RuntimeError("boom")

            def disable(self, reason):
                self.reason = reason

        probe = Boom()
        self.assertIsNone(observe(probe, "callback_sample", code="x"))
        self.assertTrue(str(probe.reason).startswith("callback_sample:"))


class SyntheticCallCountTests(unittest.TestCase):
    """Fake OCX: same callback counts, fewer GetCommRealData in ESSENTIAL."""

    def test_full_vs_essential_ratio(self):
        class FakeOcx:
            def __init__(self):
                self.calls = 0

            def dynamicCall(self, sig, *args):
                if "GetCommRealData" in sig:
                    self.calls += 1
                    return "1"
                raise AssertionError(sig)

        def drive(phase, n_trade, n_quote):
            ocx = FakeOcx()

            def read_fid(fid):
                return ocx.dynamicCall("GetCommRealData(QString, int)", "005930", fid)

            for _ in range(n_trade):
                read_fids_for_phase("주식체결", phase, read_fid)
            for _ in range(n_quote):
                read_fids_for_phase("주식호가잔량", phase, read_fid)
            return ocx.calls

        n_t, n_q = 100, 100
        full = drive(PHASE_A1, n_t, n_q)
        essential = drive(PHASE_B, n_t, n_q)
        self.assertEqual(full, n_t * 6 + n_q * 41)
        self.assertEqual(essential, n_t * 3 + n_q * 23)
        self.assertLess(essential, full)
        self.assertAlmostEqual(essential / full, (3 + 23) / (6 + 41), places=6)


class ControllerCounterTests(unittest.TestCase):
    def test_phase_counters(self):
        clock = {"t": 0.0}
        ctl = FidReadAbController(monotonic=lambda: clock["t"])
        ctl.mark_subscribed(0.0)
        for t, phase, rtype, expect_calls in [
            (10.0, PHASE_A1, "주식체결", 6),
            (40.0, PHASE_B, "주식호가잔량", 23),
            (70.0, PHASE_A2, "주식체결", 6),
            (100.0, PHASE_POST, "주식체결", 6),
        ]:
            clock["t"] = t
            self.assertEqual(ctl.current_phase(), phase)
            fids, n = read_fids_for_phase(rtype, phase, lambda f: "x")
            ctl.note_callback(rtype, phase, n)
            self.assertEqual(n, expect_calls)
        snap = ctl.snapshot()
        self.assertEqual(snap["trade_callbacks_by_phase"][PHASE_A1], 1)
        self.assertEqual(snap["quote_callbacks_by_phase"][PHASE_B], 1)
        self.assertEqual(snap["fid_calls_by_phase"][PHASE_A1], 6)
        self.assertEqual(snap["fid_calls_by_phase"][PHASE_B], 23)
        self.assertEqual(snap["fid_calls_by_phase"][PHASE_A2], 6)
        self.assertEqual(snap["fid_calls_by_phase"][PHASE_POST], 6)
        self.assertFalse(snap["a2_includes_shutdown_tail"])
        self.assertEqual(snap["post_90s_phase"], PHASE_POST)


class DefaultPathInvariantTests(unittest.TestCase):
    def test_off_path_uses_full_lists_only(self):
        self.assertIs(full_fids_for("주식체결"), TRADE_FIDS)
        self.assertIs(full_fids_for("주식호가잔량"), QUOTE_FIDS)

    def test_validate_off_always_none(self):
        self.assertIsNone(validate_cli_combination(
            enabled=False, storage="raw-v1", capture_telemetry=False, codes=["1"],
            plan=object(), aftermarket_given=True, explicit_ocx_teardown=True,
            duration_seconds=1, managed_launch=True))


class SubscriptionInvariantDocTests(unittest.TestCase):
    def test_independent_variable_is_fid_call_count(self):
        payload = sidecar_payload(code_revision="x")
        self.assertEqual(payload["schema"], "fid_read_ab_test_v2")
        self.assertEqual(payload["independent_variable"], "GetCommRealData_call_count")
        self.assertTrue(payload["subscription_unchanged"])
        self.assertTrue(payload["strict_phase_windows"])
        self.assertFalse(payload["phases"][PHASE_A2]["includes_shutdown_tail_after_nominal_end"])
        self.assertFalse(payload["phases"][PHASE_POST]["analysis_window"])


if __name__ == "__main__":
    unittest.main()
