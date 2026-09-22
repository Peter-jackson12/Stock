"""작은 합성 데이터만 사용. native feed 지연/실부하 검증은 아니다."""
import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from collector.kiwoom.capture_telemetry import CaptureTelemetry, clock_difference, observe
from collector.kiwoom.capture_session import CaptureSession
from collector.kiwoom.live_capture import LiveRawCapture, TRADE_FIDS, QUOTE_FIDS
from tests.test_live_collector import live
from tests.test_tick_collector_shutdown import collector


class Clock:
    now = 0
    def __call__(self):
        return self.now


@pytest.fixture
def probe(tmp_path):
    clock = Clock()
    telemetry = CaptureTelemetry(clock_ns=clock)
    assert telemetry.bind(tmp_path, session_id="fixture", code_revision="fixture")
    yield telemetry, clock
    telemetry.close()


def rows(probe):
    return [json.loads(line) for line in probe.path.read_text(encoding="utf-8").splitlines()]


def sample(probe, *, real_type="주식체결", raw_clock="090001", received_ns=0,
           received_at_utc="2026-09-22T00:00:03.250000Z"):
    probe.callback_sample(code="005930", real_type=real_type,
        fids={"20": raw_clock, "21": raw_clock}, received_ns=received_ns,
        received_at_utc=received_at_utc, accepted=True, finished_ns=received_ns + 100)


@pytest.mark.parametrize("value", [None, "", "90000", "240000", "126000", "120060", "１２３４５６", 90000, "bad"])
def test_invalid_exchange_clock_is_not_certified(value):
    result = clock_difference(value, "2026-09-22T00:00:03Z")
    assert result["status"] == "invalid_source_clock"
    assert result["difference_seconds"] is None


@pytest.mark.parametrize("receive", [None, "bad", "2026-09-22T09:00:03", "2026-09-22T99:00:00Z"])
def test_invalid_receive_clock_is_not_silently_localized(receive):
    assert clock_difference("090000", receive)["status"] == "invalid_receive_clock"


@pytest.mark.parametrize("clock,receive,difference", [
    (" 090001 ", "2026-09-22T00:00:03.250000Z", 2.25),
    ("090005", "2026-09-22T09:00:03+09:00", -2),
    ("235959", "2026-09-21T15:00:01Z", -86398),
])
def test_clock_difference_retains_sign_and_explicit_day_assumption(clock, receive, difference):
    result = clock_difference(clock, receive)
    assert result["difference_seconds"] == difference
    assert result["same_day_kst_assumption"] is True and result["not_network_latency"] is True
    assert result["status"] == "unverified_clock_difference"


def test_sampling_has_per_kind_time_gate_without_io(probe, monkeypatch):
    telemetry, _ = probe
    size = telemetry.path.stat().st_size
    def forbidden(*args, **kwargs):
        raise AssertionError("hot path must not serialize or write")
    with monkeypatch.context() as patch:
        patch.setattr(telemetry, "_write", forbidden)
        patch.setattr(json, "dumps", forbidden)
        assert telemetry.reserve_sample("주식체결", 0)
        sample(telemetry)
        assert telemetry.reserve_sample("주식호가잔량", 0)
        sample(telemetry, real_type="주식호가잔량")
        assert not telemetry.reserve_sample("주식체결", 4_999_999_999)
        assert telemetry.reserve_sample("주식체결", 5_000_000_000)
        assert not telemetry.reserve_sample("unknown", 9_000_000_000)
    assert telemetry.path.stat().st_size == size
    assert telemetry.flush()
    assert len(rows(telemetry)[-1]["samples"]) == 2


def test_flush_interval_and_force_boundary(probe):
    telemetry, clock = probe
    assert telemetry.flush()
    clock.now = telemetry.FLUSH_NS - 1
    assert not telemetry.flush()
    clock.now += 1
    assert telemetry.flush()
    assert telemetry.flush(force=True)
    assert telemetry.batches_written == 3


