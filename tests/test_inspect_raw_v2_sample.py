"""Tiny synthetic databases only; no operational capture or full-file replay."""
from dataclasses import replace
import json
import sqlite3

import pytest

from collector.raw_v2 import RawV2Writer, CaptureControl
from collector.kiwoom.tick_normalizer import normalize_tick
from scripts import inspect_raw_v2_sample as reader


UTC = "2026-09-18T00:00:00+00:00"


def create(path, *, closed=True, direction="signed_volume", mismatch=False):
    with RawV2Writer(path, source="kiwoom", session_id="synthetic", market_date="2026-09-18",
                     feed_scope="fixture") as writer:
        for seq in range(1, 5):
            packet = normalize_tick(source="kiwoom", session_id="synthetic", seq=seq,
                received_ns=seq, received_at_utc=UTC, code="005930", venue="unknown",
                real_type="주식체결", fids={"20": "090000", "10": "-10000", "15": "+2"},
                price_policy="signed_magnitude", direction_policy=direction)
            event = replace(packet.event, price=9999) if mismatch and seq == 2 else packet.event
            writer.append(event, received_at_utc=UTC, raw_fields=packet.raw_fields,
                          exchange_ts_raw=packet.exchange_ts_raw, source_time_precision=packet.source_time_precision)
        writer.commit()
        if closed:
            writer.finish(close_ns=10)


def inspect(path, **changes):
    return reader.inspect_sample(path, **(dict(start_seq=2, limit=2) | changes))


def mutate(path, sql, args=()):
    with sqlite3.connect(path) as conn:
        conn.execute(sql, args)


def test_middle_sample_readonly_and_explicitly_partial(tmp_path, monkeypatch):
    path = tmp_path / "raw.db"
    create(path)
    before = path.read_bytes()
    statements = []
    original = sqlite3.connect

    def connect(*args, **kwargs):
        assert "mode=ro" in args[0] and kwargs["uri"]
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(reader.sqlite3, "connect", connect)
    report = inspect(path)
    assert report["status"] == "sample_consistent"
    assert (report["first_seq"], report["last_seq"], report["sampled_records"]) == (2, 3, 2)
    assert report["compared_ticks"] == 2
    assert report["record_counts"] == {"trade": 2}
    assert report["full_integrity_verified"] is False
    assert report["feed_accuracy_verified"] is False
    assert report["replay_performed"] is False
    assert path.read_bytes() == before
    event_reads = [sql for sql in statements if "FROM events" in sql]
    assert len(event_reads) == 1
    assert "WHERE seq>=2 ORDER BY seq LIMIT 2" in event_reads[0]
    assert not any("COUNT(" in sql.upper() for sql in statements)


def test_changed_normalized_price_detected_without_rewriting(tmp_path):
    path = tmp_path / "raw.db"
    create(path, mismatch=True)
    before = path.read_bytes()
    report = inspect(path)
    assert report["status"] == "sample_issues"
    assert report["mismatches"] == [{"seq": 2, "fields": ["price"]}]
    assert path.read_bytes() == before


def test_unknown_direction_remains_quality_issue_even_when_consistent(tmp_path):
    path = tmp_path / "raw.db"
    create(path, direction="unknown")
    report = inspect(path)
    assert report["mismatches"] == []
    assert report["status"] == "sample_issues"
    assert report["quality_issues"] == {"trade_direction_unverified": 2}


def test_outside_sample_corruption_is_not_read_or_certified(tmp_path):
    path = tmp_path / "raw.db"
    create(path)
    mutate(path, "UPDATE events SET payload='broken' WHERE seq=4")
    assert inspect(path)["status"] == "sample_consistent"
    with pytest.raises(ValueError):
        inspect(path, start_seq=4, limit=1)


@pytest.mark.parametrize("start,limit", [(0, 2), (True, 1), (2**63, 1), (1, 0), (1, 1001), (1, True)])
def test_bad_limits_fail_before_open(tmp_path, start, limit):
    with pytest.raises(ValueError):
        inspect(tmp_path / "absent.db", start_seq=start, limit=limit)
    assert not (tmp_path / "absent.db").exists()


def test_incomplete_and_missing_input_rejected(tmp_path):
    path = tmp_path / "raw.db"
    create(path, closed=False)
    with pytest.raises(ValueError, match="incomplete"):
        inspect(path)
    with pytest.raises(FileNotFoundError):
        inspect(tmp_path / "absent.db")


@pytest.mark.parametrize("deleted", [2, 3, 4])
def test_gap_or_missing_tail_is_rejected(tmp_path, deleted):
    path = tmp_path / "raw.db"
    create(path)
    mutate(path, "DELETE FROM events WHERE seq=?", (deleted,))
    with pytest.raises(ValueError):
        inspect(path, limit=3)


