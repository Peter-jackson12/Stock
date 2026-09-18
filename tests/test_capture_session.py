import json

import pytest

from collector.kiwoom.capture_session import CaptureSession
from collector.raw_v2 import read_raw_v2, CaptureControl
from engine.tick_research_run import run_raw_v2, ResearchRunFailed


UTC = "2026-09-16T00:00:00+00:00"


def capture(path, **changes):
    return CaptureSession(path, **(dict(source="fixture", session_id="s", market_date="2026-09-16",
                          feed_scope="fixture", price_policy="signed_magnitude", direction_policy="signed_volume",
                          started_ns=0, started_at_utc=UTC) | changes))


def quote(c, ns=1):
    return c.on_tick(code="005930", venue="unknown", real_type="주식호가잔량", received_ns=ns,
                     received_at_utc=UTC, fids={"21": "090000", "41": "10001", "51": "10000",
                     **{str(i): "3" for i in range(61, 71)}, **{str(i): "10" for i in range(71, 81)}})


def trade(c, ns=2, volume="+30"):
    return c.on_tick(code="005930", venue="unknown", real_type="주식체결", received_ns=ns,
                     received_at_utc=UTC, fids={"20": "090000", "10": "10001", "15": volume})


def read(path):
    with read_raw_v2(path) as (meta, records):
        return meta, list(records)


def replay(path, root):
    config = dict(source="fixture", session_id="s", code="005930", venue="unknown", cash=100000,
                  max_quote_age_ns=100, buy_latency_ns=5, sell_latency_ns=5, cancel_latency_ns=2, fee_rate="0.001")
    return run_raw_v2(path, output_root=root, simulator_config=config, quantity=2)


def test_common_sequence_includes_control_and_complete_chain(tmp_path):
    path = tmp_path / "capture.db"
    with capture(path) as c:
        quote(c)
        trade(c)
        c.finish(20)
    meta, records = read(path)
    assert meta["schema"] == "raw_v2_prototype_2"
    assert [r["event"].seq for r in records] == [1, 2, 3]
    assert isinstance(records[0]["event"], CaptureControl)
    result = json.loads(replay(path, tmp_path / "results").read_bytes())
    assert result["open_quantity"] == 2


def test_disconnect_preserved_and_dataset_replay_fails(tmp_path):
    path = tmp_path / "capture.db"
    with capture(path) as c:
        quote(c)
        trade(c)
        c.control("disconnect", 3, UTC, {"reason": "fixture"})
        with pytest.raises(ValueError, match="new session"):
            trade(c, ns=4)
        c.finish(20)
    assert read(path)[1][-1]["event"].control_type == "disconnect"
    with pytest.raises(ResearchRunFailed) as exc:
        replay(path, tmp_path / "results")
    saved = json.loads(exc.value.path.read_bytes())
    assert saved["status"] == "failed" and "disconnect" in saved["error"]


def test_parse_failure_preserves_raw_trade_then_quality_event(tmp_path):
    path = tmp_path / "capture.db"
    with capture(path) as c:
        trade(c, ns=1, volume="bad")
        c.finish(20)
    records = read(path)[1]
    assert records[1]["event"].volume is None
    assert records[1]["raw_fields"]["fids"]["15"] == "bad"
    assert records[2]["event"].control_type == "parse_error"


@pytest.mark.parametrize("selected_code", ["005930", "000660"])
def test_unsigned_volume_stays_unknown_and_blocks_research(tmp_path, selected_code):
    # Shape observed in a bounded 2026-09-18 sample; synthetic capture only.
    path = tmp_path / "unsigned.db"
    with capture(path) as c:
        trade(c, ns=1, volume=" 52244")
        c.finish(20)
    records = read(path)[1]
    tick, error = records[1], records[2]["event"]
    assert tick["raw_fields"]["fids"]["15"] == " 52244"
    assert tick["event"].volume == 52244 and tick["event"].is_buy is None
    assert error.control_type == "parse_error"
    assert error.details["issues"] == ["trade_direction_unverified"]
    config = dict(source="fixture", session_id="s", code=selected_code, venue="unknown", cash=100000,
                  max_quote_age_ns=100, buy_latency_ns=5, sell_latency_ns=5, cancel_latency_ns=2, fee_rate="0.001")
    with pytest.raises(ResearchRunFailed) as caught:
        run_raw_v2(path, output_root=tmp_path / "results", simulator_config=config, quantity=2)
    result = json.loads(caught.value.path.read_bytes())
    assert result["status"] == "failed" and result["diagnostics_only"] is True
    assert result["input_complete"] is False
    if selected_code == "000660":
        assert result["event_count"] == 0
        assert "dataset quality event parse_error" in result["error"]


