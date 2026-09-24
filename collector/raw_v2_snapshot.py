"""Fail-closed frozen snapshot acquisition for a known raw-v2 residue pattern.

This module is the production-facing counterpart of the synthetic clone lab. It
never opens the source SQLite database. On Windows/local NTFS it acquires
exclusive read handles for the main DB, zero-byte WAL and 32 KiB SHM together,
pins relevant directory identities, streams each held source member once into
two fresh destinations (raw evidence + working copy), and only after releasing
the source handles lets SQLite touch the working copy.

The working cleanup is accepted only when all copied SQLite sidecars disappear,
the working main-file SHA-256 still equals the digest observed from the sealed
source stream, and the expected raw-v2 session id is present. Success means only
"safe input candidate for bounded prefix qualification". It is not whole-stream
qualification, research eligibility, or strategy-performance approval.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import platform
import stat
import uuid

from collector.raw_v2 import SUPPORTED_SCHEMAS, _identity, _load_json
from collector.raw_v2_qualification import _require_local_ntfs


SCHEMA = "raw_v2_frozen_snapshot_v1"
RESIDUE_POLICY = "zero-wal-32768-shm-v1"
SIDECARS = ("-wal", "-shm", "-journal")
COPY_CHUNK_BYTES = 8 * 1024 * 1024
MAX_MAIN_BYTES = 64 * 1024 * 1024 * 1024
FREE_SPACE_MARGIN_BYTES = 2 * 1024 * 1024 * 1024
DIRECTORY_ACCESS = 1  # FILE_LIST_DIRECTORY


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, value, *, replace=False):
    path = Path(path)
    temporary = path.with_name(path.name + ".partial")
    mode = "w" if replace else "x"
    with temporary.open(mode, encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        os.replace(temporary, path)
    else:
        temporary.replace(path)


def _error(exc):
    message = f"{type(exc).__name__}: {exc}"
    return message[:4096] + ("..." if len(message) > 4096 else "")


def _kernel():
    if os.name != "nt":
        raise OSError("frozen snapshot acquisition requires Windows sharing semantics")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _open_handle(path, *, directory):
    kernel = _kernel()
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    access, sharing = (DIRECTORY_ACCESS, 3) if directory else (0x80000000, 0)
    flags = 0x00200000 | (0x02000000 if directory else 0x80)
    handle = create(str(path), access, sharing, None, 3, flags, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _close_handle(handle):
    close = _kernel().CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    if not close(handle):
        raise ctypes.WinError(ctypes.get_last_error())


class _Info(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("created", wintypes.FILETIME),
        ("accessed", wintypes.FILETIME),
        ("written", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


def _handle_identity(handle):
    info = _Info()
    query = _kernel().GetFileInformationByHandle
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Info)]
    query.restype = wintypes.BOOL
    if not query(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if info.attributes & 0x400:
        raise ValueError("reparse handle rejected")
    return info.volume, info.index_high, info.index_low


@contextmanager
def _pinned_directory(path):
    handle = _open_handle(path, directory=True)
    try:
        identity = _handle_identity(handle)

        def check():
            other = _open_handle(path, directory=True)
            try:
                if _handle_identity(other) != identity:
                    raise ValueError("directory path/handle identity changed")
            finally:
                _close_handle(other)

        check()
        yield check
    finally:
        _close_handle(handle)


@contextmanager
def _exclusive_stream(path):
    import msvcrt

    handle = _open_handle(path, directory=False)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _close_handle(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        yield stream


def _stat_key(info):
    return (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink,
    )


def _path_record(path):
    info = Path(path).stat()
    return {
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "st_dev": info.st_dev,
        "st_ino": info.st_ino,
        "st_nlink": info.st_nlink,
    }


def _same_file_record(path, expected):
    return _path_record(path) == expected


def _source_members(path, *, residue_policy):
    if residue_policy != RESIDUE_POLICY:
        raise ValueError("unsupported residue policy")
    path = Path(path).absolute()
    _require_local_ntfs(path)
    if not path.is_file():
        raise ValueError("regular raw database file required")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("single-link regular main database required")
    if not 0 < info.st_size <= MAX_MAIN_BYTES:
        raise ValueError("main database outside supported 64 GiB acquisition envelope")

    wal = Path(str(path) + "-wal")
    shm = Path(str(path) + "-shm")
    journal = Path(str(path) + "-journal")
    if os.path.lexists(journal):
        raise ValueError("rollback journal is not an accepted residue candidate")
    if not wal.is_file() or not shm.is_file():
        raise ValueError("zero-WAL/32KiB-SHM residue policy requires both sidecars")
    for item in (wal, shm):
        _require_local_ntfs(item)
        item_info = item.stat()
        if not stat.S_ISREG(item_info.st_mode) or item_info.st_nlink != 1:
            raise ValueError("single-link regular SQLite sidecars required")
    if wal.stat().st_size != 0:
        raise ValueError("non-empty WAL may contain database state and is rejected")
    if shm.stat().st_size != 32768:
        raise ValueError("SHM must be exactly 32768 bytes for this residue policy")
    return path, {
        path.name: path,
        wal.name: wal,
        shm.name: shm,
    }


def _member_stat_records(members):
    return {name: _path_record(path) for name, path in members.items()}


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(COPY_CHUNK_BYTES):
            digest.update(block)
    return digest.hexdigest()


def _code_provenance():
    root = Path(__file__).resolve().parents[1]
    names = (
        "collector/raw_v2_snapshot.py",
        "collector/raw_v2.py",
        "collector/raw_v2_qualification.py",
    )
    return {name: _hash_file(root / name) for name in names}


def _copy_held_member(name, source_path, stream, evidence_dir, working_dir):
    before = os.fstat(stream.fileno())
    if before.st_nlink != 1 or not stat.S_ISREG(before.st_mode):
        raise ValueError("held source is not a single-link regular file")
    if _stat_key(before) != _stat_key(source_path.stat()):
        raise ValueError("source path/held-handle identity mismatch")
    stream.seek(0)
    digest = hashlib.sha256()
    total = 0
    evidence = evidence_dir / name
    working = working_dir / name
    with evidence.open("xb") as raw, working.open("xb") as work:
        while block := stream.read(COPY_CHUNK_BYTES):
            total += len(block)
            if total > before.st_size:
                raise ValueError("source grew while exclusive handle was held")
            if raw.write(block) != len(block) or work.write(block) != len(block):
                raise OSError("short snapshot destination write")
            digest.update(block)
        if total != before.st_size:
            raise ValueError("source byte count changed during snapshot copy")
        for destination in (raw, work):
            destination.flush()
            os.fsync(destination.fileno())
    after = os.fstat(stream.fileno())
    if _stat_key(before) != _stat_key(after):
        raise ValueError("held source metadata changed during snapshot copy")
    if evidence.stat().st_size != total or working.stat().st_size != total:
        raise ValueError("snapshot destination size mismatch")
    return {
        "source": {
            "path": str(source_path),
            **_path_record(source_path),
            "sha256": digest.hexdigest(),
        },
        "evidence": {
            "path": str(evidence),
            "size": evidence.stat().st_size,
            "sha256_from_sealed_stream": digest.hexdigest(),
            "readback_verified": False,
        },
        "working": {
            "path": str(working),
            "size": working.stat().st_size,
            "sha256_from_sealed_stream": digest.hexdigest(),
            "readback_verified": False,
        },
    }


def _read_working_manifest_and_cleanup(path):
    """SQLite may touch only the working copy. No write SQL is issued."""
    path = Path(path)
    conn = sqlite3.connect(path, timeout=0)
    cursor = None
    try:
        cursor = conn.execute("SELECT value FROM metadata LIMIT 2")
        rows = cursor.fetchall()
    finally:
        if cursor is not None:
            cursor.close()
        conn.close()
    if len(rows) != 1:
        raise ValueError("working copy must contain exactly one raw manifest")
    manifest = _load_json(rows[0][0])
    _identity(manifest)
    if manifest.get("schema") not in SUPPORTED_SCHEMAS:
        raise ValueError("unsupported raw-v2 schema in working copy")
    return manifest


def _sidecars(path):
    return {
        suffix: {
            "exists": os.path.lexists(str(path) + suffix),
            "size": (
                Path(str(path) + suffix).stat().st_size
                if os.path.lexists(str(path) + suffix) else None
            ),
        }
        for suffix in SIDECARS
    }


@dataclass(frozen=True)
class SnapshotPaths:
    run_dir: Path
    result: Path
    evidence_main: Path
    working_main: Path


def acquire_frozen_snapshot(source, *, output_root, expected_session_id,
                            residue_policy=RESIDUE_POLICY):
    """Acquire and normalize one raw-v2 snapshot without opening source SQLite.

    The output root must already exist on local NTFS and must not be within the
    source directory. All success/failure artifacts are retained. The function
    never deletes source or destination files.
    """
    if not isinstance(expected_session_id, str) or not expected_session_id.strip():
        raise ValueError("expected session id required")
    source = Path(source).absolute()
    source, initial_members = _source_members(source, residue_policy=residue_policy)
    source_parent = source.parent.absolute()

    output_root = Path(output_root).absolute()
    if not output_root.is_dir():
        raise ValueError("existing output directory required")
    _require_local_ntfs(output_root)
    try:
        output_root.relative_to(source_parent)
    except ValueError:
        pass
    else:
        raise ValueError("snapshot output must not be inside the source directory")

    source_total = sum(path.stat().st_size for path in initial_members.values())
    required_free = source_total * 2 + FREE_SPACE_MARGIN_BYTES
    free = shutil.disk_usage(output_root).free
    if free < required_free:
        raise ValueError(
            f"insufficient snapshot space: free={free} required={required_free}"
        )

    run_id = uuid.uuid4().hex
    run_dir = output_root / f"raw_v2_snapshot_{run_id}"
    run_dir.mkdir(exist_ok=False)
    evidence_dir = run_dir / "evidence"
    working_dir = run_dir / "working"
    evidence_dir.mkdir()
    working_dir.mkdir()
    result_path = run_dir / "result.json"
    paths = SnapshotPaths(
        run_dir=run_dir,
        result=result_path,
        evidence_main=evidence_dir / source.name,
        working_main=working_dir / source.name,
    )
    report = {
        "schema": SCHEMA,
        "run_id": run_id,
        "status": "running",
        "started_at_utc": _utc_now(),
        "source": str(source),
        "expected_session_id": expected_session_id,
        "residue_policy": residue_policy,
        "declared_source_sqlite_policy": "never_open_source_sqlite",
        "source_unchanged_during_acquisition": None,
        "exclusive_source_members_acquired": False,
        "snapshot_ready_for_prefix_qualification": False,
        "whole_stream_assessed": False,
        "research_eligible": False,
        "performance_research_eligible": False,
        "free_space": {"observed": free, "required": required_free},
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
        },
        "code_provenance": _code_provenance(),
        "paths": {
            "run_dir": str(run_dir),
            "evidence_main": str(paths.evidence_main),
            "working_main": str(paths.working_main),
        },
        "copy_records": {},
        "working_cleanup": None,
        "error": None,
    }
    _write_json(result_path, report)

    source_stat_before = _member_stat_records(initial_members)
    try:
        # Keep destination namespace pins for the entire acquisition + cleanup.
        # Source handles live only in the nested stack and are released before
        # SQLite is allowed to touch the disposable working copy.
        with ExitStack() as output_stack:
            output_check = output_stack.enter_context(_pinned_directory(output_root))
            run_check = output_stack.enter_context(_pinned_directory(run_dir))
            evidence_check = output_stack.enter_context(_pinned_directory(evidence_dir))
            working_check = output_stack.enter_context(_pinned_directory(working_dir))

            with ExitStack() as source_stack:
                source_parent_check = source_stack.enter_context(_pinned_directory(source_parent))
                current_source, members = _source_members(
                    source, residue_policy=residue_policy
                )
                if current_source != source or _member_stat_records(members) != source_stat_before:
                    raise ValueError("source file set changed before exclusive acquisition")
                streams = {
                    name: source_stack.enter_context(_exclusive_stream(path))
                    for name, path in sorted(members.items())
                }
                report["exclusive_source_members_acquired"] = True
                source_parent_check()
                output_check()
                run_check()
                evidence_check()
                working_check()
                if _member_stat_records(members) != source_stat_before:
                    raise ValueError("source file set changed after exclusive acquisition")

                records = {}
                for name in sorted(members):
                    records[name] = _copy_held_member(
                        name, members[name], streams[name], evidence_dir, working_dir
                    )
                report["copy_records"] = records

                source_parent_check()
                output_check()
                run_check()
                evidence_check()
                working_check()
                _, final_members = _source_members(source, residue_policy=residue_policy)
                final_stats = _member_stat_records(final_members)
                if final_stats != source_stat_before:
                    raise ValueError("source file set changed during snapshot acquisition")
                report["source_unchanged_during_acquisition"] = True
                report["source_stat_before"] = source_stat_before
                report["source_stat_after"] = final_stats

            # Source handles are released here. Destination namespace pins remain.
            main_record = report["copy_records"][source.name]
            manifest = _read_working_manifest_and_cleanup(paths.working_main)
            report["working_cleanup"] = {
                "manifest": manifest,
                "sidecars_after_close": _sidecars(paths.working_main),
            }
            if manifest["session_id"] != expected_session_id:
                raise ValueError("working raw manifest session does not match expected session")
            if any(item["exists"] for item in report["working_cleanup"]["sidecars_after_close"].values()):
                raise ValueError("SQLite sidecar remains on working copy after managed close")

            working_hash = _hash_file(paths.working_main)
            report["working_cleanup"]["main_sha256_after_close"] = working_hash
            report["working_cleanup"]["main_readback_verified"] = True
            if working_hash != main_record["source"]["sha256"]:
                raise ValueError("working main bytes changed during sidecar cleanup")

            # Sidecar evidence is tiny and independently read back. The 50 GiB
            # evidence main is retained but not re-read here; its hash was computed
            # from the sealed source stream while both destinations were written.
            evidence_sidecars = {}
            for name in sorted(report["copy_records"]):
                if name == source.name:
                    continue
                path = evidence_dir / name
                digest = _hash_file(path)
                evidence_sidecars[name] = {
                    "path": str(path),
                    "size": path.stat().st_size,
                    "sha256": digest,
                }
                if digest != report["copy_records"][name]["source"]["sha256"]:
                    raise ValueError("evidence sidecar readback hash mismatch")
            report["evidence_sidecar_readback"] = evidence_sidecars

            output_check()
            run_check()
            evidence_check()
            working_check()
            report["status"] = "snapshot_ready"
            report["snapshot_ready_for_prefix_qualification"] = True
            report["finished_at_utc"] = _utc_now()
    except BaseException as exc:
        report["status"] = "failed"
        report["snapshot_ready_for_prefix_qualification"] = False
        report["error"] = _error(exc)
        report["finished_at_utc"] = _utc_now()
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            _write_json(result_path, report, replace=True)
            raise
    _write_json(result_path, report, replace=True)
    return paths
