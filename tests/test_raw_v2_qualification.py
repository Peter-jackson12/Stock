import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from collector.kiwoom.capture_session import CaptureSession
from collector.raw_v2 import read_raw_v2
from collector.raw_v2_qualification import qualify_raw_v2
from scripts.qualify_raw_v2 import main


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows sealed immutable reader")
UTC = "2026-09-21T00:00:00Z"


def capture(path, *, finish=True, unsigned=False):
    with CaptureSession(path, source="fixture", session_id="s", market_date="2026-09-21",
                        feed_scope="fixture", price_policy="signed_magnitude",
                        direction_policy="signed_volume", started_ns=0,
                        started_at_utc=UTC) as session:
        session.on_tick(code="005930", venue="unknown", real_type="주식체결",
                        fids={"10": "+10001", "15": "30" if unsigned else "+30",
                              "20": "090001"},
                        received_ns=1, received_at_utc="2026-09-21T00:00:01Z")
        session.commit()
        if finish:
            session.finish(10)


def report(path, tmp_path):
    result = qualify_raw_v2(path, output_root=tmp_path / "qualification",
                            expected_session_id="s",
                            closure_evidence="synthetic fixture writer closed")
    return result, json.loads(result.read_text(encoding="utf-8"))


def snapshot(directory):
    return {item.name: (item.stat().st_size, item.stat().st_mtime_ns,
                        hashlib.sha256(item.read_bytes()).hexdigest())
            for item in directory.iterdir() if item.is_file()}


def test_clean_closed_raw_is_fully_verified_and_eligible(tmp_path):
    path = tmp_path / "source" / "raw.db"
    capture(path)
    before = snapshot(path.parent)
    result, data = report(path, tmp_path)
    assert result.exists()
    assert data["scan_complete"] is True
    assert data["stream_integrity_verified"] is True
    assert data["research_eligible"] is True
    assert data["counts"] == {
        "raw_records": 2, "tick_records": 1, "trade_records": 1,
        "quote_records": 0, "control_records": 1,
        "control_by_type": {"session_start": 1}}
    assert data["quality_diagnostics"]["logical_issue_counts"] == {}
    assert data["original_preservation"]["source_stat_unchanged"] is True
    assert snapshot(path.parent) == before


def test_unsigned_trade_and_mirrored_parse_error_are_one_logical_issue(tmp_path):
    path = tmp_path / "source" / "raw.db"
    capture(path, unsigned=True)
    before = snapshot(path.parent)
    _, data = report(path, tmp_path)
    quality = data["quality_diagnostics"]
    assert data["stream_integrity_verified"] is True
    assert data["research_eligible"] is False
    assert data["counts"]["raw_records"] == 3
    assert quality["affected_tick_records"] == 1
    assert quality["parse_error_records"] == 1
    assert quality["paired_parse_error_records"] == 1
    assert quality["unpaired_parse_error_records"] == 0
    assert quality["disqualifying_control_records"] == 0
    assert quality["normalized_issue_counts"] == {"trade_direction_unverified": 1}
    assert quality["parse_error_reason_counts"] == {"trade_direction_unverified": 1}
    assert quality["logical_issue_counts"] == {"trade_direction_unverified": 1}
    assert snapshot(path.parent) == before


def test_multiple_reasons_are_counted_without_double_counting_control_mirrors(tmp_path):
    path = tmp_path / "source" / "raw.db"
    with CaptureSession(path, source="fixture", session_id="s", market_date="2026-09-21",
                        feed_scope="fixture", price_policy="signed_magnitude",
                        direction_policy="signed_volume", started_ns=0,
                        started_at_utc=UTC) as session:
        session.on_tick(code="005930", venue="unknown", real_type="주식체결",
                        fids={"10": "bad", "15": "+30", "20": "bad"},
                        received_ns=1, received_at_utc="2026-09-21T00:00:01Z")
        session.on_tick(code="000660", venue="unknown", real_type="주식체결",
                        fids={"10": "+100", "15": "0", "20": "090002"},
                        received_ns=2, received_at_utc="2026-09-21T00:00:02Z")
        session.finish(10)
    _, data = report(path, tmp_path)
    quality = data["quality_diagnostics"]
    expected = {"invalid_or_missing_fid_10": 1, "invalid_or_missing_fid_20": 1,
                "out_of_range_fid_15": 1, "trade_direction_unverified": 1}
    assert data["stream_integrity_verified"] is True
    assert data["research_eligible"] is False
    assert quality["normalized_issue_counts"] == expected
    assert quality["parse_error_reason_counts"] == expected
    assert quality["logical_issue_counts"] == expected
    assert quality["affected_tick_records"] == 2
    assert quality["paired_parse_error_records"] == 2
    assert quality["disqualifying_control_records"] == 0
    assert quality["affected_code_count"] == 2
    assert sum(item["count"] for item in quality["top_problem_codes"]) == 2
    assert sum(quality["received_utc_5minute_buckets"].values()) == 2