def test_pending_budget_and_contention_do_not_block_producer(probe):
    telemetry, _ = probe
    for _ in range(telemetry.MAX_PENDING + 3):
        sample(telemetry)
    assert len(telemetry._samples) == telemetry.MAX_PENDING
    assert telemetry.samples_dropped == 3
    with telemetry._samples_lock:
        sample(telemetry)
    assert telemetry.samples_dropped == 4
    assert telemetry.flush()
    data = rows(telemetry)[-1]
    assert data["diagnostic_samples_dropped"] == 4
    assert len(data["samples"]) == telemetry.MAX_PENDING


def test_raw_text_truncation_is_explicit_not_a_clock_value(probe):
    telemetry, _ = probe
    sample(telemetry, raw_clock="090001" + "x" * 100)
    assert telemetry.flush()
    data = rows(telemetry)[-1]["samples"][0]
    assert data["source_clock_truncated"] and len(data["source_clock_raw"]) == 32
    assert data["clock_comparison"]["status"] == "truncated"
    assert data["clock_comparison"]["difference_seconds"] is None


def test_enter_without_return_is_observable_from_stats_thread(probe):
    telemetry, clock = probe
    clock.now = 100
    token = telemetry.poll_enter()
    telemetry.connection_observed(token, "1")
    clock.now = 300
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(telemetry.flush()))
    thread.start()
    thread.join(2)
    assert not thread.is_alive() and outcome == [True]
    data = rows(telemetry)[-1]["poll"]
    assert data["entries"] == 1 and data["returns"] == 0 and data["in_flight"] == 1
    assert data["return_age_ns"] is None and data["entry_age_ns"] == 200
    assert data["connection"]["value"] == "1"
    assert data["connection"]["value_type"] == "str"
    telemetry.poll_return(token, outcome="returned")
    assert telemetry.poll_snapshot(400)["returns"] == 1


def test_nested_polls_keep_separate_entry_return_counts(probe):
    telemetry, clock = probe
    one = telemetry.poll_enter()
    clock.now = 10
    two = telemetry.poll_enter()
    telemetry.poll_return(two, outcome="raised")
    assert telemetry.poll_snapshot(20)["in_flight"] == 1
    telemetry.poll_return(one, outcome="returned")
    state = telemetry.poll_snapshot(20)
    assert state["entries"] == state["returns"] == 2 and state["in_flight"] == 0
    assert state["last_return_id"] == one[0]


@pytest.mark.parametrize("value,kind,saved", [(1, "int", 1), ("1", "str", "1"), (None, "NoneType", None), (True, "bool", True), (1.0, "float", None)])
def test_actual_connection_value_type_is_preserved(probe, value, kind, saved):
    telemetry, _ = probe
    token = telemetry.poll_enter()
    telemetry.connection_observed(token, value)
    observed = telemetry.poll_snapshot(0)["connection"]
    assert observed["value_type"] == kind and observed["value"] == saved


def test_wrong_thread_cannot_create_poll_or_callback_samples(probe):
    telemetry, _ = probe
    result = []
    def worker():
        result.append(telemetry.poll_enter())
        result.append(telemetry.reserve_sample("주식체결", 0))
        sample(telemetry)
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(2)
    assert not thread.is_alive() and result == [None, False]
    assert not telemetry._samples and telemetry.poll_snapshot(0)["entries"] == 0


def test_existing_file_is_not_overwritten(tmp_path):
    path = tmp_path / "capture_telemetry.jsonl"
    path.write_bytes(b"preserve")
    telemetry = CaptureTelemetry()
    assert not telemetry.bind(tmp_path, session_id="new", code_revision="fixture")
    assert telemetry.error == "bind:FileExistsError"
    assert path.read_bytes() == b"preserve"
    assert telemetry.close()


