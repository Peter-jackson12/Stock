"""운영 파일 없이 Windows의 보호된 acquisition과 실패 경계를 검증한다."""
import json
import mmap
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.parse import unquote

import pytest

from scripts import lab_raw_v2_clone as clone
from scripts import lab_raw_v2_sidecars as lab

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows NTFS clone lab")


@pytest.fixture
def source():
    root = lab.create_lab_root()
    return root, clone.make_fixture(root)


def read_result(path):
    return json.loads(path.read_text(encoding="utf-8"))


def failed(root, fixture, **kwargs):
    before = lab.db_snapshot(fixture.path)
    result = read_result(clone.clone_fixture(root, fixture, **kwargs))
    assert result["status"] == "failed", result
    assert result["synthetic_copy_verified"] is False
    assert result["production_approved"] is False
    assert result["research_eligible"] is False
    assert result["error"]
    assert lab.db_snapshot(fixture.path) == before
    return result


@pytest.mark.parametrize("unsigned", [False, True])
def test_clone_preserves_bytes_full_stream_and_quality(unsigned):
    root = lab.create_lab_root()
    fixture = clone.make_fixture(root, unsigned=unsigned)
    before = lab.db_snapshot(fixture.path)
    result_path = clone.clone_fixture(root, fixture)
    result = read_result(result_path)
    assert result["status"] == "synthetic_verified", result["error"]
    assert result["synthetic_copy_verified"] is True
    assert result["source_unchanged"] is True
    assert result["source_sqlite_reopened"] is False
    assert result["production_approved"] is False
    assert lab.db_snapshot(fixture.path) == before
    qualified = result["qualification"]
    assert qualified["scan_complete"] is True
    assert qualified["stream_integrity_verified"] is True
    assert qualified["research_eligible"] is (not unsigned)
    assert qualified["counts"]["raw_records"] == (3 if unsigned else 2)
    assert qualified["quality_diagnostics"] == fixture.qualification["quality_diagnostics"]
    reasons = {"trade_direction_unverified": 1} if unsigned else {}
    assert qualified["quality_diagnostics"]["parse_error_reason_counts"] == reasons
    assert qualified["quality_diagnostics"]["normalized_issue_counts"] == reasons
    evidence = result_path.parent / "evidence"
    working = result_path.parent / "working"
    for name, record in fixture.files.items():
        assert lab.file_snapshot(evidence / name)["sha256"] == record["sha256"]
        assert (evidence / name).stat().st_ino != (fixture.path.parent / name).stat().st_ino
    assert not any(os.path.lexists(str(working / "raw.db") + s) for s in lab.SIDECARS)
    assert lab.enforce_budget(root) <= lab.MAX_TOTAL_BYTES
    print(f"CLONE_LAB unsigned={unsigned} source_preserved=true stream_integrity=true "
          f"research_eligible={not unsigned} report={result_path}")


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
@pytest.mark.parametrize("mode", ["rb", "r+b"])
def test_existing_read_or_write_handles_reject_acquisition(source, suffix, mode):
    root, fixture = source
    with Path(str(fixture.path) + suffix).open(mode):
        result = failed(root, fixture)
    assert "WinError 32" in result["error"], result


@pytest.mark.parametrize("suffix", ["", "-shm"])
def test_live_writable_mapping_after_file_close_rejects_acquisition(source, suffix):
    root, fixture = source
    with Path(str(fixture.path) + suffix).open("r+b") as stream:
        mapping = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_WRITE)
    try:
        result = failed(root, fixture)
        assert "WinError 32" in result["error"], result
    finally:
        mapping.close()


def test_existing_sqlite_reader_is_not_mistaken_for_quiescence(source):
    root, fixture = source
    conn = sqlite3.connect(fixture.path.as_uri() + "?mode=ro", uri=True, timeout=0)
    try:
        conn.execute("SELECT value FROM metadata").fetchone()
        result = failed(root, fixture)
        assert "WinError 32" in result["error"], result
    finally:
        conn.close()


