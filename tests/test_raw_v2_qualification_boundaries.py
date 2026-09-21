"""합성 입력만 사용하는 qualification의 fail-closed 경계 회귀."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from collector.raw_v2 import CaptureControl, RawV2Writer
from collector import raw_v2_qualification as qualification
from scripts.qualify_raw_v2 import main


UTC = "2026-09-21T00:00:00Z"
WINDOWS = pytest.mark.skipif(os.name != "nt", reason="Windows NTFS qualification")


def write_control(path, *, control_type="session_start", details=None):
    with RawV2Writer(path, source="fixture", session_id="s", market_date="2026-09-21",
                     feed_scope="fixture") as writer:
        writer.append(CaptureControl("fixture", "s", 1, 1, control_type,
                                     {} if details is None else details),
                      received_at_utc=UTC, raw_fields={})
        writer.finish(close_ns=10)


def qualify(path, root):
    result = qualification.qualify_raw_v2(
        path, output_root=root / "reports", expected_session_id="s",
        closure_evidence="synthetic fixture, writer closed")
    return json.loads(result.read_text(encoding="utf-8"))


@pytest.mark.parametrize("details", [{}, {"issues": None}, {"issues": []}, {"issues": ""}])
def test_reasonless_parse_error_is_never_eligible(details):
    diagnostics = qualification._Diagnostics()
    diagnostics.accept({"event": CaptureControl("fixture", "s", 1, 1, "parse_error", details),
                        "received_at_utc": UTC, "raw_fields": {}, "exchange_ts_raw": None})
    result = diagnostics.result()
    assert result["research_eligible"] is False
    assert result["quality_diagnostics"]["parse_error_records"] == 1
    assert result["quality_diagnostics"]["unpaired_parse_error_records"] == 1
    assert result["quality_diagnostics"]["parse_error_reason_counts"] == {"unspecified_parse_error": 1}
    assert result["quality_diagnostics"]["logical_issue_counts"] == {"unspecified_parse_error": 1}


@WINDOWS
@pytest.mark.parametrize("details", [{}, {"issues": None}, {"issues": []}])
def test_reasonless_parse_error_completes_integrity_but_fails_quality(tmp_path, details):
    path = tmp_path / "source" / "raw.db"
    write_control(path, control_type="parse_error", details=details)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = qualify(path, tmp_path)
    assert result["stream_integrity_verified"] is True
    assert result["research_eligible"] is False
    assert result["counts"]["control_by_type"] == {"parse_error": 1}
    assert result["quality_diagnostics"]["parse_error_reason_counts"] == {"unspecified_parse_error": 1}
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@WINDOWS
@pytest.mark.parametrize("entrypoint", ["qualification", "sealed_reader"])
def test_junction_parent_is_rejected_before_sqlite(tmp_path, monkeypatch, entrypoint):
    path = tmp_path / "source" / "raw.db"
    write_control(path)
    link = tmp_path / "junction"
    subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(path.parent)],
                   check=True, capture_output=True, text=True)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    def no_sqlite(*args, **kwargs):
        raise AssertionError("reparse path reached sqlite3.connect")

    monkeypatch.setattr(qualification.sqlite3, "connect", no_sqlite)
    try:
        linked_path = link / path.name
        if entrypoint == "qualification":
            result = qualify(linked_path, tmp_path)
            assert result["stream_integrity_verified"] is False
            assert "reparse" in result["stream_integrity"]["error"]
        else:
            with pytest.raises(ValueError, match="reparse"):
                with qualification.read_sealed_raw_v2(linked_path):
                    pytest.fail("reparse source was accepted")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    finally:
        # Remove only this disposable junction, never its target directory.
        link.rmdir()


@WINDOWS
def test_parent_traversal_is_rejected_without_resolving_it(tmp_path):
    path = tmp_path / "source" / "raw.db"
    write_control(path)
    result = qualify(path.parent / ".." / "source" / "raw.db", tmp_path)
    assert result["stream_integrity_verified"] is False
    assert "parent traversal" in result["stream_integrity"]["error"]


@WINDOWS
@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_zero_byte_sidecar_is_preserved_and_rejected(tmp_path, suffix):
    path = tmp_path / "source" / "raw.db"
    write_control(path)
    sidecar = Path(str(path) + suffix)
    sidecar.touch(exist_ok=False)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = qualify(path, tmp_path)
    assert result["stream_integrity_verified"] is False
    assert "sidecar" in result["stream_integrity"]["error"]
    assert sidecar.exists() and sidecar.stat().st_size == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@WINDOWS
def test_cli_returns_three_for_preserved_sidecar(tmp_path, capsys):
    path = tmp_path / "source" / "raw.db"
    write_control(path)
    sidecar = Path(str(path) + "-wal")
    sidecar.touch(exist_ok=False)
    assert main(["--db", str(path), "--output-root", str(tmp_path / "reports"),
                 "--expected-session-id", "s", "--closure-evidence", "synthetic closed fixture"]) == 3
    result = json.loads(Path(capsys.readouterr().out.strip()).read_text(encoding="utf-8"))
    assert result["research_eligible"] is False
    assert sidecar.exists() and sidecar.stat().st_size == 0