def test_unsupported_callback_is_recorded_and_interrupts(tmp_path):
    path = tmp_path / "capture.db"
    with capture(path) as c:
        assert c.on_tick(code="005930", venue="unknown", real_type="unexpected", fids={"x": "raw"},
                         received_ns=1, received_at_utc=UTC) is None
        assert c.state == "interrupted"
        c.finish(20)
    error = read(path)[1][-1]["event"]
    assert error.control_type == "callback_error" and error.details["fids"] == {"x": "raw"}


def test_context_exit_without_finish_is_not_complete(tmp_path):
    path = tmp_path / "capture.db"
    with capture(path) as c:
        quote(c)
        c.commit()
    assert c.state == "incomplete"
    with pytest.raises(ValueError, match="not writable"):
        c.commit()
    with pytest.raises(ValueError, match="incomplete"):
        read(path)


def test_new_session_can_restart_sequence_in_new_file(tmp_path):
    for name in ("one", "two"):
        path = tmp_path / f"{name}.db"
        with capture(path, session_id=name) as c:
            c.finish(20)
        assert read(path)[1][0]["event"].seq == 1


def test_control_clock_reversal_prevents_finish(tmp_path):
    with capture(tmp_path / "capture.db") as c:
        quote(c, ns=5)
        with pytest.raises(ValueError, match="clock"):
            c.control("session_note", 4, UTC, {})
        with pytest.raises(ValueError):
            c.finish(20)


def test_unknown_control_does_not_finalize_false_success(tmp_path):
    with capture(tmp_path / "capture.db") as c:
        with pytest.raises(ValueError):
            c.control("unknown", 1, UTC, {})
        assert c.state == "failed"


def test_overflow_signal_is_preserved_as_bad_quality(tmp_path):
    path = tmp_path / "capture.db"
    with capture(path) as c:
        c.control("queue_overflow", 1, UTC, {"lost_events": 1})
        c.finish(20)
    with pytest.raises(ResearchRunFailed):
        replay(path, tmp_path / "results")


def test_signed_volume_removes_only_direction_quality_records(tmp_path):
    errors = {}
    for policy in ("unknown", "signed_volume"):
        path = tmp_path / f"{policy}.db"
        with capture(path, direction_policy=policy) as c:
            trade(c, ns=1, volume="+30")
            trade(c, ns=2, volume="-30")
            c.on_tick(code="005930", venue="unknown", real_type="주식체결",
                      received_ns=3, received_at_utc=UTC,
                      fids={"10": "bad", "15": "+30", "20": "bad"})
            trade(c, ns=4, volume="0")
            c.finish(20)
        records = read(path)[1]
        errors[policy] = [r["event"].details["issues"] for r in records
                          if isinstance(r["event"], CaptureControl)
                          and r["event"].control_type == "parse_error"]
        assert [r["raw_fields"]["fids"]["15"] for r in records
                if not isinstance(r["event"], CaptureControl)] == ["+30", "-30", "+30", "0"]
        with pytest.raises(ResearchRunFailed):
            replay(path, tmp_path / f"results_{policy}")
    assert len(errors["unknown"]) == 4
    assert len(errors["signed_volume"]) == 2
    assert errors["signed_volume"][0] == ["invalid_or_missing_fid_20", "invalid_or_missing_fid_10"]
    assert errors["signed_volume"][1] == ["out_of_range_fid_15", "trade_direction_unverified"]