def test_late_child_reader_writer_and_file_mutations_are_blocked(source):
    root, fixture = source
    observed = []
    code = r'''
import json, pathlib, sqlite3, sys
p = pathlib.Path(sys.argv[1])
results = []
for suffix in ("", "-wal", "-shm"):
    path = pathlib.Path(str(p) + suffix)
    for mode in ("rb", "r+b"):
        try:
            with path.open(mode):
                pass
            results.append("unexpected-open")
        except OSError as exc:
            results.append(exc.winerror)
for readonly in (False, True):
    conn = None
    try:
        conn = sqlite3.connect(p.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                               uri=True, timeout=0)
        conn.execute("SELECT value FROM metadata" if readonly else "BEGIN IMMEDIATE").fetchone()
        results.append("unexpected-sqlite")
    except sqlite3.Error:
        results.append("sqlite-blocked")
    finally:
        if conn is not None:
            conn.close()
print(json.dumps(results))
'''
    def hook(stage, path, run):
        if stage != "sealed":
            return
        print("CLONE_PROBE child_start", flush=True)
        process = subprocess.run([sys.executable, "-c", code, str(path)],
                                 capture_output=True, text=True, timeout=lab.CHILD_TIMEOUT_SECONDS)
        print("CLONE_PROBE child_result", process.returncode, process.stdout, process.stderr, flush=True)
        assert process.returncode == 0, process.stderr
        observed.extend(json.loads(process.stdout))
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(path) + suffix)
            print("CLONE_PROBE unlink", target.name, flush=True)
            with pytest.raises(OSError):
                target.unlink()
            print("CLONE_PROBE rename_file", target.name, flush=True)
            with pytest.raises(OSError):
                target.rename(target.with_name(target.name + ".replaced"))
        for directory in (path.parent, root, run / "working"):
            print("CLONE_PROBE rename_directory", directory.name, flush=True)
            with pytest.raises(OSError):
                directory.rename(directory.with_name(directory.name + "_moved"))
    result = read_result(clone.clone_fixture(root, fixture, hook=hook))
    print("CLONE_PROBE end", result["error"], sorted(p.name for p in root.iterdir()), flush=True)
    assert result["synthetic_copy_verified"] is True, result["error"]
    assert observed == [32] * 6 + ["sqlite-blocked"] * 2


