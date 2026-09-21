"""Pin the local sidecar lab's negative findings in Windows CI.

These tests reproduce unsafe transitions; passing is not production cleanup
approval.  Only UUID-marked synthetic lab roots are used and retained.
"""
import os
import sqlite3

import pytest

from scripts import lab_raw_v2_sidecars as lab


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows NTFS/SQLite lifecycle")


@pytest.fixture
def fixture_set():
    root = lab.create_lab_root()
    baseline = root / "source" / "raw.db"
    lab.create_closed_fixture(baseline)
    return root, baseline


def test_handles_partial_cleanup_and_release_reconnect_race(fixture_set):
    root, baseline = fixture_set
    result = lab._run_locking(root, baseline)

    for name in ("existing_write_capable_main_handle", "active_sqlite_writer"):
        outcome = result[name]
        assert outcome["status"] == "blocked", outcome
        assert outcome["winerror"] == 32, outcome

    # Main-file sealing can coexist with a SQLite read-only connection.
    # That does not prove that its WAL/SHM are absent or immutable.
    assert result["open_sqlite_read_only_connection"]["status"] == "succeeded"
    for name in ("own_writable_sqlite_connection_while_sealed", "writer_started_after_seal"):
        outcome = result[name]
        assert outcome["status"] == "blocked", outcome
        assert outcome["sqlite_errorcode"] == sqlite3.SQLITE_READONLY, outcome

    # A successful SQLite call can leave only part of the sidecar set cleaned.
    partial = result["open_wal_handle_then_page_read_cleanup"]
    assert partial["status"] == "succeeded", partial
    after = partial["value"]["after"]
    assert "raw.db-wal" in after and after["raw.db-wal"]["size"] == 0
    assert "raw.db-shm" not in after
    assert "raw.db-wal" in result["open_wal_handle_after_release"]

    # The child enters through the deliberately released guard before the
    # cleanup reconnect.  Synchronization, rather than scheduler luck, proves
    # the gap; it does not establish continuous exclusion.
    competitor = result["competing_writer"]
    assert competitor["status"] == "write_committed", competitor
    reconnect = result["cleanup_reconnect_after_competing_writer_entered"]
    assert reconnect["status"] == "blocked", reconnect
    assert (reconnect["error"].startswith("PermissionError:")
            or reconnect["sqlite_errorcode"] in {
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
                sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN,
            }), reconnect
    assert lab.enforce_budget(root) <= lab.MAX_TOTAL_BYTES


def test_real_journal_incomplete_unknown_sidecar_and_namespace_rejections(fixture_set):
    root, baseline = fixture_set
    result = lab._run_rejections(root, baseline)

    journal = result["rollback_journal"]
    assert "raw.db-journal" in journal["snapshot"]
    assert journal["snapshot"]["raw.db-journal"]["size"] > 0
    for name, reason in (("rollback_journal", "sidecar"),
                         ("incomplete_raw", "incomplete"),
                         ("unknown_sidecar", "sidecar")):
        report = result[name]["qualification"]
        assert report["stream_integrity_verified"] is False, report
        assert report["research_eligible"] is False, report
        assert reason in report["stream_integrity"]["error"], report

    # A missing ability to create a junction is not a passing rejection test.
    reparse = result["reparse_path"]
    assert reparse["status"] == "blocked", reparse
    assert "reparse" in reparse["error"], reparse
    assert reparse["method"] in {"directory_symlink", "directory_junction"}

    replacement = result["path_replacement_after_guard_release"]
    assert replacement["before"]["identity"] == replacement["during"]["identity"]
    assert replacement["identity_changed"] is True
    assert replacement["before"]["identity"] != replacement["after"]["identity"]
    assert lab.enforce_budget(root) <= lab.MAX_TOTAL_BYTES