def test_nonindexed_table_rejected_before_sample_read(tmp_path):
    path = tmp_path / "raw.db"
    create(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE events RENAME TO old_events")
        conn.execute("CREATE TABLE events AS SELECT * FROM old_events")
    with pytest.raises(ValueError, match="PRIMARY KEY"):
        inspect(path)


@pytest.mark.parametrize("budget,value", [
    ("MAX_PAYLOAD_BYTES", 10), ("MAX_TOTAL_BYTES", 10), ("MAX_SECONDS", -1),
])
def test_budgets_reject_sample(tmp_path, monkeypatch, budget, value):
    path = tmp_path / "raw.db"
    create(path)
    monkeypatch.setattr(reader, budget, value)
    with pytest.raises((ValueError, sqlite3.OperationalError)):
        inspect(path)


def test_empty_and_end_sample_are_distinct(tmp_path):
    path = tmp_path / "raw.db"
    create(path)
    assert inspect(path, start_seq=5)["status"] == "empty_sample"
    report = inspect(path, start_seq=4)
    assert report["status"] == "sample_consistent" and report["sampled_records"] == 1


def test_quality_control_is_reported(tmp_path):
    path = tmp_path / "raw.db"
    with RawV2Writer(path, source="kiwoom", session_id="s", market_date="2026-09-18", feed_scope="fixture") as w:
        w.append(CaptureControl("kiwoom", "s", 1, 1, "parse_error", {"reason": "test"}),
                 received_at_utc=UTC, raw_fields={})
        w.finish(close_ns=2)
    report = inspect(path, start_seq=1)
    assert report["status"] == "sample_issues"
    assert report["quality_issues"] == {"control:parse_error": 1}


def test_cli_exit_codes_and_json(tmp_path, capsys):
    path = tmp_path / "raw.db"
    create(path)
    args = ["--db", str(path), "--start-seq", "2", "--limit", "2"]
    assert reader.main(args) == 0
    assert json.loads(capsys.readouterr().out)["full_integrity_verified"] is False
    assert reader.main(args[:-3] + ["5", "--limit", "2"]) == 2
    capsys.readouterr()
    mutate(path, "UPDATE events SET payload='invalid' WHERE seq=2")
    assert reader.main(args) == 3
    assert "Cannot inspect sample" in capsys.readouterr().err


@pytest.mark.parametrize("field,value,expected", [
    ("is_buy", 1, "is_buy"), ("exchange_ts_raw", "090001", "exchange_ts_raw"),
    ("issues", ["invented"], "raw_fields.issues"),
])
def test_mismatched_types_and_envelope_evidence(tmp_path, field, value, expected):
    path = tmp_path / "raw.db"
    create(path)
    with sqlite3.connect(path) as conn:
        payload = json.loads(conn.execute("SELECT payload FROM events WHERE seq=2").fetchone()[0])
        if field == "is_buy":
            payload["event"][field] = value
        elif field == "issues":
            payload["raw_fields"][field] = value
        else:
            payload[field] = value
        conn.execute("UPDATE events SET payload=? WHERE seq=2", (json.dumps(payload),))
    assert inspect(path)["mismatches"] == [{"seq": 2, "fields": [expected]}]


def test_quote_depth_and_stored_price_policy_are_preserved(tmp_path):
    path = tmp_path / "quotes.db"
    fids = {"21": "090000", "41": "-10001", "51": "-10000"}
    fids.update({str(fid): "5" for fid in range(61, 81)})
    with RawV2Writer(path, source="kiwoom", session_id="s", market_date="2026-09-18", feed_scope="fixture") as w:
        for seq, policy in enumerate(("signed_magnitude", "positive_only"), 1):
            packet = normalize_tick(source="kiwoom", session_id="s", seq=seq, received_ns=seq,
                received_at_utc=UTC, code="005930", venue="unknown", real_type="주식호가잔량",
                fids=fids, price_policy=policy, direction_policy="unknown")
            w.append(packet.event, received_at_utc=UTC, raw_fields=packet.raw_fields,
                     exchange_ts_raw=packet.exchange_ts_raw, source_time_precision=packet.source_time_precision)
        w.finish(close_ns=3)
    report = inspect(path, start_seq=1)
    assert report["compared_ticks"] == 2 and report["mismatches"] == []
    assert report["record_counts"] == {"quote": 2}
    assert report["quality_issues"] == {"out_of_range_fid_41": 1, "out_of_range_fid_51": 1}


def test_unsupported_normalization_is_not_silently_approved(tmp_path):
    path = tmp_path / "raw.db"
    create(path)
    mutate(path, "UPDATE events SET payload=replace(payload, 'kiwoom_fids_prototype_1', 'other') WHERE seq=2")
    report = inspect(path)
    assert report["status"] == "sample_issues"
    assert report["quality_issues"] == {"unsupported_normalization": 1}
    assert report["compared_ticks"] == 1


def test_oversized_manifest_rejected(tmp_path):
    path = tmp_path / "raw.db"
    create(path)
    mutate(path, "UPDATE metadata SET value=?", (" " * (reader.MAX_MANIFEST_BYTES + 1),))
    with pytest.raises(ValueError, match="bounded manifest"):
        inspect(path)
