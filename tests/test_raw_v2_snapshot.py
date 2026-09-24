"""Synthetic Windows regressions for production raw-v2 frozen snapshots."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from collector.kiwoom.capture_session import CaptureSession
from collector import raw_v2_snapshot as snapshot
from scripts.acquire_raw_v2_snapshot import main


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
UTC = "2026-09-21T00:00:00Z"


def residue_fixture(path, *, session_id="s"):
    path.parent.mkdir(parents=True, exist_ok=False)
    with CaptureSession(
        path,
        source="fixture",
        session_id=session_id,
        market_date="2026-09-21",
        feed_scope="fixture",
        price_policy="signed_magnitude",
        direction_policy="signed_volume",
        started_ns=0,
        started_at_utc=UTC,
    ) as session:
        session.on_tick(
            code="005930",
            venue="unknown",
            real_type="주식체결",
            fids={"10": "+10001", "15": "+30", "20": "090001"},
            received_ns=1,
            received_at_utc="2026-09-21T00:00:01Z",
        )
        session.finish(10)
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()

    # Create the exact reader-residue shape already observed in the real session.
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
    try:
        conn.execute("SELECT value FROM metadata LIMIT 1").fetchone()
    finally:
        conn.close()
    wal = Path(str(path) + "-wal")
    shm = Path(str(path) + "-shm")
    assert wal.exists() and wal.stat().st_size == 0
    assert shm.exists() and shm.stat().st_size == 32768
    return path


def output_root(tmp_path):
    root = tmp_path / "snapshots"
    root.mkdir()
    return root


def load_result(paths):
    return json.loads(paths.result.read_text(encoding="utf-8"))


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def test_residue_snapshot_preserves_source_and_publishes_sidecar_free_working_copy(tmp_path):
    source = residue_fixture(tmp_path / "source" / "raw.db")
    before = {
        item.name: (item.stat().st_size, item.stat().st_mtime_ns, sha(item))
        for item in source.parent.iterdir() if item.is_file()
    }
    paths = snapshot.acquire_frozen_snapshot(
        source,
        output_root=output_root(tmp_path),
        expected_session_id="s",
    )
    result = load_result(paths)

    assert result["status"] == "snapshot_ready"
    assert result["snapshot_ready_for_prefix_qualification"] is True
    assert result["declared_source_sqlite_policy"] == "never_open_source_sqlite"
    assert len(result["code_provenance"]["collector/raw_v2_snapshot.py"]) == 64
    assert result["source_unchanged_during_acquisition"] is True
    assert result["whole_stream_assessed"] is False
    assert result["research_eligible"] is False
    assert result["working_cleanup"]["manifest"]["session_id"] == "s"
    assert not any(
        Path(str(paths.working_main) + suffix).exists()
        for suffix in snapshot.SIDECARS
    )
    source_hash = result["copy_records"][source.name]["source"]["sha256"]
    assert result["working_cleanup"]["main_sha256_after_close"] == source_hash
    assert sha(paths.working_main) == source_hash
    for suffix in ("-wal", "-shm"):
        source_sidecar = Path(str(source) + suffix)
        evidence_sidecar = Path(str(paths.evidence_main) + suffix)
        assert evidence_sidecar.read_bytes() == source_sidecar.read_bytes()
    after = {
        item.name: (item.stat().st_size, item.stat().st_mtime_ns, sha(item))
        for item in source.parent.iterdir() if item.is_file()
    }
    assert after == before


def test_source_is_never_opened_with_sqlite(tmp_path, monkeypatch):
    source = residue_fixture(tmp_path / "source" / "raw.db")
    root = output_root(tmp_path)
    original = sqlite3.connect
    opened = []

    def spy(database, *args, **kwargs):
        text = str(database).replace("\\", "/").lower()
        assert str(source).replace("\\", "/").lower() not in text
        opened.append(text)
        return original(database, *args, **kwargs)

    monkeypatch.setattr(snapshot.sqlite3, "connect", spy)
    paths = snapshot.acquire_frozen_snapshot(
        source, output_root=root, expected_session_id="s"
    )
    result = load_result(paths)
    assert result["snapshot_ready_for_prefix_qualification"] is True
    assert opened and all("/working/" in value for value in opened)


def test_existing_reader_blocks_exclusive_acquisition(tmp_path):
    source = residue_fixture(tmp_path / "source" / "raw.db")
    root = output_root(tmp_path)
    with source.open("rb"):
        paths = snapshot.acquire_frozen_snapshot(
            source, output_root=root, expected_session_id="s"
        )
    result = load_result(paths)
    assert result["status"] == "failed"
    assert result["snapshot_ready_for_prefix_qualification"] is False
    assert "WinError 32" in result["error"]


@pytest.mark.parametrize("damage", ["nonempty_wal", "journal", "wrong_shm", "hardlink"])
def test_untrusted_source_sets_fail_closed_before_copy(tmp_path, damage):
    source = residue_fixture(tmp_path / "source" / "raw.db")
    wal = Path(str(source) + "-wal")
    shm = Path(str(source) + "-shm")
    if damage == "nonempty_wal":
        wal.write_bytes(b"database-state")
    elif damage == "journal":
        Path(str(source) + "-journal").write_bytes(b"journal-state")
    elif damage == "wrong_shm":
        shm.write_bytes(b"x")
    else:
        os.link(source, source.parent / "raw-alias.db")
    root = output_root(tmp_path)
    before = set(root.iterdir())
    with pytest.raises(ValueError):
        snapshot.acquire_frozen_snapshot(
            source, output_root=root, expected_session_id="s"
        )
    assert set(root.iterdir()) == before


def test_wrong_expected_session_keeps_snapshot_as_failed_evidence(tmp_path):
    source = residue_fixture(tmp_path / "source" / "raw.db")
    paths = snapshot.acquire_frozen_snapshot(
        source,
        output_root=output_root(tmp_path),
        expected_session_id="wrong",
    )
    result = load_result(paths)
    assert result["status"] == "failed"
    assert result["snapshot_ready_for_prefix_qualification"] is False
    assert "session does not match" in result["error"]
    assert paths.evidence_main.exists()
    assert paths.working_main.exists()
    assert source.exists()


def test_insufficient_space_rejected_before_output(tmp_path, monkeypatch):
    source = residue_fixture(tmp_path / "source" / "raw.db")
    root = output_root(tmp_path)
    usage = shutil.disk_usage(root)
    monkeypatch.setattr(
        snapshot.shutil,
        "disk_usage",
        lambda _: type(usage)(usage.total, usage.used, 1),
    )
    with pytest.raises(ValueError, match="insufficient snapshot space"):
        snapshot.acquire_frozen_snapshot(
            source, output_root=root, expected_session_id="s"
        )
    assert list(root.iterdir()) == []


def test_output_inside_source_directory_is_rejected(tmp_path):
    source = residue_fixture(tmp_path / "source" / "raw.db")
    nested = source.parent / "snapshots"
    nested.mkdir()
    with pytest.raises(ValueError, match="must not be inside"):
        snapshot.acquire_frozen_snapshot(
            source, output_root=nested, expected_session_id="s"
        )


def test_cli_reports_ready_snapshot(tmp_path, capsys):
    source = residue_fixture(tmp_path / "source" / "raw.db")
    root = output_root(tmp_path)
    code = main([
        "--db", str(source),
        "--output-root", str(root),
        "--expected-session-id", "s",
        "--residue-policy", snapshot.RESIDUE_POLICY,
    ])
    assert code == 0
    result_path = Path(capsys.readouterr().out.strip())
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["snapshot_ready_for_prefix_qualification"] is True
    assert Path(result["paths"]["working_main"]).exists()


def test_evidence_members_stay_write_delete_sealed_through_working_cleanup(tmp_path, monkeypatch):
    source = residue_fixture(tmp_path / "source" / "raw.db")
    root = output_root(tmp_path)
    original = snapshot._read_working_manifest_and_cleanup
    observed = []

    def guarded(path):
        evidence_main = path.parent.parent / "evidence" / path.name
        for candidate in (
            evidence_main,
            Path(str(evidence_main) + "-wal"),
            Path(str(evidence_main) + "-shm"),
        ):
            with pytest.raises(PermissionError):
                with candidate.open("r+b"):
                    pass
            observed.append(candidate.name)
        return original(path)

    monkeypatch.setattr(snapshot, "_read_working_manifest_and_cleanup", guarded)
    paths = snapshot.acquire_frozen_snapshot(
        source, output_root=root, expected_session_id="s"
    )
    result = load_result(paths)
    assert result["snapshot_ready_for_prefix_qualification"] is True
    assert result["evidence_members_sealed_through_finalization"] is True
    assert observed == [source.name, source.name + "-wal", source.name + "-shm"]
