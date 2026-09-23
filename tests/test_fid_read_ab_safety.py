"""PR #20 독립 회귀: fake callback, 작은 새 DB, 주입 시계만 사용한다."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sqlite3

import pytest

from collector.kiwoom.capture_telemetry import CaptureTelemetry
from collector.kiwoom.fid_read_ab_diagnostic import (
    FEED_SCOPE_DIAGNOSTIC, PHASE_PRE, PHASE_A1, PHASE_B, PHASE_A2,
    FidReadAbController, active_fids_for, sidecar_payload,
)
from collector.kiwoom.live_capture import LiveRawCapture, TRADE_FIDS, QUOTE_FIDS
from collector.raw_v2 import RawV2Writer, read_raw_v2
from collector.raw_v2_qualification import qualify_raw_v2
from control_tower.windows_process import ProcessFacts
from engine.tick_research_run import run_raw_v2, run_research
from tests.test_live_collector import live
from tests.test_tick_collector_shutdown import collector
from tests.test_tick_research_run import events as fixture_events

UTC = "2026-09-23T00:00:00+00:00"
NORMAL_SCOPE = "kiwoom_universe_venue_unverified"


class Queue:
    def __init__(self, error=None):
        self.packets = []
        self.error = error

    def submit_tick(self, **packet):
        self.packets.append(deepcopy(packet))
        if self.error is not None:
            raise self.error
        return True


class Probe:
    def __init__(self, *, fail_at=None, invalid_at=None, invalid_value=None, sampled=True):
        self.fail_at, self.invalid_at, self.invalid_value = fail_at, invalid_at, invalid_value
        self.sampled = sampled
        self.clocks = 0
        self.errors = []
        self.rows = []

    def reserve_sample(self, *args):
        return self.sampled

    def clock_ns(self):
        self.clocks += 1
        if self.clocks == self.fail_at:
            raise RuntimeError("sample clock failed")
        if self.clocks == self.invalid_at:
            return self.invalid_value
        return 100 * self.clocks

    def disable(self, reason):
        self.errors.append(reason)

    def callback_sample(self, **row):
        self.rows.append(row)


def backend(elapsed=None, *, probe=None, queue_error=None):
    capture = LiveRawCapture.__new__(LiveRawCapture)
    capture.queue = Queue(queue_error)
    capture.telemetry = probe
    capture._error = None
    capture.received_trades = capture.received_quotes = 0
    capture.fid_read_ab = None
    if elapsed is not None:
        capture.fid_read_ab = FidReadAbController(monotonic=lambda: elapsed)
        capture.fid_read_ab.mark_subscribed(0)
    return capture


def send(capture, read, real_type="주식체결"):
    return capture.on_tick("005930", real_type, read, received_ns=0, received_at_utc=UTC)


@pytest.mark.parametrize("real_type,full", [("주식체결", TRADE_FIDS), ("주식호가잔량", QUOTE_FIDS)])
def test_off_actual_callback_preserves_fid_order_and_original_values(real_type, full):
    capture = backend(probe=Probe(sampled=False))
    calls = []
    values = {fid: f" +{fid} " for fid in full}
    assert send(capture, lambda fid: calls.append(fid) or values[fid], real_type)
    assert calls == list(full)
    packet = capture.queue.packets[0]
    assert packet == dict(code="005930", venue="unknown", real_type=real_type,
                          fids={str(fid): values[fid] for fid in full}, received_ns=0, received_at_utc=UTC)
    assert capture.telemetry.clocks == 0 and capture._error is None


@pytest.mark.parametrize("elapsed,phase", [(10, PHASE_A1), (40, PHASE_B)])
@pytest.mark.parametrize("real_type,full", [("주식체결", TRADE_FIDS), ("주식호가잔량", QUOTE_FIDS)])
def test_every_fid_failure_position_preserves_partial_input_and_attempt_counts(elapsed, phase, real_type, full):
    active = active_fids_for(real_type, phase)
    for failed_index, failed_fid in enumerate(active):
        capture = backend(elapsed)
        calls = []
        def read(fid):
            calls.append(fid)
            if fid == failed_fid:
                raise RuntimeError("FID sentinel")
            return f" +{fid} "
        assert send(capture, read, real_type)
        assert calls == list(active[:failed_index + 1])
        packet = capture.queue.packets[0]
        assert packet["real_type"] == "callback_error"
        before_failure = full[:full.index(failed_fid)]
        expected = {str(fid): f" +{fid} " if fid in active else None for fid in before_failure}
        actual = dict(packet["fids"])
        assert "FID sentinel" in actual.pop("_read_error")
        assert actual == expected, (phase, real_type, failed_fid, actual)
        counters = capture.fid_read_ab.snapshot()
        assert counters["fid_calls_by_phase"][phase] == failed_index
        assert counters["fid_attempts_by_phase"][phase] == failed_index + 1
        assert counters["fid_read_failures_by_phase"][phase] == 1
        key = "trade_callbacks_by_phase" if real_type == "주식체결" else "quote_callbacks_by_phase"
        assert counters[key][phase] == 1


@pytest.mark.parametrize("elapsed", [10, 40])
@pytest.mark.parametrize("fail_at", [1, 2])
def test_sample_clock_exception_is_not_a_fid_or_capture_failure(elapsed, fail_at):
    probe = Probe(fail_at=fail_at)
    capture = backend(elapsed, probe=probe)
    assert send(capture, lambda fid: f" +{fid} ")
    assert capture._error is None and capture.received_trades == 1
    assert capture.queue.packets[0]["real_type"] == "주식체결"
    assert "_read_error" not in capture.queue.packets[0]["fids"]
    assert probe.errors


@pytest.mark.parametrize("value", [None, True, -1, 1.5, "100"])
@pytest.mark.parametrize("invalid_at", [1, 2])
def test_invalid_sample_clock_value_cannot_break_capture(value, invalid_at):
    probe = Probe(invalid_at=invalid_at, invalid_value=value)
    capture = backend(40, probe=probe)
    assert send(capture, lambda fid: "raw")
    assert capture._error is None and capture.received_trades == 1
    assert capture.queue.packets[0]["real_type"] == "주식체결"
    assert probe.errors


def test_timing_failure_does_not_mask_original_queue_exception():
    primary = RuntimeError("primary queue failure")
    capture = backend(40, probe=Probe(fail_at=2), queue_error=primary)
    with pytest.raises(RuntimeError) as caught:
        send(capture, lambda fid: "raw")
    assert caught.value is primary
    assert capture.received_trades == 0 and capture._error is None


def test_unsampled_diagnostic_callback_uses_no_extra_timing_clocks():
    probe = Probe(fail_at=1, sampled=False)
    capture = backend(40, probe=probe)
    assert send(capture, lambda fid: "raw")
    assert probe.clocks == 0


@pytest.mark.parametrize("before,after,expected", [
    (29.999, 30.001, TRADE_FIDS), (59.999, 60.001, (20, 10, 15)),
    (89.999, 90.001, TRADE_FIDS),
])
def test_phase_is_fixed_for_entire_callback_even_when_clock_crosses_boundary(before, after, expected):
    clock = [before]
    capture = backend(before)
    capture.fid_read_ab._monotonic = lambda: clock[0]
    calls = []
    def read(fid):
        calls.append(fid)
        clock[0] = after
        return "raw"
    assert send(capture, read)
    assert calls == list(expected)
    calls.clear()
    assert send(capture, read)
    assert calls == list((20, 10, 15) if after < 60 else TRADE_FIDS)


def test_pre_subscription_callback_stays_full():
    capture = backend(100)
    capture.fid_read_ab.subscribed_at = None
    calls = []
    assert send(capture, lambda fid: calls.append(fid) or "raw")
    assert calls == list(TRADE_FIDS)
    assert capture.fid_read_ab.snapshot()["trade_callbacks_by_phase"][PHASE_PRE] == 1


@pytest.mark.parametrize("server,scope,controller_present", [
    ("live", None, True), ("live", FEED_SCOPE_DIAGNOSTIC, True),
    ("mock", NORMAL_SCOPE, True), ("mock", FEED_SCOPE_DIAGNOSTIC, False),
])
def test_backend_rejects_invalid_diagnostic_identity_before_os_or_storage(tmp_path, monkeypatch, server, scope, controller_present):
    import collector.kiwoom.live_capture as module
    def forbidden(*args, **kwargs):
        raise AssertionError("OS/filesystem access before diagnostic guard")
    monkeypatch.setattr(module, "WindowsProcess", forbidden)
    monkeypatch.setattr(module, "require_disk_space", forbidden)
    ctl = FidReadAbController() if controller_present else None
    with pytest.raises(ValueError, match="diagnostic|mock-only"):
        LiveRawCapture(tmp_path, server=server, code_revision="fixture", feed_scope=scope, fid_read_ab=ctl)
    assert not list(tmp_path.iterdir())


def test_direct_mock_backend_automatically_persists_diagnostic_scope(tmp_path):
    facts = ProcessFacts(123, "2026-09-23T00:00:00Z", "C:/fixture/python.exe", 32)
    capture = LiveRawCapture(tmp_path, server="mock", code_revision="fixture", facts=facts,
                             fid_read_ab=FidReadAbController())
    try:
        assert capture.identity.feed_scope == FEED_SCOPE_DIAGNOSTIC
    finally:
        assert capture.finish("synthetic completion")
    with read_raw_v2(capture.path) as (manifest, rows):
        list(rows)
    assert manifest["feed_scope"] == FEED_SCOPE_DIAGNOSTIC


def config():
    return dict(source="test", session_id="s", code="005930", venue="unknown", cash=100000,
                max_quote_age_ns=100, buy_latency_ns=5, sell_latency_ns=5,
                cancel_latency_ns=2, fee_rate="0.001")


def raw_file(path, scope=FEED_SCOPE_DIAGNOSTIC, *, populated=True):
    with RawV2Writer(path, source="test", session_id="s", market_date="2026-09-23", feed_scope=scope) as writer:
        if populated:
            for event in fixture_events():
                writer.append(event, received_at_utc=UTC, raw_fields={"issues": []})
        writer.finish(close_ns=20)
    return path


@pytest.mark.parametrize("populated", [False, True])
def test_diagnostic_raw_is_readable_but_research_is_blocked_without_sidecar(tmp_path, populated):
    path = raw_file(tmp_path / "ordinary_name.db", populated=populated)
    before = path.read_bytes()
    with read_raw_v2(path) as (manifest, rows):
        assert len(list(rows)) == (2 if populated else 0)
    assert manifest["feed_scope"] == FEED_SCOPE_DIAGNOSTIC
    assert not list(tmp_path.glob("*.json"))
    with pytest.raises(ValueError, match="diagnostic feed_scope"):
        run_raw_v2(path, output_root=tmp_path / "out", simulator_config=config(), quantity=2)
    assert not (tmp_path / "out").exists()
    assert path.read_bytes() == before


def test_direct_research_rejects_known_diagnostic_provenance_before_iteration_or_output(tmp_path):
    class MustNotIterate:
        def __iter__(self):
            raise AssertionError("research iterator was consumed")
    with pytest.raises(ValueError, match="diagnostic feed_scope"):
        run_research(MustNotIterate(), output_root=tmp_path / "out", dataset_label="synthetic",
                     simulator_config=config(), quantity=2, close_ns=20,
                     input_provenance={"raw_manifest": {"feed_scope": FEED_SCOPE_DIAGNOSTIC}})
    assert not (tmp_path / "out").exists()


def test_raw_entry_guard_precedes_iterator_and_strategy(tmp_path, monkeypatch):
    import collector.raw_v2 as reader_module
    @contextmanager
    def header_only(path):
        class MustNotIterate:
            def __iter__(self):
                raise AssertionError("raw rows were consumed")
        yield {"feed_scope": FEED_SCOPE_DIAGNOSTIC}, MustNotIterate()
    monkeypatch.setattr(reader_module, "read_raw_v2", header_only)
    with pytest.raises(ValueError, match="diagnostic feed_scope"):
        run_raw_v2(tmp_path / "not_opened.db", output_root=tmp_path / "out", simulator_config={}, quantity=1)
    assert not (tmp_path / "out").exists()


def test_research_cli_returns_failure_for_diagnostic_raw(tmp_path, capsys):
    from scripts.run_tick_research import main
    path = raw_file(tmp_path / "raw.db")
    code = main(["--db", str(path), "--output-root", str(tmp_path / "out"),
                 "--code", "005930", "--venue", "unknown", "--quantity", "2", "--cash", "100000",
                 "--fee-rate", "0.001", "--buy-latency-sec", "0", "--sell-latency-sec", "0",
                 "--cancel-latency-sec", "0", "--max-quote-age-sec", "1"])
    assert code == 2
    assert "diagnostic feed_scope" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("scope,eligible", [(FEED_SCOPE_DIAGNOSTIC, False), (NORMAL_SCOPE, True)])
def test_qualification_separates_integrity_from_diagnostic_exclusion(tmp_path, scope, eligible):
    path = raw_file(tmp_path / "raw.db", scope)
    before = path.read_bytes()
    result = qualify_raw_v2(path, output_root=tmp_path / "qualification", expected_session_id="s",
                            closure_evidence="synthetic writer closed in this test")
    report = json.loads(result.read_text(encoding="utf-8"))
    assert report["stream_integrity_verified"] is True
    assert report["research_eligible"] is eligible
    assert report["research_eligibility"]["eligible"] is eligible
    if not eligible:
        assert any("diagnostic feed_scope" in reason for reason in report["research_eligibility"]["reasons"])
    assert "collector/research_input_policy.py" in report["code_provenance"]
    assert path.read_bytes() == before


def test_diagnostic_exclusion_does_not_hide_checksum_failure(tmp_path):
    path = raw_file(tmp_path / "raw.db")
    conn = sqlite3.connect(path)
    try:
        meta = json.loads(conn.execute("SELECT value FROM metadata").fetchone()[0])
        meta["payload_sha256"] = "0" * 64
        conn.execute("UPDATE metadata SET value=?", (json.dumps(meta),))
        conn.commit()
    finally:
        conn.close()
    result = qualify_raw_v2(path, output_root=tmp_path / "qualification", expected_session_id="s",
                            closure_evidence="synthetic corrupt checksum fixture")
    report = json.loads(result.read_text(encoding="utf-8"))
    assert report["stream_integrity_verified"] is False and report["research_eligible"] is False
    assert len(report["research_eligibility"]["reasons"]) == 2


def test_ui_worker_rejects_diagnostic_header_before_scan_and_result(tmp_path, monkeypatch):
    import control_tower.offline_worker as worker
    from control_tower.jobs import JobStore
    from tests.test_control_tower import plan
    path = raw_file(tmp_path / "sampledata" / "raw_ticks_v2" / "synthetic.db")
    job = plan(tmp_path, path)
    store = JobStore(tmp_path)
    store.queue_replay(job)
    original_reader = worker.read_raw_v2
    @contextmanager
    def header_only(path):
        with original_reader(path) as (manifest, rows):
            class MustNotIterate:
                def __iter__(self):
                    raise AssertionError("UI consumed diagnostic rows")
            yield manifest, MustNotIterate()
    monkeypatch.setattr(worker, "read_raw_v2", header_only)
    worker.run_replay_job(tmp_path, job, now=datetime(2026, 9, 23, 18, tzinfo=worker.KST))
    record = store.recent()[0]
    assert record["status"] == "failed" and "diagnostic feed_scope" in record["error"]
    assert not (tmp_path / "research_runs").exists()


def test_design_metadata_discloses_cost_coupling_and_tail():
    meta = sidecar_payload(code_revision="fixture")
    assert meta["pure_com_cost_experiment"] is False
    assert "json_serialization" in meta["co_varying_costs"]
    assert meta["backlog_reset_between_phases"] is False
    assert meta["duration_is_shutdown_request_not_hard_cutoff"] is True
    assert meta["phases"][PHASE_A2]["includes_shutdown_tail_after_nominal_end"] is True