@pytest.mark.parametrize("damage", ["gap", "payload"])
def test_structural_damage_never_becomes_complete_or_eligible(tmp_path, damage):
    path = tmp_path / "source" / "raw.db"
    capture(path)
    with sqlite3.connect(path) as conn:
        if damage == "gap":
            conn.execute("DELETE FROM events WHERE seq=1")
        else:
            conn.execute("UPDATE events SET payload=replace(payload, '+10001', '+10002') WHERE seq=2")
    _, data = report(path, tmp_path)
    assert data["status"] == "failed"
    assert data["scan_complete"] is False
    assert data["stream_integrity_verified"] is False
    assert data["research_eligible"] is False
    assert data["stream_integrity"]["error"]


def test_incomplete_raw_is_rejected(tmp_path):
    path = tmp_path / "source" / "raw.db"
    capture(path, finish=False)
    _, data = report(path, tmp_path)
    assert data["scan_complete"] is False
    assert data["stream_integrity_verified"] is False
    assert "incomplete" in data["stream_integrity"]["error"]


def test_external_closure_session_must_match_manifest(tmp_path):
    path = tmp_path / "source" / "raw.db"
    capture(path)
    result = qualify_raw_v2(path, output_root=tmp_path / "qualification",
                            expected_session_id="different-session",
                            closure_evidence="synthetic fixture writer closed")
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["scan_complete"] is False
    assert data["stream_integrity_verified"] is False
    assert "session does not match" in data["stream_integrity"]["error"]


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sidecars_are_preserved_and_rejected_without_opening_sqlite(tmp_path, suffix):
    path = tmp_path / "source" / "raw.db"
    capture(path)
    sidecar = Path(str(path) + suffix)
    sidecar.write_bytes(b"preserve")
    before = snapshot(path.parent)
    _, data = report(path, tmp_path)
    assert data["stream_integrity_verified"] is False
    assert "sidecar" in data["stream_integrity"]["error"]
    assert snapshot(path.parent) == before


def test_active_write_capable_handle_is_rejected(tmp_path):
    path = tmp_path / "source" / "raw.db"
    capture(path)
    with path.open("r+b") as active_writer:
        active_writer.read(1)
        _, data = report(path, tmp_path)
        assert data["stream_integrity_verified"] is False
        assert data["stream_integrity"]["error"].startswith("PermissionError: [WinError 32]")


def test_nonempty_wal_is_never_silently_ignored_by_immutable_reader(tmp_path):
    path = tmp_path / "source" / "raw.db"
    capture(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE uncheckpointed(value TEXT)")
        conn.execute("INSERT INTO uncheckpointed VALUES ('must not disappear')")
        conn.commit()
        wal = Path(str(path) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0
        before = snapshot(path.parent)
        _, data = report(path, tmp_path)
        assert data["stream_integrity_verified"] is False
        assert "sidecar" in data["stream_integrity"]["error"]
        assert snapshot(path.parent) == before
        assert conn.execute("SELECT value FROM uncheckpointed").fetchone()[0] == "must not disappear"
    finally:
        conn.close()


def test_general_read_only_reader_can_create_wal_sidecars_on_windows(tmp_path):
    path = tmp_path / "source" / "raw.db"
    capture(path)
    assert not Path(str(path) + "-shm").exists()
    with read_raw_v2(path) as (_, rows):
        list(rows)
        assert Path(str(path) + "-shm").exists()


def test_cli_exit_codes_distinguish_quality_from_integrity(tmp_path, capsys):
    clean = tmp_path / "clean" / "raw.db"
    bad_quality = tmp_path / "quality" / "raw.db"
    capture(clean)
    capture(bad_quality, unsigned=True)
    common = ["--expected-session-id", "s", "--closure-evidence", "synthetic closed fixture"]
    assert main(["--db", str(clean), "--output-root", str(tmp_path / "out-clean"), *common]) == 0
    assert Path(capsys.readouterr().out.strip()).exists()
    assert main(["--db", str(bad_quality), "--output-root", str(tmp_path / "out-quality"),
                 *common]) == 2
    assert Path(capsys.readouterr().out.strip()).exists()
