"""Windows/NTFS SQLite sidecar lifecycle laboratory.

This is deliberately not an operational cleanup command.  It accepts no source
database.  Every run creates a UUID-named directory below the current user's
temporary directory, marks it, and only operates on newly-created fixtures
below that directory.  Evidence is retained on success and failure.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collector.kiwoom.capture_session import CaptureSession
from collector.raw_archive import sealed_source
from collector.raw_v2 import _read_raw_v2_connection
from collector.raw_v2_qualification import _require_local_ntfs, qualify_raw_v2


SCHEMA = "raw_v2_sidecar_lab_v1"
ROOT_PREFIX = "Stock_raw_sidecar_lab_"
MARKER = ".stock_raw_sidecar_lab.json"
MAX_DB_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
CHILD_TIMEOUT_SECONDS = 20
SIDECARS = ("-wal", "-shm", "-journal")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, value):
    path = Path(path)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def _replace_json(path, value):
    path = Path(path)
    partial = path.with_name(path.name + ".partial")
    with partial.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, path)


def create_lab_root():
    root = Path(tempfile.gettempdir()) / f"{ROOT_PREFIX}{uuid.uuid4().hex}"
    root.mkdir()
    filesystem = _require_local_ntfs(root)
    _write_json(root / MARKER, {
        "schema": SCHEMA,
        "created_at_utc": utc_now(),
        "root": str(root),
        "filesystem": filesystem,
    })
    return root


def require_lab_root(root):
    root = Path(root).absolute()
    suffix = root.name[len(ROOT_PREFIX):] if root.name.startswith(ROOT_PREFIX) else ""
    if len(suffix) != 32 or suffix.strip("0123456789abcdef"):
        raise ValueError("UUID-named sidecar lab root required")
    marker = root / MARKER
    if not marker.is_file():
        raise ValueError("sidecar lab marker required")
    data = json.loads(marker.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA or data.get("root") != str(root):
        raise ValueError("sidecar lab marker/root mismatch")
    _require_local_ntfs(root)
    return root


def safe_lab_path(root, candidate, *, must_exist=False):
    root = require_lab_root(root)
    candidate = Path(candidate).absolute()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes sidecar lab root") from exc
    probe = candidate if candidate.exists() else candidate.parent
    if must_exist and not candidate.exists():
        raise FileNotFoundError(candidate)
    while probe != root.parent:
        if probe.exists() and probe.is_symlink():
            raise ValueError("reparse/symlink path inside lab root is rejected")
        if probe == root:
            break
        probe = probe.parent
    if probe != root:
        raise ValueError("path ancestry did not reach lab root")
    # GetFileAttributes catches directory junctions and other reparse points.
    # For a not-yet-created nested output, inspect its nearest existing parent;
    # _require_local_ntfs then checks that parent and all of its ancestors.
    attributes_probe = candidate
    while not attributes_probe.exists():
        attributes_probe = attributes_probe.parent
    _require_local_ntfs(attributes_probe)
    return candidate


def enforce_budget(root):
    root = require_lab_root(root)
    total = 0
    for item in root.rglob("*"):
        if item.is_symlink():
            raise ValueError("reparse/symlink entry inside lab root")
        if item.is_file():
            size = item.stat().st_size
            total += size
            if item.name.endswith((".db", ".sqlite", ".sqlite3")) and size > MAX_DB_BYTES:
                raise ValueError("lab database exceeds 32 MiB")
    if total > MAX_TOTAL_BYTES:
        raise ValueError("lab fixtures exceed 256 MiB")
    return total


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def file_snapshot(path):
    path = Path(path)
    info = path.stat()
    return {
        "name": path.name,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "identity": {"st_dev": info.st_dev, "st_ino": info.st_ino},
        "sha256": _sha256(path),
    }


def db_snapshot(path):
    path = Path(path)
    result = {}
    for item in (path, *(Path(str(path) + suffix) for suffix in SIDECARS)):
        if os.path.lexists(item):
            result[item.name] = file_snapshot(item)
    return result


def create_closed_fixture(path, *, session_id="sidecar-lab"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=False)
    session = CaptureSession(
        path,
        source="sidecar-lab",
        session_id=session_id,
        market_date="2026-09-21",
        feed_scope="synthetic_fixture",
        price_policy="signed_magnitude",
        direction_policy="signed_volume",
        started_ns=0,
        started_at_utc="2026-09-21T00:00:00Z",
    )
    session.on_tick(
        code="005930", venue="unknown", real_type="주식체결",
        fids={"10": "+10001", "15": "+7", "20": "090001"},
        received_ns=1, received_at_utc="2026-09-21T00:00:01Z",
    )
    session.commit()
    session.finish(2)
    after_finish = db_snapshot(path)
    session.__exit__(None, None, None)
    return {"after_finish_before_connection_close": after_finish,
            "after_explicit_connection_close": db_snapshot(path)}


def logical_snapshot(path, *, immutable=False):
    path = Path(path)
    query = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    conn = sqlite3.connect(path.as_uri() + query, uri=True, timeout=0)
    try:
        metadata_rows = conn.execute("SELECT value FROM metadata").fetchall()
        event_rows = conn.execute("SELECT seq,payload FROM events ORDER BY seq").fetchall()
        marker = None
        if conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='wal_marker'").fetchone()[0]:
            marker = conn.execute("SELECT value FROM wal_marker ORDER BY rowid").fetchall()
    finally:
        conn.close()
    payload_digest = hashlib.sha256()
    for _, payload in event_rows:
        payload_digest.update(payload.encode("utf-8") + b"\n")
    manifests = [json.loads(value) for (value,) in metadata_rows]
    return {
        "manifest": manifests,
        "event_rows": event_rows,
        "payload_sha256": payload_digest.hexdigest(),
        "wal_marker_rows": marker,
    }


def copy_sqlite_set(root, source, destination_dir):
    source = safe_lab_path(root, source, must_exist=True)
    destination_dir = safe_lab_path(root, destination_dir)
    destination_dir.mkdir(parents=False, exist_ok=False)
    target = destination_dir / source.name
    for item in (source, *(Path(str(source) + suffix) for suffix in SIDECARS)):
        if os.path.lexists(item):
            shutil.copy2(item, destination_dir / item.name)
    enforce_budget(root)
    return target


def qualify_fixture(root, path, label):
    output = safe_lab_path(root, Path(root) / "qualification" / label)
    result_path = qualify_raw_v2(
        path,
        output_root=output,
        expected_session_id="sidecar-lab",
        closure_evidence="synthetic lab: writer connection and child process closed",
    )
    return json.loads(result_path.read_text(encoding="utf-8"))


def sqlite_managed_candidate(path, *, read_page):
    before = db_snapshot(path)
    conn = sqlite3.connect(path, timeout=0)
    cursor = None
    try:
        if read_page:
            cursor = conn.execute("SELECT value FROM metadata LIMIT 1")
            cursor.fetchone()
    finally:
        if cursor is not None:
            cursor.close()
        conn.close()
    return {"before": before, "after": db_snapshot(path)}


def _wait_for(path, timeout=CHILD_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"timed out waiting for {path}")


def _stop_started_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


@contextmanager
def started_worker(root, action, db):
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", action,
               "--root", str(root), "--db", str(db)]
    process = subprocess.Popen(command, cwd=PROJECT_ROOT)
    try:
        yield process, command
    finally:
        _stop_started_process(process)


def _touch(path):
    with Path(path).open("x", encoding="ascii") as stream:
        stream.write("ready\n")


def _worker(root, action, db):
    root = require_lab_root(root)
    db = safe_lab_path(root, db, must_exist=action not in {"baseline"})
    sync = safe_lab_path(root, root / "sync")
    sync.mkdir(exist_ok=True)
    result = sync / f"{action}.json"
    if action == "baseline":
        stages = create_closed_fixture(db)
        _write_json(result, stages)
        return 0
    if action == "reader_hold":
        conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0)
        try:
            with _read_raw_v2_connection(conn) as (_, rows):
                list(rows)
            _write_json(result, {"while_connection_open": db_snapshot(db)})
            _touch(sync / "reader_open.ready")
            _wait_for(sync / "reader_close.release")
        finally:
            conn.close()
        data = json.loads(result.read_text(encoding="utf-8"))
        data["after_explicit_close_before_exit"] = db_snapshot(db)
        _replace_json(result, data)
        _touch(sync / "reader_closed.ready")
        _wait_for(sync / "reader_exit.release")
        return 0
    if action == "abrupt_wal":
        conn = sqlite3.connect(db, timeout=0)
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE wal_marker(value TEXT NOT NULL)")
        conn.execute("INSERT INTO wal_marker VALUES ('committed-only-in-wal')")
        conn.commit()
        _write_json(result, {"before_abrupt_exit": db_snapshot(db)})
        os._exit(0)
    if action == "rollback_journal":
        conn = sqlite3.connect(db, timeout=0)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE rollback_marker(value TEXT)")
        conn.execute("INSERT INTO rollback_marker VALUES ('uncommitted')")
        _write_json(result, {"before_abrupt_exit": db_snapshot(db)})
        os._exit(0)
    if action in {"late_writer", "race_writer"}:
        _wait_for(sync / f"{action}.start")
        try:
            conn = sqlite3.connect(db, timeout=0)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("CREATE TABLE IF NOT EXISTS competing_writer(value TEXT)")
                conn.execute("INSERT INTO competing_writer VALUES (?)", (action,))
                if action == "race_writer":
                    _touch(sync / "race_writer.acquired")
                    _wait_for(sync / "race_writer.release")
                conn.commit()
                value = {"status": "write_committed"}
            finally:
                conn.close()
        except BaseException as exc:
            value = {"status": "blocked", "error": f"{type(exc).__name__}: {exc}",
                     "winerror": getattr(exc, "winerror", None),
                     "sqlite_errorcode": getattr(exc, "sqlite_errorcode", None)}
        _write_json(result, value)
        return 0
    raise ValueError(f"unknown worker action: {action}")


def _run_baseline(root):
    db = safe_lab_path(root, root / "baseline" / "raw.db")
    with started_worker(root, "baseline", db) as (process, command):
        process.wait(timeout=CHILD_TIMEOUT_SECONDS)
        if process.returncode:
            raise RuntimeError(f"baseline worker failed: {process.returncode}")
    stages = json.loads((root / "sync/baseline.json").read_text(encoding="utf-8"))
    stages["after_child_process_exit"] = db_snapshot(db)
    stages["command"] = command
    stages["qualification"] = qualify_fixture(root, db, "baseline")
    stages["logical"] = logical_snapshot(db, immutable=True)
    return db, stages


def _run_read_only_residue(root, baseline):
    db = copy_sqlite_set(root, baseline, root / "read_only_residue")
    before = db_snapshot(db)
    with started_worker(root, "reader_hold", db) as (process, command):
        _wait_for(root / "sync/reader_open.ready")
        while_open = db_snapshot(db)
        _touch(root / "sync/reader_close.release")
        _wait_for(root / "sync/reader_closed.ready")
        after_close = db_snapshot(db)
        _touch(root / "sync/reader_exit.release")
        process.wait(timeout=CHILD_TIMEOUT_SECONDS)
        if process.returncode:
            raise RuntimeError(f"reader worker failed: {process.returncode}")
    return db, {"before_query": before, "while_reader_connection_open": while_open,
                "after_explicit_close_before_child_exit": after_close,
                "after_child_process_exit": db_snapshot(db), "command": command,
                "worker": json.loads((root / "sync/reader_hold.json").read_text(encoding="utf-8"))}


def _run_cleanup_candidates(root, residue, baseline_logical):
    results = {}
    for label, read_page in (("connect_close", False), ("page_read_close", True)):
        db = copy_sqlite_set(root, residue, root / label)
        before_main = file_snapshot(db)
        candidate = sqlite_managed_candidate(db, read_page=read_page)
        after_main = file_snapshot(db)
        entry = {"operation": "writable SQLite connection; explicit close",
                 "read_page": read_page, **candidate,
                 "main_bytes_unchanged": before_main["sha256"] == after_main["sha256"],
                 "sidecars_removed": not any(os.path.lexists(str(db) + suffix) for suffix in SIDECARS)}
        if entry["sidecars_removed"]:
            entry["logical"] = logical_snapshot(db, immutable=True)
            entry["logical_matches_baseline"] = entry["logical"] == baseline_logical
            entry["qualification"] = qualify_fixture(root, db, label)
            entry["after_qualification"] = db_snapshot(db)
            conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("SELECT value FROM metadata LIMIT 1").fetchone()
            finally:
                conn.close()
            entry["after_general_read_only_reopen"] = db_snapshot(db)
        else:
            entry["logical_matches_baseline"] = None
            entry["qualification"] = qualify_fixture(root, db, label)
        results[label] = entry
    return results


def _run_wal_counterexample(root, baseline):
    db = copy_sqlite_set(root, baseline, root / "committed_wal")
    clean_main_hash = file_snapshot(db)["sha256"]
    with started_worker(root, "abrupt_wal", db) as (process, command):
        process.wait(timeout=CHILD_TIMEOUT_SECONDS)
        if process.returncode:
            raise RuntimeError(f"WAL worker failed: {process.returncode}")
    worker_before_exit = json.loads((root / "sync/abrupt_wal.json").read_text(encoding="utf-8"))
    after_exit = db_snapshot(db)
    main_only_dir = safe_lab_path(root, root / "committed_wal_main_only")
    main_only_dir.mkdir()
    main_only = main_only_dir / "raw.db"
    shutil.copy2(db, main_only)
    full_logical = logical_snapshot(db)
    after_full_read = db_snapshot(db)
    main_only_logical = logical_snapshot(main_only, immutable=True)
    return {
        "command": command,
        "before_abrupt_process_exit": worker_before_exit["before_abrupt_exit"],
        "after_abrupt_process_exit_before_reopen": after_exit,
        "main_sha256_unchanged_from_clean_fixture": after_exit[db.name]["sha256"] == clean_main_hash,
        "full_file_set_marker": full_logical["wal_marker_rows"],
        "after_full_set_read": after_full_read,
        "main_only_marker": main_only_logical["wal_marker_rows"],
        "qualification": qualify_fixture(root, db, "committed_wal_rejected"),
        "classification": "reject: non-empty committed WAL is database state, not disposable residue",
    }


def _attempt(callable_):
    try:
        value = callable_()
        return {"status": "succeeded", "value": value}
    except BaseException as exc:
        return {"status": "blocked", "error": f"{type(exc).__name__}: {exc}",
                "winerror": getattr(exc, "winerror", None),
                "sqlite_errorcode": getattr(exc, "sqlite_errorcode", None)}


def _sealed_probe(path):
    with sealed_source(path):
        return "sealed"


def _writable_write_probe(path):
    conn = sqlite3.connect(path, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS own_writer_probe(value TEXT)")
        conn.rollback()
        return "write transaction acquired"
    finally:
        conn.close()


def _run_locking(root, baseline):
    results = {"sealed_source_createfile": {
        "desired_access": "GENERIC_READ (0x80000000)",
        "share_mode": "FILE_SHARE_READ (0x00000001); write/delete sharing denied",
    }}
    db = copy_sqlite_set(root, baseline, root / "lock_plain_handle")
    with db.open("r+b"):
        results["existing_write_capable_main_handle"] = _attempt(
            lambda: _sealed_probe(db))

    db = copy_sqlite_set(root, baseline, root / "lock_read_only_sqlite")
    ro = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0)
    try:
        ro.execute("SELECT value FROM metadata LIMIT 1").fetchone()
        results["open_sqlite_read_only_connection"] = _attempt(
            lambda: _sealed_probe(db))
    finally:
        ro.close()

    db = copy_sqlite_set(root, baseline, root / "lock_active_writer")
    writer = sqlite3.connect(db, timeout=0)
    try:
        writer.execute("BEGIN IMMEDIATE")
        results["active_sqlite_writer"] = _attempt(lambda: _sealed_probe(db))
    finally:
        writer.rollback()
        writer.close()

    db = copy_sqlite_set(root, baseline, root / "lock_sidecar_handle")
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("SELECT value FROM metadata LIMIT 1").fetchone()
    finally:
        conn.close()
    wal = Path(str(db) + "-wal")
    with wal.open("rb"):
        results["open_wal_handle_then_page_read_cleanup"] = _attempt(
            lambda: sqlite_managed_candidate(db, read_page=True))
    results["open_wal_handle_after_release"] = db_snapshot(db)

    db = copy_sqlite_set(root, baseline, root / "late_writer")
    with started_worker(root, "late_writer", db) as (process, command):
        with sealed_source(db):
            _touch(root / "sync/late_writer.start")
            process.wait(timeout=CHILD_TIMEOUT_SECONDS)
        results["writer_started_after_seal"] = json.loads(
            (root / "sync/late_writer.json").read_text(encoding="utf-8")) | {"command": command}

    db = copy_sqlite_set(root, baseline, root / "self_reconnect")
    with sealed_source(db):
        results["own_writable_sqlite_connection_while_sealed"] = _attempt(
            lambda: _writable_write_probe(db))

    db = copy_sqlite_set(root, baseline, root / "release_reconnect_race")
    with started_worker(root, "race_writer", db) as (process, command):
        with sealed_source(db):
            pass
        _touch(root / "sync/race_writer.start")
        _wait_for(root / "sync/race_writer.acquired")
        results["cleanup_reconnect_after_competing_writer_entered"] = _attempt(
            lambda: sqlite_managed_candidate(db, read_page=True))
        _touch(root / "sync/race_writer.release")
        process.wait(timeout=CHILD_TIMEOUT_SECONDS)
        results["competing_writer"] = json.loads(
            (root / "sync/race_writer.json").read_text(encoding="utf-8")) | {"command": command}
    return results


def _run_rejections(root, baseline):
    results = {}
    db = copy_sqlite_set(root, baseline, root / "rollback_journal")
    with started_worker(root, "rollback_journal", db) as (process, command):
        process.wait(timeout=CHILD_TIMEOUT_SECONDS)
    results["rollback_journal"] = {"command": command, "snapshot": db_snapshot(db),
        "qualification": qualify_fixture(root, db, "rollback_journal_rejected")}

    incomplete = safe_lab_path(root, root / "incomplete" / "raw.db")
    incomplete.parent.mkdir()
    session = CaptureSession(incomplete, source="sidecar-lab", session_id="sidecar-lab",
        market_date="2026-09-21", feed_scope="synthetic_fixture",
        price_policy="signed_magnitude", direction_policy="signed_volume",
        started_ns=0, started_at_utc="2026-09-21T00:00:00Z")
    session.__exit__(None, None, None)
    results["incomplete_raw"] = {"snapshot": db_snapshot(incomplete),
        "qualification": qualify_fixture(root, incomplete, "incomplete_rejected")}

    unknown = copy_sqlite_set(root, baseline, root / "unknown_sidecar")
    Path(str(unknown) + "-shm").write_bytes(b"synthetic-unknown-origin")
    results["unknown_sidecar"] = {"snapshot": db_snapshot(unknown),
        "qualification": qualify_fixture(root, unknown, "unknown_sidecar_rejected"),
        "note": "manually created rejection counterexample; not reported as SQLite residue"}

    link_parent = safe_lab_path(root, root / "reparse_parent")
    target_parent = safe_lab_path(root, root / "reparse_target")
    target_parent.mkdir()
    linked_db = copy_sqlite_set(root, baseline, target_parent / "fixture")
    created = None
    try:
        try:
            os.symlink(target_parent, link_parent, target_is_directory=True)
            created = {"method": "directory_symlink"}
        except OSError as symlink_error:
            command = ["cmd", "/d", "/c", "mklink", "/J", str(link_parent), str(target_parent)]
            completed = subprocess.run(command, check=True, capture_output=True, text=True,
                                       timeout=CHILD_TIMEOUT_SECONDS)
            created = {"method": "directory_junction", "command": command,
                       "symlink_error": f"{type(symlink_error).__name__}: {symlink_error}",
                       "stdout": completed.stdout.strip()}
        results["reparse_path"] = _attempt(
            lambda: _require_local_ntfs(link_parent / "fixture" / linked_db.name)) | created
    except (OSError, subprocess.SubprocessError) as exc:
        results["reparse_path"] = {"status": "not_reproduced",
            "error": f"{type(exc).__name__}: {exc}", "winerror": getattr(exc, "winerror", None)}
    finally:
        if created is not None and os.path.lexists(link_parent):
            # This is the verified disposable link itself, never its target.
            link_parent.rmdir()

    namespace = safe_lab_path(root, root / "path_replacement")
    namespace.mkdir()
    path_a = copy_sqlite_set(root, baseline, namespace / "a")
    path_b = copy_sqlite_set(root, baseline, namespace / "b")
    before = file_snapshot(path_a)
    held = sealed_source(path_a)
    with held:
        during = file_snapshot(path_a)
    displaced = path_a.with_name("original.db")
    path_a.rename(displaced)
    path_b.rename(path_a)
    after = file_snapshot(path_a)
    results["path_replacement_after_guard_release"] = {
        "before": before, "during": during, "after": after,
        "identity_changed": before["identity"] != after["identity"],
        "conclusion": "a file handle does not continuously protect the parent namespace after release",
    }
    return results


def run_lab():
    root = create_lab_root()
    evidence_path = root / "result.json"
    evidence = {"schema": SCHEMA, "status": "running", "started_at_utc": utc_now(),
                "root": str(root), "limits": {"per_db": MAX_DB_BYTES,
                "all_fixtures": MAX_TOTAL_BYTES, "child_timeout_seconds": CHILD_TIMEOUT_SECONDS}}
    _replace_json(evidence_path, evidence)
    try:
        source = sqlite3.connect(":memory:")
        try:
            sqlite_source_id = source.execute("SELECT sqlite_source_id()").fetchone()[0]
        finally:
            source.close()
        evidence["environment"] = {
            "windows": platform.platform(), "python_executable": sys.executable,
            "python_version": sys.version, "python_bitness": platform.architecture()[0],
            "sqlite_version": sqlite3.sqlite_version, "sqlite_source_id": sqlite_source_id,
            "filesystem": _require_local_ntfs(root),
            "code_revision": subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
                check=True, capture_output=True, text=True, timeout=10).stdout.strip(),
            "git_status_short": subprocess.run(["git", "status", "--short"], cwd=PROJECT_ROOT,
                check=True, capture_output=True, text=True, timeout=10).stdout.splitlines(),
            "code_sha256": {
                "scripts/lab_raw_v2_sidecars.py": _sha256(Path(__file__).resolve()),
                "collector/raw_v2.py": _sha256(PROJECT_ROOT / "collector/raw_v2.py"),
                "collector/raw_archive.py": _sha256(PROJECT_ROOT / "collector/raw_archive.py"),
                "collector/raw_v2_qualification.py": _sha256(
                    PROJECT_ROOT / "collector/raw_v2_qualification.py"),
            },
            "command": [sys.executable, str(Path(__file__).resolve())],
        }
        baseline, evidence["normal_shutdown"] = _run_baseline(root)
        baseline_logical = evidence["normal_shutdown"]["logical"]
        residue, evidence["read_only_residue"] = _run_read_only_residue(root, baseline)
        evidence["cleanup_candidates"] = _run_cleanup_candidates(root, residue, baseline_logical)
        evidence["committed_wal_counterexample"] = _run_wal_counterexample(root, baseline)
        evidence["locking_and_races"] = _run_locking(root, baseline)
        evidence["rejections_and_reopen"] = _run_rejections(root, baseline)
        evidence["total_fixture_bytes"] = enforce_budget(root)
        evidence["assessment"] = {
            "synthetic_experiment": "partially_unverified",
            "production_application": "not_approved",
            "candidate": "writable SQLite page read followed by explicit cursor/connection close, only on an isolated quiescent consistent residue-only fixture",
            "blocking_gap": "sealed_source must be released before SQLite writable recovery; no continuous exclusion against a non-cooperating late writer was established",
        }
        evidence["status"] = "completed"
        evidence["finished_at_utc"] = utc_now()
    except BaseException as exc:
        evidence["status"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        evidence["finished_at_utc"] = utc_now()
        _replace_json(evidence_path, evidence)
        raise
    _replace_json(evidence_path, evidence)
    return evidence_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("baseline", "reader_hold", "abrupt_wal",
                        "rollback_journal", "late_writer", "race_writer"), help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--db", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        if args.root is None or args.db is None:
            parser.error("internal worker requires --root and --db")
        return _worker(args.root, args.worker, args.db)
    if args.root is not None or args.db is not None:
        parser.error("the lab does not accept an external root or database")
    result = run_lab()
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