@pytest.mark.parametrize("limit", ["bytes", "batches"])
def test_exhaustion_stops_diagnostics_not_previous_evidence(probe, limit):
    telemetry, _ = probe
    before = telemetry.path.read_bytes()
    if limit == "bytes":
        telemetry.MAX_BYTES = len(before)
    else:
        telemetry.MAX_BATCHES = 0
    assert not telemetry.flush()
    assert telemetry.error
    assert telemetry.path.read_bytes() == before


def test_io_error_and_closed_state_are_contained(probe, monkeypatch):
    telemetry, _ = probe
    def broken(*args):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(telemetry, "_write", broken)
    assert not telemetry.flush()
    assert telemetry.error == "flush:OSError"
    assert telemetry.close() and telemetry.close()
    assert not telemetry.reserve_sample("주식체결", 1)
    assert not telemetry.flush()


def test_observer_exception_never_escapes():
    class Broken:
        def disable(self, reason):
            raise RuntimeError("also broken")
        def reserve_sample(self, *args):
            raise OSError("broken")
    assert observe(Broken(), "reserve_sample", "주식체결", 0) is None
    assert observe(None, "anything") is None


def backend(probe=None, *, queue_error=False):
    packets = []
    obj = LiveRawCapture.__new__(LiveRawCapture)
    obj.telemetry = probe
    obj.received_trades = obj.received_quotes = 0
    obj._error = None
    def submit(**packet):
        if queue_error:
            raise ValueError("original queue failure")
        packets.append(copy.deepcopy(packet))
        return True
    obj.queue = SimpleNamespace(submit_tick=submit)
    return obj, packets


def produce(obj, *, unsigned=False):
    calls = []
    def read(fid):
        calls.append(fid)
        if fid in (20, 21):
            return "090001"
        if fid == 15:
            return "7" if unsigned else "+7"
        return "+10001"
    for number, kind in enumerate(("주식체결", "주식호가잔량", "주식체결"), 1):
        assert obj.on_tick("005930", kind, read, received_ns=number * 5_000_000_000,
                          received_at_utc="2026-09-22T00:00:03Z")
    return calls


@pytest.mark.parametrize("unsigned", [False, True])
def test_enabled_disabled_payloads_and_fid_call_counts_are_identical(probe, tmp_path, unsigned):
    telemetry, _ = probe
    plain, plain_packets = backend()
    watched, watched_packets = backend(telemetry)
    expected = list(TRADE_FIDS) + list(QUOTE_FIDS) + list(TRADE_FIDS)
    assert produce(plain, unsigned=unsigned) == expected
    assert produce(watched, unsigned=unsigned) == expected
    assert watched_packets == plain_packets
    assert watched.received_trades == plain.received_trades == 2
    assert watched.received_quotes == plain.received_quotes == 1
    hashes = []
    for label, packets in (("plain", plain_packets), ("watched", watched_packets)):
        with CaptureSession(tmp_path / label / "raw.db", source="fixture", session_id="same",
                market_date="2026-09-22", feed_scope="synthetic", price_policy="signed_magnitude",
                direction_policy="signed_volume", started_ns=0,
                started_at_utc="2026-09-22T00:00:00Z") as session:
            for packet in packets:
                session.on_tick(**packet)
            session.finish(20_000_000_000)
            hashes.append((session.writer.meta["event_count"], session.writer.meta["payload_sha256"]))
    assert hashes[0] == hashes[1]
    assert telemetry.flush()
    assert all(packet["venue"] == "unknown" for packet in watched_packets)


def test_diagnostic_failure_keeps_original_queue_exception(probe, monkeypatch):
    telemetry, _ = probe
    obj, _ = backend(telemetry, queue_error=True)
    def failed(**kwargs):
        raise OSError("sample failure")
    monkeypatch.setattr(telemetry, "callback_sample", failed)
    with pytest.raises(ValueError, match="original queue failure"):
        obj.on_tick("005930", "주식체결", lambda fid: "+1", received_ns=5_000_000_000,
                    received_at_utc="2026-09-22T00:00:00Z")
    assert obj.received_trades == 0 and telemetry.error


