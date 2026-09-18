"""Bounded Windows raw-v2 archive prototype. Never changes/deletes a source."""
from contextlib import contextmanager
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import zlib

from collector.kiwoom.collector_lease import CollectorLease
from collector.raw_v2 import CaptureControl, read_raw_v2
from control_tower.storage_guard import MIN_FREE_BYTES, require_disk_space

MAX_BYTES = 32 * 1024 * 1024
CHUNK = 1024 * 1024
SCHEMA = "raw_archive_prototype_1"


@contextmanager
def _sealed_source(path):
    # Windows share mode denies existing and future write/delete handles.
    # Unlike a SQLite read transaction this also protects the exact file bytes.
    if os.name != "nt":
        raise OSError("this prototype requires Windows file sharing locks")
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        yield stream


def _no_sidecars(path):
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.lexists(str(path) + suffix):
            raise ValueError("SQLite sidecar exists; preserve it and resolve closure separately")


def _transfer(source, destination=None, *, limit=MAX_BYTES):
    digest, size = hashlib.sha256(), 0
    while block := source.read(min(CHUNK, limit - size + 1)):
        size += len(block)
        if size > limit:
            raise ValueError("prototype byte limit exceeded")
        digest.update(block)
        if destination is not None:
            destination.write(block)
    return dict(size=size, sha256=digest.hexdigest())


def _sync(stream):
    stream.flush()
    os.fsync(stream.fileno())


def _publish(partial, final):
    # Atomic new name; unlike replace(), a conflicting destination is preserved.
    os.link(partial, final)
    partial.unlink()


def _json_publish(directory, name, value):
    partial = directory / (name + ".partial")
    with partial.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        _sync(stream)
    _publish(partial, directory / name)


def _validate_raw(path):
    # Bound JSON materialization too, not just compression buffers. This is a
    # private restored copy; never open the original SQLite file here.
    conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        conn.execute("PRAGMA cache_size=-2048")
        count, largest = conn.execute(
            "SELECT count(*), max(length(CAST(payload AS BLOB))) FROM events").fetchone()
        if count > 100_000 or (largest or 0) > 64 * 1024:
            raise ValueError("prototype raw reader record budget exceeded")
    finally:
        conn.close()
    counts = {}
    with read_raw_v2(path) as (meta, rows):
        for row in rows:
            event = row["event"]
            if isinstance(event, CaptureControl):
                counts[event.control_type] = counts.get(event.control_type, 0) + 1
    return meta, counts


def _inflate(archive, output, expected):
    with gzip.open(archive, "rb") as compressed, output.open("xb") as restored:
        actual = _transfer(compressed, restored, limit=expected["size"])
        _sync(restored)
    if actual != expected:
        raise ValueError("restored byte size/hash mismatch")
    meta, controls = _validate_raw(output)
    # The existing SQLite reader may use WAL sidecars only on this private copy.
    with output.open("rb") as stream:
        if _transfer(stream) != expected:
            raise ValueError("raw validation changed restored bytes")
    return meta, controls


def archive_raw(source, destination, *, root, session_id, closure_note):
    """closure_note is an operator evidence reference, not automated proof."""
    source = Path(source).resolve(strict=True)
    destination = Path(destination).resolve()
    if not session_id.strip() or not closure_note.strip() or len(closure_note) > 8192:
        raise ValueError("session and bounded closure evidence note required")
    if destination.parent == source.parent:
        raise ValueError("separate archive directory required")
    # Fail before hashing or SQLite access for real large datasets.
    size = source.stat().st_size
    if not 0 < size <= MAX_BYTES:
        raise ValueError("prototype accepts only 1 byte to 32 MiB")
    with CollectorLease(root), _sealed_source(source) as stream:
        _no_sidecars(source)
        before = os.fstat(stream.fileno())
        if not 0 < before.st_size <= MAX_BYTES:
            raise ValueError("prototype accepts only 1 byte to 32 MiB")
        require_disk_space(destination, minimum=MIN_FREE_BYTES + 3 * before.st_size)
        destination.mkdir(parents=True, exist_ok=False)
        compressed = destination / "raw.db.gz.partial"
        with compressed.open("xb") as out:
            with gzip.GzipFile(filename="", mode="wb", fileobj=out,
                               compresslevel=6, mtime=0) as zipped:
                original = _transfer(stream, zipped)
            _sync(out)
        restored = destination / "verification.db.partial"
        meta, controls = _inflate(compressed, restored, original)
        if meta["session_id"] != session_id:
            raise ValueError("closure session mismatch")
        _no_sidecars(source)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("source changed")
        with compressed.open("rb") as check:
            packed = _transfer(check, limit=MAX_BYTES + CHUNK)
        restored.unlink()
        _publish(compressed, destination / "raw.db.gz")
        receipt = dict(schema=SCHEMA, status="verified", created_at=datetime.now(timezone.utc).isoformat(),
            source=dict(path=str(source), mtime_ns=before.st_mtime_ns, **original),
            raw_manifest=meta, closure_note=closure_note,
            compression=dict(format="gzip", level=6, python=platform.python_version(),
                             zlib=zlib.ZLIB_RUNTIME_VERSION, **packed),
            verification=dict(bytes="sha256_match", raw_reader="fully_consumed",
                              control_counts=controls, research_quality="not_certified"))
        _json_publish(destination, "archive.json", receipt)
        return receipt


def restore_raw(bundle, destination):
    bundle, destination = Path(bundle).resolve(strict=True), Path(destination).resolve()
    with (bundle / "archive.json").open("rb") as stream:
        data = stream.read(128 * 1024 + 1)
    if len(data) > 128 * 1024:
        raise ValueError("archive receipt too large")
    receipt = json.loads(data)
    if receipt.get("schema") != SCHEMA or receipt.get("status") != "verified":
        raise ValueError("verified archive receipt required")
    expected = {key: receipt["source"][key] for key in ("size", "sha256")}
    if type(expected["size"]) is not int or not 0 < expected["size"] <= MAX_BYTES:
        raise ValueError("prototype restore limit exceeded")
    if receipt["compression"]["format"] != "gzip":
        raise ValueError("unsupported compression")
    if destination == bundle or destination == Path(receipt["source"]["path"]).parent:
        raise ValueError("separate restore directory required")
    require_disk_space(destination, minimum=MIN_FREE_BYTES + expected["size"])
    with _sealed_source(bundle / "raw.db.gz") as stream:
        packed = _transfer(stream, limit=MAX_BYTES + CHUNK)
        if packed != {key: receipt["compression"][key] for key in ("size", "sha256")}:
            raise ValueError("compressed byte size/hash mismatch")
        destination.mkdir(parents=True, exist_ok=False)
        partial = destination / "raw.db.partial"
        meta, controls = _inflate(bundle / "raw.db.gz", partial, expected)
        if meta != receipt["raw_manifest"] or controls != receipt["verification"]["control_counts"]:
            raise ValueError("restored raw contract mismatch")
        _publish(partial, destination / "raw.db")
    return destination / "raw.db"
