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


@pytest.mark.parametrize("field,value", [("market_date", "bad-date"), ("source", 123),
                                        ("session_id", " "), ("feed_scope", None)])
def test_invalid_manifest_identity_is_rejected(tmp_path, field, value):
    path = tmp_path / "v2.db"
    create(path)
    with sqlite3.connect(path) as c:
        meta = json.loads(c.execute("SELECT value FROM metadata").fetchone()[0])
        meta[field] = value
        c.execute("UPDATE metadata SET value=?", (json.dumps(meta),))
    with pytest.raises(ValueError):
        read(path)


@pytest.mark.parametrize("field,value", [("source_time_precision", ""),
                                        ("exchange_ts_raw", 123), ("raw_fields", None)])
def test_invalid_envelope_fields_rejected(tmp_path, field, value):
    path = tmp_path / "v2.db"
    create(path)
    with sqlite3.connect(path) as c:
        data = json.loads(c.execute("SELECT payload FROM events WHERE seq=1").fetchone()[0])
        data[field] = value
        c.execute("UPDATE events SET payload=? WHERE seq=1", (json.dumps(data),))
    with pytest.raises(ValueError):
        read(path)


def test_nonempty_dataset_wrong_selection_is_not_empty_input(tmp_path):
    path = tmp_path / "v2.db"
    create(path)
    result = run_raw_v2(path, output_root=tmp_path / "results",
                        simulator_config=config() | {"code": "000660"}, quantity=2)
    assert json.loads(result.read_bytes())["status"] == "completed_no_selected_events"


def test_cli_closed_fixture_to_result_end_to_end(tmp_path, capsys):
    from scripts.run_tick_research import main
    path = tmp_path / "v2.db"
    create(path)
    assert main(["--db", str(path), "--output-root", str(tmp_path / "results"),
                 "--code", "005930", "--venue", "unknown", "--quantity", "2", "--cash", "100000",
                 "--fee-rate", "0.001", "--buy-latency-sec", "0.000000005",
                 "--sell-latency-sec", "0.000000005", "--cancel-latency-sec", "0.000000002",
                 "--max-quote-age-sec", "0.0000001"]) == 0
    from pathlib import Path
    result = json.loads(Path(capsys.readouterr().out.strip()).read_bytes())
    assert result["status"] == "completed_with_open_position"


@pytest.mark.parametrize("operation", ["commit", "finish"])
def test_commit_failure_never_leaves_a_closed_replayable_file(tmp_path, operation):
    path = tmp_path / "v2.db"
    with writer(path) as w:
        append(w, event())
        original = w.conn
        class FailCommit:
            def execute(self, *args):
                return original.execute(*args)
            def commit(self):
                raise sqlite3.OperationalError("synthetic disk failure")
            def close(self):
                original.close()
        w.conn = FailCommit()
        with pytest.raises(sqlite3.OperationalError):
            w.commit() if operation == "commit" else w.finish(close_ns=20)
        assert w.failed and not w.finished
        with pytest.raises(ValueError):
            w.finish(close_ns=20)
    with pytest.raises(ValueError, match="incomplete"):
        read(path)