def test_existing_fid_read_error_is_preserved_with_telemetry(probe):
    telemetry, _ = probe
    obj, packets = backend(telemetry)
    def read(fid):
        if fid == 15:
            raise RuntimeError("synthetic fid failure")
        return "090001" if fid == 20 else "+1"
    assert obj.on_tick("005930", "주식체결", read, received_ns=5_000_000_000,
                       received_at_utc="2026-09-22T00:00:00Z")
    assert packets[0]["real_type"] == "callback_error"
    assert "RuntimeError" in packets[0]["fids"]["_read_error"]
    assert obj._error and obj.received_trades == 1


def attach(logger):
    telemetry = CaptureTelemetry()
    logger.telemetry = telemetry
    return telemetry


@pytest.mark.parametrize("connection", [1, 0, "bad"])
def test_real_poll_path_keeps_connection_and_shutdown_behavior(live, connection):
    logger, _, _ = live
    telemetry = attach(logger)
    logger._on_login(0)
    original = logger.ocx.dynamicCall
    calls = []
    def call(method, *args):
        if method == "GetConnectState()":
            calls.append(method)
            return connection
        return original(method, *args)
    logger.ocx.dynamicCall = call
    logger._poll_control()
    assert calls == ["GetConnectState()"]
    state = telemetry.poll_snapshot(telemetry.clock_ns())
    assert state["entries"] == state["returns"] == 1
    assert state["connection"]["value"] == connection
    if connection == 1:
        assert not logger._shutdown_done
    else:
        assert logger._shutdown_done and logger.exit_code == 2
        assert logger.raw_capture.queue.snapshot()["state"] == "interrupted"
    logger._shutdown("cleanup")
    telemetry.close()


def test_broken_poll_observer_does_not_interrupt_collection(live, monkeypatch):
    logger, _, _ = live
    telemetry = attach(logger)
    logger._on_login(0)
    def broken(*args):
        raise OSError("observation failed")
    monkeypatch.setattr(telemetry, "connection_observed", broken)
    logger._poll_control()
    logger._on_receive_real_data("005930", "주식체결", "")
    assert not logger._shutdown_done and logger.raw_capture.received_trades == 1
    assert telemetry.error and logger.raw_capture.error is None
    logger._shutdown("cleanup")
    telemetry.close()


def test_default_and_cli_validation(collector, monkeypatch):
    logger, _, _ = collector
    parser = logger.start.__globals__["parse_collector_args"]
    assert logger.telemetry is None
    assert parser([])[0].capture_telemetry is False
    assert parser(["--capture-telemetry"])[0].capture_telemetry is True
    for args in (("--storage", "raw-v1"), ("--aftermarket-nxt-codes", "005930_NX")):
        with pytest.raises(SystemExit):
            parser(["--capture-telemetry", *args])
    def forbidden(*args):
        raise AssertionError("Qt construction forbidden")
    monkeypatch.setitem(logger.__init__.__globals__, "QApplication", forbidden)
    with pytest.raises(ValueError, match="single raw-v2"):
        type(logger)(code_revision="fixture", storage="raw-v1", capture_telemetry=True)
    with pytest.raises(ValueError, match="single raw-v2"):
        type(logger)(code_revision="fixture", aftermarket_plan=object(), capture_telemetry=True)


def test_shutdown_finally_closes_last_poll_evidence(live):
    logger, _, _ = live
    telemetry = attach(logger)
    logger._on_login(0)
    logger._shutdown_requested = "fixture"
    logger._poll_control()
    logger._finish_process_resources()
    assert telemetry.closed
    data = rows(telemetry)[-1]
    assert data["poll"]["entries"] == data["poll"]["returns"] == 1
    status = json.loads((logger.raw_capture.directory / "status.json").read_text(encoding="utf-8"))
    assert status["control_heartbeat"] is False
    assert status["snapshot"]["data_quality"] == "unverified"
