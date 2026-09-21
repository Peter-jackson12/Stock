import json
import os
from pathlib import Path
import sqlite3

import pytest

from scripts import lab_raw_v2_sidecars as lab


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows NTFS/SQLite lifecycle")


@pytest.fixture
def root():
    value = lab.create_lab_root()
    yield value
    # Deliberately retain failures in the script; pytest's disposable root may be removed.


def test_lab_guard_rejects_outside_and_unmarked_paths(root, tmp_path):
    assert lab.safe_lab_path(root, root / "fixture") == root / "fixture"
    with pytest.raises(ValueError, match="escapes"):
        lab.safe_lab_path(root, tmp_path / "outside")
    fake = tmp_path / f"{lab.ROOT_PREFIX}{'0' * 32}"
    fake.mkdir()
    with pytest.raises(ValueError, match="marker"):
        lab.require_lab_root(fake)


def test_finish_close_and_real_read_only_residue(root):
    db = root / "fixture" / "raw.db"
    stages = lab.create_closed_fixture(db)
    assert "raw.db-wal" in stages["after_finish_before_connection_close"]
    assert set(stages["after_explicit_connection_close"]) == {"raw.db"}

    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0)
    try:
        conn.execute("SELECT value FROM metadata LIMIT 1").fetchone()
        while_open = lab.db_snapshot(db)
        assert while_open["raw.db-wal"]["size"] == 0
        assert while_open["raw.db-shm"]["size"] > 0
    finally:
        conn.close()
    assert lab.db_snapshot(db) == while_open


def test_writable_page_read_is_distinct_from_connect_only(root):
    source = root / "source" / "raw.db"
    lab.create_closed_fixture(source)
    conn = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("SELECT value FROM metadata LIMIT 1").fetchone()
    finally:
        conn.close()
    baseline = lab.logical_snapshot(source)

    connect_only = lab.copy_sqlite_set(root, source, root / "connect_only")
    page_read = lab.copy_sqlite_set(root, source, root / "page_read")
    connect_before = lab.file_snapshot(connect_only)["sha256"]
    page_before = lab.file_snapshot(page_read)["sha256"]

    lab.sqlite_managed_candidate(connect_only, read_page=False)
    assert Path(str(connect_only) + "-wal").exists()
    assert Path(str(connect_only) + "-shm").exists()

    lab.sqlite_managed_candidate(page_read, read_page=True)
    assert not Path(str(page_read) + "-wal").exists()
    assert not Path(str(page_read) + "-shm").exists()
    assert lab.file_snapshot(connect_only)["sha256"] == connect_before
    assert lab.file_snapshot(page_read)["sha256"] == page_before
    assert lab.logical_snapshot(page_read, immutable=True) == baseline


def test_committed_wal_is_rejected_and_main_only_loses_marker(root):
    baseline = root / "source" / "raw.db"
    lab.create_closed_fixture(baseline)
    db = lab.copy_sqlite_set(root, baseline, root / "wal")
    with lab.started_worker(root, "abrupt_wal", db) as (process, _):
        process.wait(timeout=lab.CHILD_TIMEOUT_SECONDS)
        assert process.returncode == 0
    assert Path(str(db) + "-wal").stat().st_size > 0
    assert lab.logical_snapshot(db)["wal_marker_rows"] == [("committed-only-in-wal",)]

    main_only_dir = root / "main_only"
    main_only_dir.mkdir()
    main_only = main_only_dir / "raw.db"
    main_only.write_bytes(db.read_bytes())
    assert lab.logical_snapshot(main_only, immutable=True)["wal_marker_rows"] is None
    report = lab.qualify_fixture(root, db, "nonempty_wal")
    assert report["stream_integrity_verified"] is False
    assert "sidecar" in report["stream_integrity"]["error"]


def test_lab_cli_accepts_no_external_database(tmp_path):
    with pytest.raises(SystemExit):
        lab.main(["--db", str(tmp_path / "anything.db")])
