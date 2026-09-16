from dataclasses import replace
import json
import sqlite3

import pytest

from collector.raw_v2 import RawV2Writer, read_raw_v2
from engine.tick_ordering import OrderedTick
from engine.tick_research_run import run_raw_v2, ResearchRunFailed


def event(seq=1, **changes):
    q = OrderedTick("test", "s", seq, 0, "005930", "unknown", "quote",
                    bid=10000, ask=10001, bid_size=10, ask_size=3,
                    market_second=32399, bid_sizes=(10, 10, 10), ask_sizes=(3, 3, 3))
    return replace(q, **changes)


def writer(path):
    return RawV2Writer(path, source="test", session_id="s", market_date="2026-09-16", feed_scope="fixture")


def append(w, e):
    w.append(e, received_at_utc="2026-09-16T00:00:00+00:00", raw_fields={"original": "-10001"})


def create(path):
    with writer(path) as w:
        append(w, event())
        append(w, event(2, received_ns=1, kind="trade", price=10001, volume=30,
                        is_buy=True, market_second=32400))
        w.finish(close_ns=20)


def read(path):
    with read_raw_v2(path) as (meta, records):
        return meta, list(records)


def test_roundtrip_preserves_raw_values_sequence_and_tuple_depth(tmp_path):
    path = tmp_path / "v2.db"
    create(path)
    before = path.read_bytes()
    meta, records = read(path)
    assert meta["state"] == "closed" and meta["event_count"] == 2
    assert records[0]["event"] == event()
    assert records[0]["raw_fields"] == {"original": "-10001"}
    assert path.read_bytes() == before


def test_never_overwrite_existing_file(tmp_path):
    path = tmp_path / "v2.db"
    create(path)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        writer(path)
    assert path.read_bytes() == before


def test_incomplete_committed_capture_is_preserved_but_rejected(tmp_path):
    path = tmp_path / "v2.db"
    with writer(path) as w:
        append(w, event())
        w.commit()
    with pytest.raises(ValueError, match="incomplete"):
        read(path)
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT seq FROM events").fetchone() == (1,)


@pytest.mark.parametrize("bad", [event(2), event(source="other"), event(received_ns=-1)])
def test_bad_event_prevents_finish(tmp_path, bad):
    with writer(tmp_path / "v2.db") as w:
        with pytest.raises(ValueError):
            append(w, bad)
        with pytest.raises(ValueError):
            w.finish(close_ns=20)


def test_receipt_utc_must_be_explicit(tmp_path):
    with writer(tmp_path / "v2.db") as w:
        with pytest.raises(ValueError):
            w.append(event(), received_at_utc="2026-09-16T09:00:00", raw_fields={})


def test_checksum_tampering_rejected(tmp_path):
    path = tmp_path / "v2.db"
    create(path)
    with sqlite3.connect(path) as c:
        c.execute("UPDATE events SET payload=replace(payload, '-10001', '-10002') WHERE seq=1")
    with pytest.raises(ValueError, match="checksum"):
        read(path)


def config():
    return dict(source="test", session_id="s", code="005930", venue="unknown", cash=100000,
                max_quote_age_ns=100, buy_latency_ns=5, sell_latency_ns=5,
                cancel_latency_ns=2, fee_rate="0.001")


def test_closed_file_runs_through_saved_research_result(tmp_path):
    path = tmp_path / "v2.db"
    create(path)
    result = run_raw_v2(path, output_root=tmp_path / "results", simulator_config=config(), quantity=2)
    saved = json.loads(result.read_bytes())
    assert saved["status"] == "completed_with_open_position"
    assert saved["input_provenance"]["raw_manifest"]["event_count"] == 2


def test_late_raw_checksum_error_marks_replay_failed(tmp_path):
    path = tmp_path / "v2.db"
    create(path)
    with sqlite3.connect(path) as c:
        c.execute("UPDATE events SET payload=replace(payload, '-10001', '-10002') WHERE seq=2")
    with pytest.raises(ResearchRunFailed) as exc:
        run_raw_v2(path, output_root=tmp_path / "results", simulator_config=config(), quantity=2)
    assert json.loads(exc.value.path.read_bytes())["status"] == "failed"


def test_missing_input_does_not_create_database(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        read(path)
    assert not path.exists()