def test_clone_never_opens_source_with_sqlite(source, monkeypatch):
    root, fixture = source
    original = sqlite3.connect
    calls = []
    def spy(database, *args, **kwargs):
        value = unquote(str(database)).replace("\\", "/").lower()
        assert fixture.path.as_posix().lower() not in value
        calls.append(value)
        return original(database, *args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", spy)
    result = read_result(clone.clone_fixture(root, fixture))
    assert result["synthetic_copy_verified"] is True, result["error"]
    assert calls and all("/working/raw.db" in value for value in calls)


@pytest.mark.parametrize("stage", ["sealed", "copy_chunk", "copied", "before_cleanup",
                                   "after_cleanup", "before_qualification"])
def test_interruption_preserves_original_partial_files_and_releases_handles(source, stage):
    root, fixture = source
    seen = []
    def hook(at, path, run):
        if at == stage:
            seen.append(run)
            raise RuntimeError("injected interruption: " + at)
    result = failed(root, fixture, hook=hook)
    assert seen and "injected interruption" in result["error"]
    assert result["source_unchanged"] is True
    assert seen[0].exists() and (seen[0] / "result.json").exists()
    if stage != "sealed":
        assert (seen[0] / "evidence/raw.db").exists()
    for name in fixture.files:
        with (fixture.path.parent / name).open("r+b"):
            pass


@pytest.mark.parametrize("damage", ["nonempty_wal", "journal", "unknown", "missing_shm",
                                    "hardlink", "altered_source"])
def test_untrusted_or_aliased_file_sets_are_not_opened_as_sqlite(source, damage, monkeypatch):
    root, fixture = source
    path = fixture.path
    if damage == "nonempty_wal":
        Path(str(path) + "-wal").write_bytes(b"not-residue")
    elif damage == "journal":
        Path(str(path) + "-journal").write_bytes(b"journal-evidence")
    elif damage == "unknown":
        (path.parent / "unknown.bin").write_bytes(b"unknown-provenance")
    elif damage == "missing_shm":
        Path(str(path) + "-shm").rename(root / "preserved-shm")
    elif damage == "hardlink":
        os.link(path, root / "alias.db")
    else:
        with Path(str(path) + "-shm").open("r+b") as stream:
            stream.write(b"provenance-differs")
    opened = []
    def forbidden(*args, **kwargs):
        opened.append(args)
        raise RuntimeError("unexpected SQLite open during rejected acquisition")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    result = failed(root, fixture)
    assert result["synthetic_copy_verified"] is False
    assert not opened


def test_incomplete_fixture_is_never_promoted():
    root = lab.create_lab_root()
    fixture = clone.make_fixture(root, finish=False)
    result = failed(root, fixture)
    assert "incomplete" in result["error"]


@pytest.mark.parametrize("damage", ["conflict", "hash", "sidecar_remaining", "main_changed"])
def test_destination_failures_preserve_evidence_without_publishing(source, damage):
    root, fixture = source
    outputs = []
    def hook(stage, path, run):
        work = run / "working/raw.db"
        if damage == "conflict" and stage == "sealed":
            work.write_bytes(b"preserve-existing-destination")
            outputs.append(work)
        elif damage == "hash" and stage == "copied":
            work.write_bytes(b"corrupt-copy")
            outputs.append(work)
        elif damage == "sidecar_remaining" and stage == "after_cleanup":
            Path(str(work) + "-wal").write_bytes(b"")
            outputs.append(Path(str(work) + "-wal"))
        elif damage == "main_changed" and stage == "after_cleanup":
            work.write_bytes(b"not-the-same-dataset")
            outputs.append(work)
    result = failed(root, fixture, hook=hook)
    assert outputs and outputs[0].exists()
    assert "qualification" not in result
    if damage == "conflict":
        assert outputs[0].read_bytes() == b"preserve-existing-destination"


def test_new_journal_name_is_detected_not_claimed_excluded(source):
    root, fixture = source
    before_main = lab.file_snapshot(fixture.path)
    def hook(stage, path, run):
        if stage == "copied":
            Path(str(path) + "-journal").write_bytes(b"late-untrusted-journal")
    result = read_result(clone.clone_fixture(root, fixture, hook=hook))
    assert result["status"] == "failed", result
    assert "journal" in result["error"]
    assert result["production_approved"] is False
    assert result["source_unchanged"] is not True
    assert lab.file_snapshot(fixture.path) == before_main
    assert Path(str(fixture.path) + "-journal").read_bytes() == b"late-untrusted-journal"
    assert "qualification" not in result


def test_external_db_argument_is_rejected_before_creating_any_lab(monkeypatch):
    def forbidden():
        pytest.fail("no fixture creation for invalid CLI")
    monkeypatch.setattr(lab, "create_lab_root", forbidden)
    with pytest.raises(SystemExit) as exc:
        clone.main(["--db", "C:/operating/raw.db"])
    assert exc.value.code == 2


@pytest.mark.parametrize("limit,value", [("MAX_IO_BYTES", 1), ("MAX_SECONDS", -1)])
def test_stream_budget_failure_preserves_source(source, monkeypatch, limit, value):
    root, fixture = source
    monkeypatch.setattr(clone, limit, value)
    result = failed(root, fixture)
    assert "budget" in result["error"]
    assert "qualification" not in result
