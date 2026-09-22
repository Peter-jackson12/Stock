"""보호 handle -> 원시 증거/작업 복제본의 Windows 합성 lab.

운영 입력 CLI가 아니다. 스스로 만든 UUID/TEMP fixture만 처리한다.
원본 SQLite 재연결, 원본 정리, 자동 재시도, 운영 후보 승격은 없다.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import stat
import sys
import time
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collector.kiwoom.capture_session import CaptureSession
from collector.raw_archive import sealed_source
from scripts import lab_raw_v2_sidecars as lab

SCHEMA = "raw_v2_clone_lab_v1"
CHUNK = 64 * 1024
MAX_IO_BYTES = 16 * lab.MAX_DB_BYTES
MAX_SECONDS = 20


@dataclass(frozen=True)
class Fixture:
    path: Path
    files: dict
    logical: dict
    qualification: dict


def make_fixture(root, *, unsigned=False, finish=True):
    """출처는 이 생산 단계의 관측이다. 외부 경로의 자체 주장으로 대체하지 않는다."""
    root = lab.require_lab_root(root)
    path = lab.safe_lab_path(root, root / "source" / "raw.db")
    path.parent.mkdir()
    with CaptureSession(
        path, source="sidecar-lab", session_id="sidecar-lab",
        market_date="2026-09-21", feed_scope="synthetic_fixture",
        price_policy="signed_magnitude", direction_policy="signed_volume",
        started_ns=0, started_at_utc="2026-09-21T00:00:00Z",
    ) as session:
        session.on_tick(
            code="005930", venue="unknown", real_type="주식체결",
            fids={"10": "+10001", "15": "7" if unsigned else "+7", "20": "090001"},
            received_ns=1, received_at_utc="2026-09-21T00:00:01Z",
        )
        session.commit()
        if finish:
            session.finish(10)
    assert not any(os.path.lexists(str(path) + s) for s in lab.SIDECARS)
    logical = lab.logical_snapshot(path, immutable=True)
    qualification = lab.qualify_fixture(root, path, "before_residue")
    before = lab.file_snapshot(path)
    # fixture 생산 단계에서만 reader 잔여물의 출처를 만든다.
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
    try:
        conn.execute("SELECT value FROM metadata LIMIT 1").fetchone()
    finally:
        conn.close()
    files = lab.db_snapshot(path)
    assert files[path.name] == before
    assert files[path.name + "-wal"]["size"] == 0
    assert files[path.name + "-shm"]["size"] == 32768
    lab._write_json(root / "fixture.json", {
        "schema": SCHEMA, "origin": "created_here_and_closed_reader_observed",
        "files": files, "logical": logical, "qualification": qualification,
    })
    lab.enforce_budget(root)
    return Fixture(path, files, logical, qualification)


def _kernel():
    if os.name != "nt":
        raise OSError("clone lab requires Windows sharing semantics")
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _open_handle(path, *, directory):
    kernel = _kernel()
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    # 파일은 read/write/delete를 모두 배제하고 디렉터리는 기존 이름의 delete/rename을 막는다.
    # OPEN_REPARSE_POINT로 최종 성분을 암묵 추적하지 않는다. 상위 경로도 별도 검사한다.
    access, sharing = (0, 3) if directory else (0x80000000, 0)
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
    _fields_ = [("attributes", wintypes.DWORD), ("created", wintypes.FILETIME),
                ("accessed", wintypes.FILETIME), ("written", wintypes.FILETIME),
                ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]


def _handle_identity(handle):
    info = _Info()
    query = _kernel().GetFileInformationByHandle
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Info)]
    query.restype = wintypes.BOOL
    if not query(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if info.attributes & 0x400:
        raise ValueError("reparse handle rejected")
    return (info.volume, info.index_high, info.index_low)


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
def _exclusive_source(path):
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
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)


def _inventory(root, path):
    path = lab.safe_lab_path(root, path, must_exist=True)
    actual = {p.name: p for p in path.parent.iterdir()}
    allowed = {path.name, path.name + "-wal", path.name + "-shm"}
    if path.name + "-journal" in actual:
        raise ValueError("rollback journal is not a residue candidate")
    if set(actual) != allowed:
        raise ValueError("unknown or incomplete source file set")
    for item in actual.values():
        lab.safe_lab_path(root, item, must_exist=True)
        info = item.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("regular non-aliased single-link file required")
        if not 0 <= info.st_size <= lab.MAX_DB_BYTES:
            raise ValueError("source file exceeds lab byte budget")
    if actual[path.name + "-wal"].stat().st_size != 0:
        raise ValueError("non-empty WAL is database state, not residue")
    if actual[path.name + "-shm"].stat().st_size != 32768:
        raise ValueError("unexpected SHM size; provenance still required")
    return actual


def clone_fixture(root, fixture, *, hook=None):
    """합성 fixture 전용. hook은 결정적 실패 주입 전용이다."""
    root = lab.require_lab_root(root)
    source = lab.safe_lab_path(root, fixture.path, must_exist=True)
    run_id = uuid.uuid4().hex
    run = lab.safe_lab_path(root, root / ("clone_" + run_id))
    run.mkdir(exist_ok=False)
    evidence, working = run / "evidence", run / "working"
    evidence.mkdir()
    working.mkdir()
    result_path = run / "result.json"
    result = {
        "schema": SCHEMA, "status": "running", "synthetic_copy_verified": False,
        "production_approved": False, "research_eligible": False,
        "source": str(source), "working_copy": str(working / source.name),
        "source_unchanged": None, "source_sqlite_reopened": False,
        "source_after": None, "error": None,
        "protection": "exclusive main/WAL/SHM; existing ancestor names pinned",
        "namespace_limit": "new child creation is detected, not universally excluded",
        "limits": {"file_bytes": lab.MAX_DB_BYTES, "total_bytes": lab.MAX_TOTAL_BYTES,
                   "tracked_stream_io_bytes": MAX_IO_BYTES, "cooperative_seconds": MAX_SECONDS},
        "io": {"read_bytes": 0, "written_bytes": 0},
        "io_tracking_scope": "source streaming reads and destination writes; excludes SQLite/evidence rehash I/O",
    }
    lab._write_json(result_path, result)
    deadline = time.monotonic() + MAX_SECONDS

    def notify(stage):
        if hook is not None:
            hook(stage, source, run)

    def charge(read=0, written=0):
        result["io"]["read_bytes"] += read
        result["io"]["written_bytes"] += written
        if sum(result["io"].values()) > MAX_IO_BYTES:
            raise ValueError("lab tracked stream I/O budget exceeded")
        if time.monotonic() > deadline:
            raise TimeoutError("lab cooperative time budget exceeded")

    def blocks(stream):
        stream.seek(0)
        total = 0
        while block := stream.read(min(CHUNK, lab.MAX_DB_BYTES - total + 1)):
            total += len(block)
            charge(read=len(block))
            if total > lab.MAX_DB_BYTES:
                raise ValueError("lab stream byte budget exceeded")
            yield block

    def held_record(path, stream):
        before = os.fstat(stream.fileno())
        if (before.st_nlink != 1 or not stat.S_ISREG(before.st_mode)
                or _stat_key(before) != _stat_key(path.stat())):
            raise ValueError("source identity/alias mismatch")
        digest = hashlib.sha256()
        for block in blocks(stream):
            digest.update(block)
        if _stat_key(before) != _stat_key(os.fstat(stream.fileno())):
            raise ValueError("held source changed")
        return {"name": path.name, "size": before.st_size, "mtime_ns": before.st_mtime_ns,
                "identity": {"st_dev": before.st_dev, "st_ino": before.st_ino},
                "sha256": digest.hexdigest()}

    try:
        if fixture.qualification.get("stream_integrity_verified") is not True:
            raise ValueError("fixture is incomplete or integrity was not verified at production")
        actual = _inventory(root, source)
        with ExitStack() as stack:
            parents = {p for d in (source.parent, evidence, working)
                       for p in (d, *d.parents)}
            checks = [stack.enter_context(_pinned_directory(p))
                      for p in sorted(parents, key=lambda p: (len(p.parts), str(p)))]
            _inventory(root, source)
            streams = {name: stack.enter_context(_exclusive_source(actual[name]))
                       for name in sorted(actual)}

            def verify_source():
                current = _inventory(root, source)
                records = {name: held_record(current[name], stream)
                           for name, stream in streams.items()}
                if records != fixture.files:
                    raise ValueError("source bytes/stat differ from fixture provenance")
                for check in checks:
                    check()
                return records

            try:
                result["source_before"] = verify_source()
                notify("sealed")
                verify_source()
                for name, stream in streams.items():
                    with (evidence / name).open("xb") as raw, (working / name).open("xb") as work:
                        digest = hashlib.sha256()
                        for block in blocks(stream):
                            charge(written=2 * len(block))
                            if raw.write(block) != len(block) or work.write(block) != len(block):
                                raise OSError("short destination write")
                            digest.update(block)
                            notify("copy_chunk")
                        for out in (raw, work):
                            out.flush()
                            os.fsync(out.fileno())
                    if digest.hexdigest() != fixture.files[name]["sha256"]:
                        raise ValueError("copy digest mismatch")
                notify("copied")
                verify_source()
                for directory in (evidence, working):
                    for name, original in fixture.files.items():
                        copy = directory / name
                        if copy.stat().st_size > lab.MAX_DB_BYTES:
                            raise ValueError("destination exceeds lab byte budget")
                        snapshot = lab.file_snapshot(copy)
                        if (snapshot["size"], snapshot["sha256"]) != (original["size"], original["sha256"]):
                            raise ValueError("destination size/hash mismatch")
                        if snapshot["identity"] in [v["identity"] for v in fixture.files.values()]:
                            raise ValueError("destination aliases source")
                        if copy.stat().st_nlink != 1:
                            raise ValueError("destination hardlink rejected")
                # 원시 증거 복제본은 cleanup 중에도 write/delete에서 보호한다.
                for name in fixture.files:
                    stack.enter_context(sealed_source(evidence / name))
                raw_before = lab.db_snapshot(evidence / source.name)
                work = working / source.name
                before_logical = lab.logical_snapshot(work)
                if before_logical != fixture.logical:
                    raise ValueError("pre-cleanup manifest/seq/payload mismatch")
                notify("before_cleanup")
                result["cleanup"] = lab.sqlite_managed_candidate(work, read_page=True)
                notify("after_cleanup")
                if any(os.path.lexists(str(work) + suffix) for suffix in lab.SIDECARS):
                    raise ValueError("sidecar remains on working copy; no immutable bypass")
                if lab.file_snapshot(work)["sha256"] != fixture.files[source.name]["sha256"]:
                    raise ValueError("cleanup changed main bytes; no dataset substitution")
                if lab.logical_snapshot(work, immutable=True) != before_logical:
                    raise ValueError("post-cleanup manifest/seq/payload mismatch")
                notify("before_qualification")
                qualified = lab.qualify_fixture(root, work, "clone_" + run_id)
                keys = ("scan_complete", "stream_integrity_verified", "research_eligible",
                        "counts", "quality_diagnostics")
                if any(qualified[k] != fixture.qualification[k] for k in keys):
                    raise ValueError("qualification/count/quality preservation mismatch")
                if qualified["stream_integrity_verified"] is not True:
                    raise ValueError("whole iterator integrity was not verified")
                if lab.db_snapshot(evidence / source.name) != raw_before:
                    raise ValueError("raw sidecar evidence changed")
                if lab.file_snapshot(work)["sha256"] != fixture.files[source.name]["sha256"]:
                    raise ValueError("qualification changed working main bytes")
                result["qualification"] = qualified
                result["raw_evidence"] = raw_before
            finally:
                result["source_after"] = verify_source()
                result["source_unchanged"] = True
            lab.enforce_budget(root)
            result["status"] = "synthetic_verified"
            result["synthetic_copy_verified"] = True
            result["research_eligible"] = qualified["research_eligible"]
    except BaseException as exc:
        result["status"] = "failed"
        result["synthetic_copy_verified"] = False
        result["research_eligible"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            lab._replace_json(result_path, result)
            raise
    lab._replace_json(result_path, result)
    return result_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)  # 외부 DB/출력 경로 인수를 받지 않는다.
    reports = []
    for unsigned in (False, True):
        root = lab.create_lab_root()
        fixture = make_fixture(root, unsigned=unsigned)
        report = clone_fixture(root, fixture)
        reports.append(str(report))
    print(json.dumps({"schema": SCHEMA, "python": platform.python_version(),
                      "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
                      "reports": reports, "production_approved": False}, ensure_ascii=False))
    return 0 if all(json.loads(Path(p).read_text(encoding="utf-8"))["synthetic_copy_verified"]
                    for p in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
