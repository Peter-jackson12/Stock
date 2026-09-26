"""MWFD-04 Phase 0 admission과 1,286-cell event cache의 재개 가능한 materialization.

하위 명령(각각 시간 예산 안에서 끝나고 다시 호출하면 이어서 진행한다):
  phase0   admission contract/identity 검증 후 create-only run root와 manifest 생성
  build    raw prefix 1회 순회(여러 호출에 나눠도 각 레코드는 한 번만 소비)
  finish   index 생성과 cache manifest 계산, MWFD-03 45셀 digest 대조
  publish  local build DB를 run root로 재개 가능한 복사 후 digest 검증
  verify   게시된 cache 전체 payload/digest 재검증
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import re
try:
    import resource
except ImportError:  # Windows
    resource = None
import shutil
import sqlite3
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from collector.raw_archive import reject_sqlite_sidecars
from collector.raw_v2_prefix_qualification import _cutoff_times, _decode_record, _read_manifest
from research.fast_backtest.execution_depth import load_execution_depth_cache
from research.fast_backtest.full_run import (
    EXPECTED_CANDIDATES,
    EXPECTED_CELLS,
    RUN_SCHEMA,
    ResumableEventCacheBuilder,
    ResumableSha256,
    load_full_population,
    read_json,
    write_json_atomic,
)
from research.fast_backtest.runtime_probe import (
    EVENT_CACHE_SCHEMA,
    json_digest,
    load_candidate_family,
    sha256_file,
    write_json_create,
)

KST = timezone(timedelta(hours=9))
CODE_FILES = (
    "research/fast_backtest/full_run.py",
    "research/fast_backtest/runtime_probe.py",
    "research/fast_backtest/sweep.py",
    "research/fast_backtest/features.py",
    "research/fast_backtest/feasibility.py",
    "research/fast_backtest/execution_depth.py",
    "research/fast_backtest/input_cache.py",
    "research/fast_backtest/plan.py",
    "engine/tick_ordering.py",
    "execution/quote_validation.py",
    "collector/raw_v2.py",
    "collector/raw_v2_prefix_qualification.py",
    "scripts/materialize_mwfd_04_events.py",
    "scripts/run_mwfd_04_full.py",
    "scripts/run_mwfd_03_probe.py",
)


def code_provenance() -> dict[str, str]:
    return {name: sha256_file(PROJECT_ROOT / name) for name in CODE_FILES}


def verify_code_provenance(manifest) -> None:
    if code_provenance() != manifest["code_provenance"]:
        raise ValueError("CODE_IDENTITY_DRIFT: code files changed after phase 0")


def peak_rss_bytes() -> int | None:
    if resource is None:
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


WINDOWS_TOTAL_STOCK_ROOT = "C:\\Projects\\TotalStock\\"
# Cowork Linux VM은 C:\Projects\TotalStock을 /sessions/<session-id>/mnt/TotalStock에 mount한다.
# session-id는 세션마다 바뀌므로 경로 해석에 쓰지 않는다.
LINUX_VM_MOUNT = re.compile(r"^/sessions/[^/]+/mnt/TotalStock/(?P<rel>.+)$")


def _under_root(total_stock_root: Path, parts: list[str]) -> Path:
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"unsafe TotalStock-relative path: {'/'.join(parts)!r}")
    return total_stock_root.joinpath(*parts)


def win_to_local(path: str, total_stock_root: Path, *, host_os: str | None = None) -> Path:
    """manifest 경로를 현재 호스트 경로로 해석한다.

    Windows 경로(C:\\Projects\\TotalStock\\...)는 Windows에서는 그대로, 다른 호스트에서는
    total_stock_root 기준으로 바꾼다. 이전 run이 기록한 Linux VM mount 경로
    (/sessions/<id>/mnt/TotalStock/...)는 어느 호스트에서든 total_stock_root 기준으로 바꾼다.
    """
    host_os = os.name if host_os is None else host_os
    if path.startswith(WINDOWS_TOTAL_STOCK_ROOT):
        if host_os == "nt":
            return Path(path)
        return _under_root(total_stock_root, path[len(WINDOWS_TOTAL_STOCK_ROOT):].split("\\"))
    mounted = LINUX_VM_MOUNT.match(path)
    if mounted:
        return _under_root(total_stock_root, mounted["rel"].split("/"))
    return Path(path)


def local_to_win(path: Path, total_stock_root: Path, *, host_os: str | None = None) -> str:
    """현재 호스트 경로를 manifest용 Windows 경로로 기록한다(호스트와 무관한 표기)."""
    host_os = os.name if host_os is None else host_os
    if host_os == "nt":
        return str(path)
    rel = Path(path).resolve().relative_to(total_stock_root.resolve())
    return WINDOWS_TOTAL_STOCK_ROOT + "\\".join(rel.parts)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="MWFD-04 phase0 + resumable event materialization")
    value.add_argument("command", choices=["phase0", "build", "finish", "publish", "verify"])
    value.add_argument("--run-root", required=True)
    value.add_argument("--total-stock-root", required=True, help="C:\\Projects\\TotalStock에 해당하는 경로")
    value.add_argument("--build-dir", help="local build directory (build/finish/publish)")
    value.add_argument("--admission", help="MWFD-03 full_run_admission.json (phase0)")
    value.add_argument("--code-revision", help="phase0에서 기록할 git HEAD")
    value.add_argument("--budget-seconds", type=float, default=140.0)
    value.add_argument("--minimum-free-bytes", type=int, default=50_000_000_000)
    return value


def phase0(args, run_root: Path, root: Path) -> int:
    admission_path = Path(args.admission).resolve(strict=True)
    admission = read_json(admission_path)
    probe_root = admission_path.parent
    probe_manifest = read_json(probe_root / "run_manifest.json")
    checks = {}

    def check(name, ok, detail=None):
        checks[name] = {"status": "PASS" if ok else "FAIL", "detail": detail}
        return ok

    check("admission_status", admission.get("status") == "FULL_RUN_ADMITTED_WITH_CONDITIONS", admission.get("status"))
    check("admission_pnl_not_used", admission.get("pnl_used_for_admission") is False)
    basis = admission["basis"]
    check("admission_basis_pass", basis["correctness"] == "PASS" and basis["resumability"] == "PASS"
          and basis["warm_cache_identity"] == "PASS" and basis["failed_cells"] == 0)
    check("admission_resume_validation", admission["resume_validation"]["status"] == "PASS")
    probe_artifacts = read_json(probe_root / "artifact_manifest.json")
    listed = {entry["path"]: entry["sha256"] for entry in probe_artifacts["entries"]}
    check("admission_file_digest", listed.get("full_run_admission.json") == sha256_file(admission_path))

    source = probe_manifest["source"]
    raw = win_to_local(source["raw_path"], root)
    stat = raw.stat()
    check("source_stat", stat.st_size == source["raw_size"] and stat.st_mtime_ns == source["raw_mtime_ns"],
          {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    try:
        reject_sqlite_sidecars(raw)
        check("source_no_sidecars", True)
    except ValueError as exc:
        check("source_no_sidecars", False, str(exc))
    prefix_report_path = win_to_local(source["prefix_report"], root)
    prefix = read_json(prefix_report_path)
    check("prefix_report", prefix.get("status") == "completed" and prefix.get("prefix_structure_verified")
          and prefix["prefix_event_sha256"] == source["prefix_digest"]
          and prefix["counts"]["raw_records"] == source["prefix_records"]
          and prefix["scope"].get("tail_scanned") is False)
    snapshot_path = raw.parents[1] / "result.json"
    snapshot = read_json(snapshot_path)
    check("snapshot_ready", snapshot.get("status") == "snapshot_ready"
          and snapshot.get("snapshot_ready_for_prefix_qualification") is True
          and snapshot.get("working_cleanup", {}).get("main_readback_verified") is True)

    family_source = win_to_local(probe_manifest["candidate_family"]["source_path"], root)
    family = load_candidate_family(family_source)
    expected_family = probe_manifest["candidate_family"]
    check("candidate_family", len(family.candidates) == EXPECTED_CANDIDATES
          and family.canonical_family_digest == expected_family["canonical_family_digest"]
          and family.ordered_identity_digest == expected_family["ordered_identity_digest"]
          and family.source_sha256 == expected_family["source_sha256"]
          and family.source_to_fast_identity_digest == expected_family["source_to_fast_identity_digest"])

    mwfd02 = win_to_local(probe_manifest["shared_cache"]["execution_depth_path"], root).parents[1]
    mwfd02_summary = read_json(mwfd02 / "summary.json")
    population = load_full_population(
        mwfd02 / "market_inventory.json", expected_sha256=probe_manifest["sample"]["inventory_sha256"]
    )
    check("population", len(population.cells) == EXPECTED_CELLS
          and population.cells and all(cell.venue == "unknown" for cell in population.cells))
    probe_ids = {cell["cell_id"] for cell in probe_manifest["sample"]["cells"]}
    check("probe_cells_subset", probe_ids <= {cell.cell_id for cell in population.cells})

    depth_path = win_to_local(probe_manifest["shared_cache"]["execution_depth_path"], root)
    depth = load_execution_depth_cache(depth_path, expected_source_prefix_digest=source["prefix_digest"],
                                       verify_payload=False)
    mwfd02_artifacts = read_json(mwfd02 / "artifact_manifest.json")
    depth_file_sha = sha256_file(depth_path / "depth.sqlite3")
    check("execution_depth_cache", depth.cache_id == probe_manifest["shared_cache"]["execution_depth_cache_id"]
          and depth.logical_digest == probe_manifest["shared_cache"]["execution_depth_logical_digest"]
          and depth.cache_id == mwfd02_summary["execution_depth_cache"]["cache_id"]
          and depth_file_sha in json.dumps(mwfd02_artifacts), depth_file_sha)
    check("execution_depth_no_sidecars", not any(
        Path(str(depth_path / "depth.sqlite3") + suffix).exists() for suffix in ("-wal", "-shm", "-journal")))
    check("venue_unknown_daily_not_ready", True, "venue literal unknown; daily KRX metadata not joined")
    check("single_worker", True, "one process, cells executed sequentially")

    run_root.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(run_root.parent).free
    projected = int(admission["basis"]["projected_single_worker"]["projected_output_bytes"])
    check("free_disk", free >= max(args.minimum_free_bytes, 3 * projected), {"free_bytes": free, "projected_output_bytes": projected})
    failed = sorted(name for name, value in checks.items() if value["status"] != "PASS")
    if failed:
        print(json.dumps({"status": "BLOCKED", "failed": failed, "checks": checks}, indent=2, default=str))
        return 2

    run_root.mkdir(parents=False, exist_ok=False)
    started = datetime.now(KST).isoformat()
    admission_record = {
        "schema": "mwfd_04_phase0_admission_v1",
        "status": "ADMITTED",
        "checked_at": started,
        "admission_contract": {
            "path": local_to_win(admission_path, root),
            "sha256": sha256_file(admission_path),
            "status": admission["status"],
            "conditions": admission["conditions"],
        },
        "checks": checks,
        "interpretation": {
            "event_cache": "MWFD-03 event cache covers only the 45 probe cells; a new 1,286-cell cache is "
                           "materialized from the same frozen source prefix with the same schema. The admission "
                           "projection already included this one-pass materialization.",
            "host": "Cowork Linux VM on the same PC; C:\\Projects\\TotalStock mounted via FUSE. Windows share-lock "
                    "(sealed_source) is unavailable; source protection = read-only immutable SQLite URI + "
                    "stat/sidecar checks before and after every chunk.",
        },
        "selection_used_pnl": False,
    }
    write_json_create(run_root / "phase0_admission.json", admission_record)
    manifest = {
        "schema": RUN_SCHEMA,
        "status": "PHASE0_ADMITTED",
        "run_id": run_root.name,
        "started_at": started,
        "code_revision": args.code_revision,
        "code_provenance": code_provenance(),
        "host": {
            "platform": platform.platform(),
            "python": sys.version,
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "note": "Linux VM (Cowork device shell) on the user's PC; each shell call is limited to ~180 s, "
                    "so work is executed in time-budgeted resumable invocations.",
        },
        "trade_date": "2026-09-21",
        "venue_interpretation": "literal unknown; not KRX/NXT-specific; venue-unverified bounded-prefix research",
        "daily_metadata": "NOT_READY; market_cap/trading_value/listed_shares not joined",
        "evaluator": "Fast screening/research evaluator; production exact remains authoritative",
        "source": source | {"snapshot_result": local_to_win(snapshot_path, root)},
        "population": population.manifest(),
        # family.source_path는 호스트에서 resolve한 경로이므로 다른 경로 필드처럼 Windows 표기로 기록한다.
        "candidate_family": family.manifest() | {"source_path": local_to_win(family_source, root)},
        "shared_cache": probe_manifest["shared_cache"] | {"execution_depth_file_sha256": depth_file_sha},
        "mwfd02_root": local_to_win(mwfd02, root),
        "mwfd03_probe_root": local_to_win(probe_root, root),
        "mwfd03_event_cache_id": read_json(probe_root / "event_materialization.json")["cache"]["cache_id"],
        "account": probe_manifest["account"],
        "feasibility_gate": {
            "version": "v0 (MWFD-02/03)",
            "ask10_notional_threshold_krw": 100_000_000,
            "max_quote_age_ns": probe_manifest["account"]["max_quote_age_ns"],
            "applies_to": "new entry evaluation only",
        },
        "expected": {
            "cells": EXPECTED_CELLS,
            "candidates": EXPECTED_CANDIDATES,
            "candidate_cell_work_units": EXPECTED_CELLS * EXPECTED_CANDIDATES,
        },
        "projection_reference": admission["basis"]["projected_single_worker"],
        "prohibited_actions": {
            "production_exact": False, "parameter_or_threshold_tuning": False,
            "holdout_exploration": False, "ocx_login_live_order": False, "parallel_workers": False,
        },
    }
    write_json_create(run_root / "run_manifest.json", manifest)
    print(json.dumps({"status": "PHASE0_ADMITTED", "run_root": str(run_root), "free_bytes": free}))
    return 0


def open_context(run_root: Path, root: Path):
    manifest = read_json(run_root / "run_manifest.json")
    verify_code_provenance(manifest)
    mwfd02 = win_to_local(manifest["mwfd02_root"], root)
    population = load_full_population(mwfd02 / "market_inventory.json",
                                      expected_sha256=manifest["population"]["inventory_sha256"])
    if population.ordered_cell_digest != manifest["population"]["ordered_cell_digest"]:
        raise ValueError("POPULATION_DRIFT")
    return manifest, population


def build(args, run_root: Path, root: Path) -> int:
    started = time.perf_counter()
    cpu = time.process_time()
    deadline = started + args.budget_seconds
    manifest, population = open_context(run_root, root)
    source = manifest["source"]
    raw = win_to_local(source["raw_path"], root)
    builder = ResumableEventCacheBuilder(
        Path(args.build_dir), population=population, source_prefix_digest=source["prefix_digest"],
        expected_prefix_records=source["prefix_records"],
    )
    state = builder.load_state()
    if state["status"] != "BUILDING":
        print(json.dumps({"status": state["status"]}))
        return 0
    before = raw.stat()
    if (before.st_size, before.st_mtime_ns) != (source["raw_size"], source["raw_mtime_ns"]):
        raise ValueError("SOURCE_IDENTITY_DRIFT")
    reject_sqlite_sidecars(raw)
    prefix = read_json(win_to_local(source["prefix_report"], root))
    conn = sqlite3.connect(raw.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)
    try:
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-8192")
        raw_manifest = _read_manifest(conn)
        _, cutoff_utc = _cutoff_times(raw_manifest, prefix["scope"]["end_market_second_exclusive"])
        state = builder.run_chunk(conn, manifest=raw_manifest, cutoff_utc=cutoff_utc,
                                  decode_record=_decode_record, deadline=deadline, clock=time.perf_counter)
    finally:
        conn.close()
    reject_sqlite_sidecars(raw)
    after = raw.stat()
    if (after.st_size, after.st_mtime_ns, after.st_ino) != (before.st_size, before.st_mtime_ns, before.st_ino):
        raise ValueError("SOURCE_IDENTITY_CHANGED_DURING_CHUNK")
    log = {
        "at": datetime.now(KST).isoformat(), "status": state["status"],
        "next_seq": state["next_seq"], "prefix_records": state["prefix_records"],
        "selected_records": state["selected_records"],
        "wall_seconds": time.perf_counter() - started, "cpu_seconds": time.process_time() - cpu,
        "peak_rss_bytes": peak_rss_bytes(), "source_stat_unchanged": True, "sidecars_absent": True,
    }
    with (Path(args.build_dir) / "build_log.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(log) + "\n")
    print(json.dumps(log))
    return 0


def finish(args, run_root: Path, root: Path) -> int:
    started = time.perf_counter()
    manifest, population = open_context(run_root, root)
    build_dir = Path(args.build_dir)
    builder = ResumableEventCacheBuilder(
        build_dir, population=population, source_prefix_digest=manifest["source"]["prefix_digest"],
        expected_prefix_records=manifest["source"]["prefix_records"],
    )
    state = builder.load_state()
    target = build_dir / "cache_manifest.json"
    if target.exists():
        print(json.dumps({"status": "ALREADY_FINISHED"}))
        return 0
    cache_manifest = builder.finish(state)
    probe_root = win_to_local(manifest["mwfd03_probe_root"], root)
    probe_cache = read_json(probe_root / "event_cache" / "manifest.json")
    cross = {}
    for cell_id, value in probe_cache["cells"].items():
        mine = cache_manifest["cells"][cell_id]
        cross[cell_id] = (mine["event_digest"] == value["event_digest"]
                          and mine["event_count"] == value["event_count"]
                          and mine["event_bytes"] == value["event_bytes"])
    if not all(cross.values()) or len(cross) != 45:
        raise ValueError("MWFD-03 45-cell event digest cross-check failed")
    if probe_cache["source"] != cache_manifest["source"] or probe_cache["session_id"] != cache_manifest["session_id"]:
        raise ValueError("MWFD-03 source/session identity mismatch")
    write_json_create(target, cache_manifest)
    result = {
        "status": "FINISHED", "cache_id": cache_manifest["cache_id"],
        "event_count": cache_manifest["event_count"], "mwfd03_cross_check_cells": len(cross),
        "wall_seconds": time.perf_counter() - started,
        "db_sha256": sha256_file(builder.db_path), "db_bytes": builder.db_path.stat().st_size,
    }
    write_json_create(build_dir / "finish.json", result)
    print(json.dumps(result))
    return 0


def publish(args, run_root: Path, root: Path) -> int:
    started = time.perf_counter()
    deadline = started + args.budget_seconds
    manifest, _ = open_context(run_root, root)
    build_dir = Path(args.build_dir)
    finish_record = read_json(build_dir / "finish.json")
    cache_manifest = read_json(build_dir / "cache_manifest.json")
    source_db = build_dir / "events.sqlite3"
    final_dir = run_root / "event_cache"
    if final_dir.exists():
        print(json.dumps({"status": "ALREADY_PUBLISHED"}))
        return 0
    staging = run_root / ".event_cache.publishing"
    staging.mkdir(exist_ok=True)
    target = staging / "events.sqlite3"
    state_path = staging / ".copy_state.json"
    if state_path.exists():
        state = read_json(state_path)
    else:
        if target.exists():
            raise FileExistsError("publish target exists without copy state")
        target.touch(exist_ok=False)
        state = {"offset": 0, "sha_state": ResumableSha256().state().hex()}
        write_json_atomic(state_path, state)
    total = source_db.stat().st_size
    digest = ResumableSha256(bytes.fromhex(state["sha_state"]))
    with source_db.open("rb") as src, target.open("r+b") as dst:
        dst.truncate(state["offset"])
        src.seek(state["offset"])
        dst.seek(state["offset"])
        while state["offset"] < total and time.perf_counter() < deadline:
            block = src.read(min(64 << 20, total - state["offset"]))
            dst.write(block)
            digest.update(block)
            dst.flush()
            os.fsync(dst.fileno())
            state["offset"] += len(block)
            state["sha_state"] = digest.state().hex()
            write_json_atomic(state_path, state)
    if state["offset"] < total:
        print(json.dumps({"status": "COPYING", "offset": state["offset"], "total": total,
                          "wall_seconds": time.perf_counter() - started}))
        return 0
    copied = digest.hexdigest()
    readback = sha256_file(target)
    if not (copied == readback == finish_record["db_sha256"]) or target.stat().st_size != finish_record["db_bytes"]:
        raise ValueError("published event cache digest mismatch")
    state_path.unlink()
    write_json_create(staging / "manifest.json", cache_manifest)
    os.rename(staging, final_dir)
    result = {"status": "PUBLISHED", "db_sha256": readback, "bytes": total,
              "wall_seconds": time.perf_counter() - started}
    print(json.dumps(result))
    return 0


def verify(args, run_root: Path, root: Path) -> int:
    """게시된 cache를 seq 순서로 다시 읽어 전체/셀 digest와 payload identity를 재검증한다."""
    started = time.perf_counter()
    deadline = started + args.budget_seconds
    manifest, population = open_context(run_root, root)
    cache_dir = run_root / "event_cache"
    cache = read_json(cache_dir / "manifest.json")
    identity = {key: cache[key] for key in ("schema", "source_prefix_digest", "sample_digest", "source",
                                            "session_id", "event_count", "event_digest", "cells")}
    if cache["cache_id"] != json_digest(identity) or cache["schema"] != EVENT_CACHE_SCHEMA:
        raise ValueError("event cache identity mismatch")
    if cache["sample_digest"] != population.ordered_cell_digest:
        raise ValueError("event cache population mismatch")
    state_path = run_root / ".event_verify_state.json"
    if (run_root / "event_materialization.json").exists():
        print(json.dumps({"status": "ALREADY_VERIFIED"}))
        return 0
    if state_path.exists():
        state = read_json(state_path)
    else:
        state = {"next_seq": 0, "rows": 0, "overall": ResumableSha256().state().hex(), "cells": {}, "counts": {}}
    overall = ResumableSha256(bytes.fromhex(state["overall"]))
    cells = {key: ResumableSha256(bytes.fromhex(value)) for key, value in state["cells"].items()}
    counts = dict(state["counts"])
    conn = sqlite3.connect((cache_dir / "events.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True)
    last_seq = state["next_seq"] - 1
    done = False
    try:
        cursor = conn.execute("SELECT seq,cell_id,payload FROM events WHERE seq >= ? ORDER BY seq", (state["next_seq"],))
        rows = 0
        for seq, cell_id, payload in cursor:
            value = json.loads(payload)
            if value["seq"] != seq or f"{value['code']}={value['venue']}" != cell_id or seq <= last_seq:
                raise ValueError("event payload identity mismatch")
            last_seq = seq
            encoded = (payload + "\n").encode("utf-8")
            overall.update(encoded)
            digest = cells.get(cell_id)
            if digest is None:
                digest = cells[cell_id] = ResumableSha256()
            digest.update(encoded)
            counts[cell_id] = counts.get(cell_id, 0) + 1
            state["rows"] += 1
            rows += 1
            if rows % 50000 == 0 and time.perf_counter() >= deadline:
                break
        else:
            done = True
        cursor.close()
        total = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    finally:
        conn.close()
    state.update({"next_seq": last_seq + 1, "overall": overall.state().hex(),
                  "cells": {key: value.state().hex() for key, value in cells.items()}, "counts": counts})
    if not done:
        write_json_atomic(state_path, state)
        print(json.dumps({"status": "VERIFYING", "rows": state["rows"], "wall_seconds": time.perf_counter() - started}))
        return 0
    if state["rows"] != cache["event_count"] or total != cache["event_count"]:
        raise ValueError("event cache row count mismatch")
    if overall.hexdigest() != cache["event_digest"]:
        raise ValueError("event cache overall digest mismatch")
    for cell_id, expected in cache["cells"].items():
        if counts.get(cell_id) != expected["event_count"] or cells[cell_id].hexdigest() != expected["event_digest"]:
            raise ValueError(f"event cache cell digest mismatch: {cell_id}")
    build_dir = Path(args.build_dir) if args.build_dir else None
    build_state = read_json(build_dir / "build_state.json") if build_dir else {}
    build_log = []
    if build_dir and (build_dir / "build_log.jsonl").exists():
        build_log = [json.loads(line) for line in (build_dir / "build_log.jsonl").read_text().splitlines() if line]
    finish_record = read_json(build_dir / "finish.json") if build_dir else {}
    probe_root = win_to_local(manifest["mwfd03_probe_root"], root)
    probe_cache = read_json(probe_root / "event_cache" / "manifest.json")
    result = {
        "schema": "mwfd_04_event_materialization_v1",
        "status": "COMPLETED",
        "cache": {
            "path": local_to_win(cache_dir, root),
            "cache_id": cache["cache_id"],
            "event_count": cache["event_count"],
            "event_digest": cache["event_digest"],
            "cell_count": len(cache["cells"]),
            "db_sha256": finish_record.get("db_sha256"),
            "bytes": sum(item.stat().st_size for item in cache_dir.iterdir()),
            "roundtrip_verified": True,
            "verification": "seq-ordered payload re-read: overall + per-cell sha256/count + payload seq/cell identity; "
                            "per-cell ReceiveOrderReplay is re-run at every cell load",
        },
        "mwfd03_cross_check": {
            "cells": len(probe_cache["cells"]),
            "event_digest_equal": all(cache["cells"][key]["event_digest"] == value["event_digest"]
                                      for key, value in probe_cache["cells"].items()),
            "mwfd03_cache_id": probe_cache["cache_id"],
        },
        "source": {
            "prefix_records": build_state.get("prefix_records"),
            "prefix_digest": build_state.get("observed_prefix_digest"),
            "boundary_sentinel": build_state.get("sentinel"),
            "raw_records_consumed_once": True,
            "chunks": build_state.get("chunks"),
        },
        "timing": {
            "source_pass_wall_seconds": sum(item["wall_seconds"] for item in build_log),
            "source_pass_cpu_seconds": sum(item["cpu_seconds"] for item in build_log),
            "source_pass_invocations": len(build_log),
            "finish_wall_seconds": finish_record.get("wall_seconds"),
        },
        "memory": {"peak_rss_bytes_max": max((item["peak_rss_bytes"] for item in build_log), default=None)},
        "source_preservation": {
            "path": manifest["source"]["raw_path"],
            "open_mode": "sqlite mode=ro&immutable=1",
            "source_stat_unchanged_every_chunk": all(item["source_stat_unchanged"] for item in build_log),
            "sidecars_absent_every_chunk": all(item["sidecars_absent"] for item in build_log),
            "windows_share_lock": "not available on Linux host",
        },
    }
    write_json_create(run_root / "event_materialization.json", result)
    state_path.unlink()
    print(json.dumps({"status": "VERIFIED", "cache_id": cache["cache_id"], "wall_seconds": time.perf_counter() - started}))
    return 0


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    run_root = Path(args.run_root).absolute()
    root = Path(args.total_stock_root).resolve(strict=True)
    if args.command == "phase0":
        return phase0(args, run_root, root)
    return {"build": build, "finish": finish, "publish": publish, "verify": verify}[args.command](args, run_root, root)


if __name__ == "__main__":
    raise SystemExit(main())
