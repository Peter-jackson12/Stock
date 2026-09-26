"""MWFD-04 Phase 3/4: reconciliation, canonical 결합 산출물, 기술통계, factor dataset manifest.

하위 명령(시간 예산 안에서 끝나며 재호출 시 이어서 진행):
  combine      체크포인트 전수 검증 + canonical 결합 파일 생성(재개형)
  crosscheck   MWFD-03 45셀 경제 digest 대조 + 다중 청크 셀의 비분할 재계산 대조
  resumecheck  실행기를 다시 돌려 완료 셀을 모두 건너뛰고 아무것도 다시 쓰지 않는지 확인
  summarize    기술통계/게이트/런타임/factor manifest 생성
  parquet      candidate_cell_results의 평탄화 Parquet 파생본(선택)
  manifest     artifact_manifest.json
최종 순위화·후보 확정은 하지 않는다(기술통계만).
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research.fast_backtest.full_run import (
    EXPECTED_CANDIDATES,
    EXPECTED_CELLS,
    ResumableConcatWriter,
    checkpoint_name,
    read_json,
    write_json_atomic,
)
from research.fast_backtest.plan import canonical_json
from research.fast_backtest.runtime_probe import assert_finite, json_digest, sha256_file, write_json_create
from scripts.materialize_mwfd_04_events import win_to_local

KST = timezone(timedelta(hours=9))
OUTPUTS = ("candidate_cell_results.jsonl", "cell_results.jsonl", "cell_factors.jsonl", "trades.jsonl")
FORBIDDEN_DAILY_KEYS = ("market_cap", "trading_value", "listed_shares", "marcap")
SUMMARY_OUTPUTS = (
    "candidate_summary.jsonl",
    "full_run_summary.json",
    "gate_summary.json",
    "runtime_summary.json",
    "factor_dataset_manifest.json",
)


def _crosscheck_passes(result: dict) -> bool:
    return (
        result.get("mwfd03_45_cell_economic_digest", {}).get("status") == "PASS"
        and result.get("unchunked_recompute", {}).get("status") == "PASS"
    )


def _resumecheck_passes(result: dict) -> bool:
    return result.get("status") == "PASS"


def _file_stats(path: Path) -> dict:
    digest = hashlib.sha256()
    rows = 0
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
            size += len(block)
            rows += block.count(b"\n")
    return {"path": path.name, "rows": rows, "bytes": size, "sha256": digest.hexdigest()}


def _write_jsonl_atomic(path: Path, rows) -> None:
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(canonical_json(row) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _summary_marker_valid(run_root: Path, marker: dict) -> bool:
    if marker.get("schema") != "mwfd_04_summarize_completion_v1" or marker.get("status") != "COMPLETED":
        return False
    outputs = marker.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != set(SUMMARY_OUTPUTS):
        return False
    for name in SUMMARY_OUTPUTS:
        path = run_root / name
        expected = outputs.get(name)
        if not path.is_file() or not isinstance(expected, dict):
            return False
        if path.stat().st_size != expected.get("bytes") or sha256_file(path) != expected.get("sha256"):
            return False
    return True


def _completion_gate_errors(run_root: Path, entries_by_name: dict[str, dict]) -> list[str]:
    errors: list[str] = []
    required = ("combine.json", "crosscheck.json", "resume_validation.json", "summarize.json")
    for name in required:
        if name not in entries_by_name:
            errors.append(f"missing:{name}")
    if errors:
        return errors

    combine = read_json(run_root / "combine.json")
    if combine.get("status") != "COMPLETED":
        errors.append("combine:not_completed")
    for name in OUTPUTS:
        expected = combine.get("outputs", {}).get(name)
        actual = entries_by_name.get(name)
        if not isinstance(expected, dict) or actual is None:
            errors.append(f"combine_output_missing:{name}")
            continue
        if expected.get("sha256") != actual.get("sha256") or expected.get("bytes") != actual.get("bytes"):
            errors.append(f"combine_output_mismatch:{name}")

    crosscheck = read_json(run_root / "crosscheck.json")
    if not _crosscheck_passes(crosscheck):
        errors.append("crosscheck:FAIL")
    resume = read_json(run_root / "resume_validation.json")
    if not _resumecheck_passes(resume):
        errors.append("resumecheck:FAIL")
    summarize = read_json(run_root / "summarize.json")
    if not _summary_marker_valid(run_root, summarize):
        errors.append("summarize:INVALID")

    summary = read_json(run_root / "full_run_summary.json")
    coverage = summary.get("coverage", {})
    if coverage.get("cells_completed") != EXPECTED_CELLS:
        errors.append("coverage:cells")
    if coverage.get("candidates") != EXPECTED_CANDIDATES:
        errors.append("coverage:candidates")
    if coverage.get("candidate_cell_evaluations") != EXPECTED_CELLS * EXPECTED_CANDIDATES:
        errors.append("coverage:candidate_cells")
    if coverage.get("duplicate_count") != 0:
        errors.append("coverage:duplicates")
    if coverage.get("failure_count") != 0:
        errors.append("coverage:failures")

    gate = read_json(run_root / "gate_summary.json")
    if gate.get("reconciled") is not True:
        errors.append("gate:not_reconciled")
    if gate.get("mwfd02_inventory_match") is not True:
        errors.append("gate:inventory_mismatch")

    unresolved = list((run_root / "failures").glob("*.json")) if (run_root / "failures").exists() else []
    if unresolved:
        errors.append(f"unresolved_failures:{len(unresolved)}")
    return errors


def now() -> str:
    return datetime.now(KST).isoformat()


def quantile(ordered, q):
    if not ordered:
        return None
    index = (len(ordered) - 1) * q
    lo, hi = math.floor(index), math.ceil(index)
    return ordered[lo] if lo == hi else ordered[lo] * (hi - index) + ordered[hi] * (index - lo)


def describe(values) -> dict:
    values = sorted(v for v in values if v is not None)
    if not values:
        return {"count": 0}
    result = {
        "count": len(values), "mean": statistics.fmean(values), "median": quantile(values, 0.5),
        "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "min": values[0], "max": values[-1],
        "quantiles": {f"p{int(q*100):02d}": quantile(values, q)
                      for q in (0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99)},
        "positive": sum(v > 0 for v in values), "zero": sum(v == 0 for v in values),
        "negative": sum(v < 0 for v in values),
    }
    assert_finite(result)
    return result


def pearson(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return None if sx == 0 or sy == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


class Env:
    def __init__(self, run_root: Path, root: Path):
        from research.fast_backtest.full_run import load_full_population
        self.run_root, self.root = run_root, root
        self.manifest = read_json(run_root / "run_manifest.json")
        mwfd02 = win_to_local(self.manifest["mwfd02_root"], root)
        self.population = load_full_population(mwfd02 / "market_inventory.json",
                                               expected_sha256=self.manifest["population"]["inventory_sha256"])
        if self.population.ordered_cell_digest != self.manifest["population"]["ordered_cell_digest"]:
            raise ValueError("POPULATION_DRIFT")
        self.candidate_order = self.manifest["candidate_family"]["candidate_order"]
        self.family_digest = self.manifest["candidate_family"]["canonical_family_digest"]
        self.event_cells = read_json(run_root / "event_cache" / "manifest.json")["cells"]
        self.checkpoints = run_root / "checkpoints"


def verify_checkpoint(env: Env, cell) -> tuple[dict, bytes, bytes, dict]:
    path = env.checkpoints / checkpoint_name(cell)
    completion = read_json(path / "completion.json")
    if completion.get("status") != "COMPLETED" or completion["cell_id"] != cell.cell_id:
        raise ValueError(f"incomplete checkpoint {cell.cell_id}")
    if completion["candidate_family_digest"] != env.family_digest:
        raise ValueError("checkpoint candidate identity mismatch")
    if completion["event_digest"] != env.event_cells[cell.cell_id]["event_digest"]:
        raise ValueError("checkpoint event digest mismatch")
    if completion["code_revision"] != env.manifest["code_revision"]:
        raise ValueError("checkpoint code revision mismatch")
    data = (path / "candidate_results.jsonl").read_bytes()
    if hashlib.sha256(data).hexdigest() != completion["candidate_results_file_digest"]:
        raise ValueError("candidate-cell serialization digest mismatch")
    rows = [json.loads(line) for line in data.decode("utf-8").splitlines()]
    identities = [row["parameter_identity"] for row in rows]
    if identities != env.candidate_order or len(set(identities)) != EXPECTED_CANDIDATES:
        raise ValueError("candidate-cell reconciliation failed")
    for row in rows:
        assert_finite(row)
        if row["cell_id"] != cell.cell_id or row["venue"] != "unknown" or row["evaluated"] != 1:
            raise ValueError("candidate row identity mismatch")
        if any(key in row for key in FORBIDDEN_DAILY_KEYS):
            raise ValueError("daily NOT_READY metadata leaked")
    trades = (path / "trades.jsonl").read_bytes()
    if hashlib.sha256(trades).hexdigest() != completion["trade_records_file_digest"]:
        raise ValueError("trade records digest mismatch")
    cell_result = read_json(path / "cell_result.json")
    if cell_result["candidate_results_digest"] != completion["candidate_results_digest"]:
        raise ValueError("cell result/completion digest mismatch")
    gate = cell_result["gate"]
    if gate["PASS"] + gate["FAIL"] + gate["UNKNOWN"] != gate["evaluated"] or gate["evaluated"] != cell_result["event"]["trade_count"]:
        raise ValueError("gate reconciliation failed")
    prepare = read_json(path / "prepare.json")
    return cell_result, data, trades, prepare


def command_combine(args, env: Env) -> int:
    started = time.perf_counter()
    deadline = started + args.budget_seconds
    missing = [c for c in env.population.cells if not (env.checkpoints / checkpoint_name(c) / "completion.json").exists()]
    if missing:
        print(json.dumps({"status": "NOT_ALL_CELLS_COMPLETE", "missing": len(missing),
                          "first_missing": missing[0].cell_id}))
        return 2
    combine_path = env.run_root / "combine.json"
    if combine_path.exists():
        record = read_json(combine_path)
        if record.get("status") != "COMPLETED":
            raise ValueError("combine completion marker is not COMPLETED")
        missing_outputs = [name for name in OUTPUTS if not (env.run_root / name).is_file()]
        if missing_outputs:
            raise ValueError(f"combine marker exists but outputs are missing: {missing_outputs}")
        print(json.dumps({"status": "ALREADY_COMBINED", "validation": "PASS"}))
        return 0

    writers = {name: ResumableConcatWriter(env.run_root / name) for name in OUTPUTS}
    published = {name: writer.final.exists() for name, writer in writers.items()}
    final_ledger = env.run_root / "combine_ledger.jsonl"
    temp_ledger = env.run_root / ".combine_ledger.jsonl"
    if any(published.values()) or final_ledger.exists():
        ledger_source = final_ledger if final_ledger.exists() else temp_ledger
        if not ledger_source.exists():
            raise ValueError("published combine output exists without ledger")
        ledger_rows = [json.loads(line) for line in ledger_source.read_text().splitlines()]
        if [row["cell_id"] for row in ledger_rows] != [cell.cell_id for cell in env.population.cells]:
            raise ValueError("combine recovery ledger is incomplete or out of order")
        results = {}
        for name, writer in writers.items():
            if writer.final.exists():
                results[name] = _file_stats(writer.final)
            else:
                state = writer.open()
                if state["next_cell"] != EXPECTED_CELLS:
                    writer.close()
                    raise ValueError(f"combine recovery output {name} is not fully committed")
                results[name] = writer.finalize()
        if results["candidate_cell_results.jsonl"]["rows"] != EXPECTED_CELLS * EXPECTED_CANDIDATES:
            raise ValueError("candidate-cell expected count reconciliation failed")
        if results["cell_results.jsonl"]["rows"] != EXPECTED_CELLS:
            raise ValueError("cell count reconciliation failed")
        if not final_ledger.exists():
            os.replace(temp_ledger, final_ledger)
        record = {
            "schema": "mwfd_04_combine_v1", "status": "COMPLETED", "completed_at": now(),
            "outputs": results, "cells": len(ledger_rows),
            "candidate_cell_rows": results["candidate_cell_results.jsonl"]["rows"],
            "per_cell_economic_digest_list_digest": json_digest([row["candidate_results_digest"] for row in ledger_rows]),
            "checks": {"all_checkpoints_verified": True, "candidate_order_frozen": True, "no_duplicate_pairs": True,
                       "finite_values": True, "daily_metadata_absent": True, "gate_reconciled_per_cell": True,
                       "publication_recovered": True},
        }
        write_json_create(combine_path, record)
        print(json.dumps({"status": "COMBINED_RECOVERED", "rows": record["candidate_cell_rows"]}))
        return 0

    states = {name: writer.open() for name, writer in writers.items()}
    positions = {state["next_cell"] for state in states.values()}
    if len(positions) != 1:
        raise ValueError("combined writers disagree on position")
    position = positions.pop()
    ledger_path = env.run_root / ".combine_ledger.jsonl"
    if position == 0 and ledger_path.exists():
        ledger_path.unlink()
    ledger_lines = ledger_path.read_text().splitlines() if ledger_path.exists() else []
    ledger_lines = ledger_lines[:position]
    ledger_path.write_text("".join(line + "\n" for line in ledger_lines))
    ledger = ledger_path.open("a", encoding="utf-8")
    cells = env.population.cells
    index = position
    since_commit = 0
    while index < len(cells):
        cell = cells[index]
        cell_result, data, trades, prepare = verify_checkpoint(env, cell)
        writers["candidate_cell_results.jsonl"].write_lines(data.splitlines(keepends=True))
        writers["cell_results.jsonl"].write_lines([(canonical_json(cell_result) + "\n").encode("utf-8")])
        factor = {"cell_index": cell.index, "cell_id": cell.cell_id, "code": cell.code, "venue": cell.venue,
                  "activity_stratum": cell.tier, "event_count": cell.event_count,
                  "feature_digest": prepare["feature_digest"], "gate_digest": prepare["gate_digest"],
                  "feature_cache": f"checkpoints/{checkpoint_name(cell)}/feature_cache",
                  "gate_cache": f"checkpoints/{checkpoint_name(cell)}/gate_cache",
                  "factors": prepare["factors"],
                  "outcomes": cell_result["candidate_outcomes"]}
        assert_finite(factor)
        writers["cell_factors.jsonl"].write_lines([(canonical_json(factor) + "\n").encode("utf-8")])
        writers["trades.jsonl"].write_lines(trades.splitlines(keepends=True))
        ledger.write(json.dumps({"cell_index": cell.index, "cell_id": cell.cell_id,
                                 "candidate_results_digest": cell_result["candidate_results_digest"],
                                 "file_digest": hashlib.sha256(data).hexdigest()}, sort_keys=True) + "\n")
        index += 1
        since_commit += 1
        if since_commit >= 25 or index == len(cells) or time.perf_counter() >= deadline:
            ledger.flush()
            os.fsync(ledger.fileno())
            for writer in writers.values():
                writer.commit_cell(index)
            since_commit = 0
            if time.perf_counter() >= deadline:
                break
    ledger.close()
    if index < len(cells):
        for writer in writers.values():
            writer.close()
        print(json.dumps({"status": "COMBINING", "next_cell": index, "wall_seconds": time.perf_counter() - started}))
        return 0
    results = {name: writer.finalize() for name, writer in writers.items()}
    if results["candidate_cell_results.jsonl"]["rows"] != EXPECTED_CELLS * EXPECTED_CANDIDATES:
        raise ValueError("candidate-cell expected count reconciliation failed")
    if results["cell_results.jsonl"]["rows"] != EXPECTED_CELLS:
        raise ValueError("cell count reconciliation failed")
    ledger_rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    if [row["cell_id"] for row in ledger_rows] != [cell.cell_id for cell in cells]:
        raise ValueError("combine ledger order mismatch")
    record = {
        "schema": "mwfd_04_combine_v1", "status": "COMPLETED", "completed_at": now(),
        "outputs": results,
        "cells": len(ledger_rows),
        "candidate_cell_rows": results["candidate_cell_results.jsonl"]["rows"],
        "per_cell_economic_digest_list_digest": json_digest([row["candidate_results_digest"] for row in ledger_rows]),
        "checks": {"all_checkpoints_verified": True, "candidate_order_frozen": True, "no_duplicate_pairs": True,
                   "finite_values": True, "daily_metadata_absent": True, "gate_reconciled_per_cell": True},
    }
    os.replace(ledger_path, env.run_root / "combine_ledger.jsonl")
    write_json_create(env.run_root / "combine.json", record)
    print(json.dumps({"status": "COMBINED", "rows": record["candidate_cell_rows"],
                      "wall_seconds": time.perf_counter() - started}))
    return 0


def command_crosscheck(args, env: Env) -> int:
    """MWFD-03 45셀 digest 대조와, 여러 청크로 계산된 셀 일부의 비분할 재계산 대조."""
    started = time.perf_counter()
    target = env.run_root / "crosscheck.json"
    state_path = env.run_root / ".crosscheck_state.json"
    if target.exists():
        result = read_json(target)
        passed = _crosscheck_passes(result)
        print(json.dumps({"status": "ALREADY_DONE", "validation": "PASS" if passed else "FAIL"}))
        return 0 if passed else 3
    state = read_json(state_path) if state_path.exists() else {"mwfd03": None, "rechunk": {}}
    probe_root = win_to_local(env.manifest["mwfd03_probe_root"], env.root)
    if state["mwfd03"] is None:
        probe = read_json(probe_root / "run_manifest.json")
        by_id = {cell.cell_id: cell for cell in env.population.cells}
        compared = []
        for item in probe["sample"]["cells"]:
            ref = read_json(probe_root / "checkpoints" / f"{item['index']:02d}-{item['code']}-{item['venue']}" / "completion.json")
            mine = read_json(env.checkpoints / checkpoint_name(by_id[item["cell_id"]]) / "completion.json")
            compared.append({"cell_id": item["cell_id"], "mwfd03": ref["candidate_results_digest"],
                             "mwfd04": mine["candidate_results_digest"],
                             "equal": ref["candidate_results_digest"] == mine["candidate_results_digest"]})
        state["mwfd03"] = {"cells": len(compared), "equal": sum(c["equal"] for c in compared), "detail": compared}
        write_json_atomic(state_path, state)
    # 비분할 재계산: 청크 2~4개였던 중간 규모 셀 3개(결정적 선택: 가장 작은 event_count 순)
    multi = []
    for cell in env.population.cells:
        prep = read_json(env.checkpoints / checkpoint_name(cell) / "prepare.json")
        parts = math.ceil(EXPECTED_CANDIDATES / prep["part_size"])
        if 2 <= parts <= 4:
            multi.append((cell.event_count, cell.cell_id, cell, parts))
    chosen = sorted(multi)[:3]
    from scripts.run_mwfd_04_full import Context, economic_record
    from research.fast_backtest.features import load_feature_cache
    from research.fast_backtest.runtime_probe import load_gate_cache
    from research.fast_backtest.sweep import run_fast_sweep
    ctx = None
    for _, cell_id, cell, parts in chosen:
        if cell_id in state["rechunk"]:
            continue
        if time.perf_counter() - started > args.budget_seconds - 60:
            break
        ctx = ctx or Context(env.run_root, env.root)
        path = env.checkpoints / checkpoint_name(cell)
        prep = read_json(path / "prepare.json")
        features = load_feature_cache(path / "feature_cache", expected_input_event_digest=prep["event"]["digest"])
        gate = load_gate_cache(path / "gate_cache", expected_event_digest=prep["event"]["digest"],
                               expected_depth_cache_id=ctx.depth.cache_id)
        results = sorted(run_fast_sweep(features, ctx.family.candidates, account=ctx.account, entry_feasibility=gate),
                         key=lambda r: r.parameter_identity)
        digest = json_digest([economic_record(r) for r in results])
        completion = read_json(path / "completion.json")
        state["rechunk"][cell_id] = {"parts_in_run": parts, "unchunked_digest": digest,
                                     "run_digest": completion["candidate_results_digest"],
                                     "equal": digest == completion["candidate_results_digest"],
                                     "event_count": cell.event_count}
        write_json_atomic(state_path, state)
    if len(state["rechunk"]) < len(chosen):
        print(json.dumps({"status": "IN_PROGRESS", "done": len(state["rechunk"])}))
        return 0
    result = {
        "schema": "mwfd_04_crosscheck_v1",
        "mwfd03_45_cell_economic_digest": {k: state["mwfd03"][k] for k in ("cells", "equal")}
        | {"status": "PASS" if state["mwfd03"]["equal"] == state["mwfd03"]["cells"] == 45 else "FAIL",
           "detail": state["mwfd03"]["detail"]},
        "unchunked_recompute": {"cells": state["rechunk"],
                                "status": "PASS" if all(v["equal"] for v in state["rechunk"].values()) else "FAIL",
                                "note": "cells executed with 2-4 candidate chunks re-swept in one pass from the persisted feature/gate caches"},
    }
    write_json_create(target, result)
    state_path.unlink()
    passed = _crosscheck_passes(result)
    print(json.dumps({"status": "DONE", "mwfd03": result["mwfd03_45_cell_economic_digest"]["status"],
                      "unchunked": result["unchunked_recompute"]["status"]}))
    return 0 if passed else 3


def command_resumecheck(args, env: Env) -> int:
    from scripts.run_mwfd_04_full import CellRunner, Context
    target = env.run_root / "resume_validation.json"
    if target.exists():
        result = read_json(target)
        passed = _resumecheck_passes(result)
        print(json.dumps({"status": "ALREADY_DONE", "validation": "PASS" if passed else "FAIL"}))
        return 0 if passed else 3
    combined = env.run_root / "candidate_cell_results.jsonl"
    before = sha256_file(combined)
    completions_before = json_digest([read_json(env.checkpoints / checkpoint_name(c) / "completion.json")
                                      for c in env.population.cells])
    ctx = Context(env.run_root, env.root)
    runner = CellRunner(ctx, Path(args.scratch_dir), time.perf_counter() + 1e9)
    statuses = Counter(runner.advance(cell) for cell in ctx.population.cells)
    after = sha256_file(combined)
    completions_after = json_digest([read_json(env.checkpoints / checkpoint_name(c) / "completion.json")
                                     for c in env.population.cells])
    log_lines = (env.run_root / "run_log.jsonl").read_text().splitlines()
    interrupted = [json.loads(line) for line in log_lines]
    deferred_cells = sorted({s["cell_index"] for r in interrupted for s in r["steps"] if s["stage"] != "finalize"}
                            - {s["cell_index"] for r in interrupted for s in r["steps"] if s["stage"] == "finalize"
                               and any(t["cell_index"] == s["cell_index"] and t["stage"] != "finalize" for t in r["steps"])})
    result = {
        "schema": "mwfd_04_resume_validation_v1",
        "status": "PASS" if statuses == Counter({"COMPLETED": EXPECTED_CELLS}) and not runner.steps
        and before == after and completions_before == completions_after else "FAIL",
        "completed_cells_skipped": statuses.get("COMPLETED", 0),
        "work_steps_executed_on_recheck": len(runner.steps),
        "candidate_cell_output_rewritten": before != after,
        "combined_file_digest_before": before, "combined_file_digest_after": after,
        "completion_records_digest": completions_after,
        "operational_resume": {
            "run_invocations": len(interrupted),
            "cells_spanning_multiple_invocations": len(deferred_cells),
            "note": "every run invocation ended by time budget and the next invocation resumed from durable "
                    "checkpoints; multi-invocation cells are cross-checked by the MWFD-03 and unchunked digests",
        },
    }
    write_json_create(target, result)
    print(json.dumps({k: result[k] for k in ("status", "completed_cells_skipped", "work_steps_executed_on_recheck",
                                            "candidate_cell_output_rewritten")}))
    return 0 if result["status"] == "PASS" else 3


def command_summarize(args, env: Env) -> int:
    started = time.perf_counter()
    run_root = env.run_root
    marker_path = run_root / "summarize.json"
    if marker_path.exists():
        marker = read_json(marker_path)
        if not _summary_marker_valid(run_root, marker):
            raise ValueError("summarize completion marker does not match derived outputs")
        print(json.dumps({"status": "ALREADY_DONE", "validation": "PASS"}))
        return 0
    combine = read_json(run_root / "combine.json")
    # ---- candidate-cell pass ----
    net_flat, net_traded = [], []
    status_counts, no_trade_counts, gate_context = Counter(), Counter(), Counter()
    per_candidate = defaultdict(lambda: {"cells_traded": 0, "trades": 0, "fills": 0, "entry_signals": 0,
                                         "net": [], "open_position_cells": 0, "open_order_cells": 0})
    per_cell = defaultdict(lambda: {"net_sum_flat": 0.0, "trade_candidates": 0, "trades": 0})
    totals = Counter()
    pairs = set()
    duplicates = 0
    with (run_root / "candidate_cell_results.jsonl").open("rb") as stream:
        for line in stream:
            row = json.loads(line)
            key = (row["cell_id"], row["parameter_identity"])
            if key in pairs:
                duplicates += 1
            pairs.add(key)
            status_counts[row["screening_status"]] += 1
            no_trade_counts[row["no_trade_status"]] += 1
            gate_context[row["gate_context"]] += 1
            for name in ("entry_signals", "submitted_intents", "fills", "completed_trades", "exit_signals"):
                totals[name] += row[name]
            totals["ending_position_nonzero"] += int(row["ending_position"] != 0)
            totals["ending_open_order"] += int(bool(row["ending_open_order"]))
            candidate = per_candidate[row["parameter_identity"]]
            candidate["trades"] += row["completed_trades"]
            candidate["fills"] += row["fills"]
            candidate["entry_signals"] += row["entry_signals"]
            candidate["cells_traded"] += int(row["completed_trades"] > 0)
            candidate["open_position_cells"] += int(row["ending_position"] != 0)
            candidate["open_order_cells"] += int(bool(row["ending_open_order"]))
            cell = per_cell[row["cell_id"]]
            cell["trades"] += row["completed_trades"]
            cell["trade_candidates"] += int(row["completed_trades"] > 0)
            net = row["fast_net_result"]
            if net is not None:
                net_flat.append(net)
                candidate["net"].append(net)
                cell["net_sum_flat"] += net
                if row["completed_trades"] > 0:
                    net_traded.append(net)
    rows = len(pairs) + duplicates
    cell_rows = [json.loads(line) for line in (run_root / "cell_results.jsonl").read_text().splitlines()]
    tiers = {row["cell"]["cell_id"]: row["cell"]["tier"] for row in cell_rows}
    cells_with_trade = sum(1 for value in per_cell.values() if value["trades"] > 0)
    # ---- trades pass ----
    trade_status, resolution = Counter(), Counter()
    trade_net = []
    with (run_root / "trades.jsonl").open("rb") as stream:
        for line in stream:
            trade = json.loads(line)
            trade_status[trade["status"]] += 1
            resolution[trade["decision_row_resolution"]] += 1
            if trade["net_pnl"] is not None:
                trade_net.append(trade["net_pnl"])
    candidate_rows = []
    for identity in env.candidate_order:
        value = per_candidate[identity]
        candidate_rows.append({
            "parameter_identity": identity, "cells_evaluated": EXPECTED_CELLS,
            "cells_traded": value["cells_traded"], "trade_frequency": value["cells_traded"] / EXPECTED_CELLS,
            "completed_trades": value["trades"], "fills": value["fills"], "entry_signals": value["entry_signals"],
            "open_position_cells": value["open_position_cells"], "open_order_cells": value["open_order_cells"],
            "net_fast_result_flat_cells": describe(value["net"]),
            "screening_only": True,
        })
    cand_path = run_root / "candidate_summary.jsonl"
    _write_jsonl_atomic(cand_path, candidate_rows)
    tier_cells = defaultdict(list)
    for cell_id, value in per_cell.items():
        tier_cells[tiers[cell_id]].append(value)
    summary = {
        "schema": "mwfd_04_full_run_summary_v1",
        "generated_at": now(),
        "interpretation": "descriptive screening statistics only; Fast is not production-authoritative; "
                          "no candidate ranking, selection, threshold or alpha conclusion is drawn",
        "coverage": {
            "total_cells_expected": EXPECTED_CELLS, "cells_completed": len(cell_rows),
            "candidates": EXPECTED_CANDIDATES, "candidate_cell_evaluations": rows,
            "expected_work_units": EXPECTED_CELLS * EXPECTED_CANDIDATES,
            "work_unit_difference": rows - EXPECTED_CELLS * EXPECTED_CANDIDATES,
            "failure_count": len(list((run_root / "failures").glob("*.json"))) if (run_root / "failures").exists() else 0,
            "duplicate_count": duplicates,
        },
        "trading": {
            "cells_with_any_trade": cells_with_trade, "cells_without_trade": len(cell_rows) - cells_with_trade,
            "candidate_cells_with_trade": sum(v["cells_traded"] for v in per_candidate.values()),
            "total_completed_trades": totals["completed_trades"], "fills": totals["fills"],
            "entry_signals": totals["entry_signals"], "submitted_intents_signals": totals["submitted_intents"],
            "exit_signals": totals["exit_signals"],
            "candidate_cells_ending_with_position": totals["ending_position_nonzero"],
            "candidate_cells_ending_with_open_order": totals["ending_open_order"],
            "screening_status": dict(sorted(status_counts.items())),
            "no_trade_status": dict(sorted(no_trade_counts.items())),
            "gate_context": dict(sorted(gate_context.items())),
            "trade_records": dict(sorted(trade_status.items())),
            "trade_decision_row_resolution": dict(sorted(resolution.items())),
        },
        "fast_net_result": {
            "all_flat_candidate_cells": describe(net_flat),
            "traded_flat_candidate_cells": describe(net_traded),
            "open_position_unranked_excluded": status_counts.get("OPEN_POSITION_UNRANKED", 0),
            "completed_trade_net": describe(trade_net),
            "unit": "KRW, quantity 1, fee_rate 0.001, Fast screening",
        },
        "cell_level": {
            "cell_net_sum_over_flat_candidates": describe([v["net_sum_flat"] for v in per_cell.values()]),
            "cell_trade_candidates": describe([v["trade_candidates"] for v in per_cell.values()]),
            "by_activity_stratum": {
                tier: {"cells": len(values),
                       "cells_with_trade": sum(v["trades"] > 0 for v in values),
                       "net_sum_flat": describe([v["net_sum_flat"] for v in values]),
                       "completed_trades": sum(v["trades"] for v in values)}
                for tier, values in sorted(tier_cells.items())
            },
        },
        "candidate_level": {
            "table": "candidate_summary.jsonl (frozen candidate order; not ranked)",
            "trade_frequency": describe([row["trade_frequency"] for row in candidate_rows]),
            "completed_trades_per_candidate": describe([row["completed_trades"] for row in candidate_rows]),
            "candidate_mean_net_flat": describe([row["net_fast_result_flat_cells"].get("mean") for row in candidate_rows]),
        },
    }
    assert_finite(summary)
    write_json_atomic(run_root / "full_run_summary.json", summary)
    # ---- gate ----
    counts, reasons = Counter(), Counter()
    for row in cell_rows:
        for key in ("PASS", "FAIL", "UNKNOWN", "evaluated"):
            counts[key] += row["gate"][key]
        reasons.update(row["gate"]["reasons"])
    inventory = read_json(win_to_local(env.manifest["mwfd02_root"], env.root) / "market_inventory.json")
    inv = Counter()
    for cell in inventory["cells"]:
        if cell["admission"] == "ELIGIBLE_FOR_FAST_PROBE":
            for key in ("PASS", "FAIL", "UNKNOWN", "evaluated"):
                inv[key] += cell["event_weighted_gate"][key]
    gate_summary = {
        "schema": "mwfd_04_gate_summary_v1", "gate_version": "v0", "threshold_ask10_notional_krw": 100_000_000,
        "max_quote_age_ns": env.manifest["account"]["max_quote_age_ns"],
        "evaluated": counts["evaluated"], "PASS": counts["PASS"], "FAIL": counts["FAIL"], "UNKNOWN": counts["UNKNOWN"],
        "pass_rate": counts["PASS"] / counts["evaluated"] if counts["evaluated"] else None,
        "reconciled": counts["PASS"] + counts["FAIL"] + counts["UNKNOWN"] == counts["evaluated"],
        "mwfd02_inventory_match": all(counts[k] == inv[k] for k in ("PASS", "FAIL", "UNKNOWN", "evaluated")),
        "reasons": dict(sorted(reasons.items())),
        "cells_with_zero_pass": sum(1 for row in cell_rows if row["gate"]["PASS"] == 0),
        "event_masking": False,
        "contract": "gate applies only to new entry evaluation; history/position/exit rows are preserved",
    }
    write_json_atomic(run_root / "gate_summary.json", gate_summary)
    # ---- runtime ----
    logs = [json.loads(line) for line in (run_root / "run_log.jsonl").read_text().splitlines()]
    steps = [step for log in logs for step in log["steps"]]
    by_stage = defaultdict(float)
    for step in steps:
        stage = step["stage"]
        stage = "sweep_part" if stage.startswith("sweep_part") else stage
        by_stage[stage] += step["wall_seconds"]
    materialization = read_json(run_root / "event_materialization.json")
    progress = read_json(run_root / "progress.json")
    active = sum(log["wall_seconds"] for log in logs)
    step_total = sum(step["wall_seconds"] for step in steps)
    cell_runtime = {row["cell"]["cell_id"]: (row["timing"]["compute_wall_seconds"] + row["timing"].get("publish_wall_seconds", 0)
                                              + row["timing"]["part_wall_seconds"] + row["timing"]["finalize_wall_seconds"])
                    for row in cell_rows}
    tier_rt = defaultdict(list)
    for row in cell_rows:
        tier_rt[row["cell"]["tier"]].append((row["cell"]["event_count"], cell_runtime[row["cell"]["cell_id"]]))
    first = datetime.fromisoformat(progress["started_at"])
    last = datetime.fromisoformat(logs[-1]["at"])
    source_pass = materialization["timing"]["source_pass_wall_seconds"]
    projection = env.manifest["projection_reference"]
    pipeline_active = active + source_pass
    exception_records = []
    exception_dir = run_root / "host_limit_exceptions"
    if exception_dir.exists():
        for path in sorted(exception_dir.glob("*.json")):
            record = read_json(path)
            exception_records.append({
                "path": path.name,
                "cell_id": record.get("cell", {}).get("cell_id"),
                "stages": [step.get("stage") for step in record.get("steps", [])],
                "step_wall_seconds": sum(float(step.get("wall_seconds", 0)) for step in record.get("steps", [])),
                "driver_sha256": record.get("driver_sha256"),
                "process_peak_rss_bytes": record.get("process_peak_rss_bytes"),
                "at": record.get("at"),
            })
    runtime = {
        "schema": "mwfd_04_runtime_summary_v1",
        "host": env.manifest["host"],
        "cell_pipeline": {
            "invocations": len(logs), "active_process_wall_seconds": active,
            "active_process_cpu_seconds": sum(log["cpu_seconds"] for log in logs),
            "step_wall_seconds": step_total,
            "startup_and_orchestration_inside_process_seconds": active - step_total,
            "by_stage_wall_seconds": dict(sorted(by_stage.items())),
            "serialization_wall_seconds": by_stage.get("compute_write", 0) + by_stage.get("publish", 0)
            + by_stage.get("publish_partial", 0) + by_stage.get("finalize", 0),
            "wall_clock_first_to_last_invocation_seconds": (last - first).total_seconds(),
            "inter_invocation_gap_seconds": (last - first).total_seconds() - active,
        },
        "event_cache_materialization": {
            "source_pass_wall_seconds": source_pass,
            "source_pass_invocations": materialization["timing"]["source_pass_invocations"],
            "note": "publish copy to FUSE-mounted run root and re-verification are excluded from compute",
        },
        "totals": {
            "active_compute_seconds_incl_source_pass": pipeline_active,
            "active_compute_hours_incl_source_pass": pipeline_active / 3600,
            "cells_per_hour_active": EXPECTED_CELLS / (active / 3600),
            "candidate_cell_evaluations_per_second_active": EXPECTED_CELLS * EXPECTED_CANDIDATES / active,
        },
        "projection_comparison": {
            "mwfd03_projected_total_seconds": projection["projected_total_seconds"],
            "mwfd03_range_seconds": projection["range_seconds"],
            "actual_active_seconds": pipeline_active,
            "deviation_pct": (pipeline_active / projection["projected_total_seconds"] - 1) * 100,
            "within_range": projection["range_seconds"][0] <= pipeline_active <= projection["range_seconds"][1],
        },
        "memory": {
            "peak_process_rss_bytes": max(log["process_peak_rss_bytes"] or 0 for log in logs),
            "peak_tracemalloc_bytes": max(log["tracemalloc_peak_bytes"] for log in logs),
            "projected_peak_tracemalloc_bytes": projection["projected_peak_tracemalloc_bytes"],
        },
        "host_limit_exceptions": {
            "count": len(exception_records),
            "step_wall_seconds": sum(item["step_wall_seconds"] for item in exception_records),
            "peak_process_rss_bytes": max((item["process_peak_rss_bytes"] or 0 for item in exception_records), default=0),
            "included_in_run_log_totals": False,
            "records": exception_records,
        },
        "tiers": {
            tier: {"cells": len(values), "events": sum(e for e, _ in values),
                   "runtime_seconds_total": sum(r for _, r in values),
                   "runtime_p50": quantile(sorted(r for _, r in values), 0.5),
                   "runtime_p90": quantile(sorted(r for _, r in values), 0.9),
                   "runtime_max": max(r for _, r in values),
                   "seconds_per_event": sum(r for _, r in values) / max(sum(e for e, _ in values), 1)}
            for tier, values in sorted(tier_rt.items())
        },
        "event_count_runtime_pearson": pearson([row["cell"]["event_count"] for row in cell_rows],
                                               [cell_runtime[row["cell"]["cell_id"]] for row in cell_rows]),
        "cache_reuse": {"event_cache": "shared MWFD-04 cache, 1 raw pass", "execution_depth": "MWFD-02 cache reused (HIT)",
                        "gate_feature": "built once per cell (MISS_BUILT), persisted in checkpoints"},
    }
    assert_finite(runtime)
    write_json_atomic(run_root / "runtime_summary.json", runtime)
    # ---- factor dataset manifest ----
    first_row = json.loads((run_root / "candidate_cell_results.jsonl").open("rb").readline())
    first_trade_line = (run_root / "trades.jsonl").open("rb").readline()
    first_factor = json.loads((run_root / "cell_factors.jsonl").open("rb").readline())
    factor_manifest = {
        "schema": "mwfd_04_factor_dataset_manifest_v1",
        "purpose": "MWFD-05 market-wide factor discovery input; venue-unverified bounded-prefix research",
        "tables": {
            "candidate_cell_results": {"path": "candidate_cell_results.jsonl", "grain": "cell x candidate (incl. no-trade)",
                                       "key": ["cell_id", "parameter_identity"], "rows": rows,
                                       "columns": sorted(first_row), "sha256": combine["outputs"]["candidate_cell_results.jsonl"]["sha256"]},
            "cell_results": {"path": "cell_results.jsonl", "grain": "cell", "key": ["cell_id"], "rows": len(cell_rows),
                             "sha256": combine["outputs"]["cell_results.jsonl"]["sha256"]},
            "cell_factors": {"path": "cell_factors.jsonl", "grain": "cell", "key": ["cell_id"],
                             "columns": sorted(first_factor), "factor_fields": sorted(first_factor["factors"]),
                             "sha256": combine["outputs"]["cell_factors.jsonl"]["sha256"]},
            "trades": {"path": "trades.jsonl", "grain": "cell x candidate x entry decision (opportunity/trade)",
                       "key": ["cell_id", "parameter_identity", "trade_index"],
                       "rows": combine["outputs"]["trades.jsonl"]["rows"],
                       "columns": sorted(json.loads(first_trade_line)) if first_trade_line else [],
                       "pre_entry_fields": sorted((json.loads(first_trade_line).get("pre_entry") or {})) if first_trade_line else [],
                       "sha256": combine["outputs"]["trades.jsonl"]["sha256"],
                       "note": "pre_entry is the causal feature row at the entry decision; same-ns ambiguity is labeled"},
            "candidate_summary": {"path": "candidate_summary.jsonl", "grain": "candidate", "key": ["parameter_identity"],
                                  "ordering": "frozen candidate order (not ranked)"},
            "candidate_family": {"path": "run_manifest.json#candidate_family", "count": EXPECTED_CANDIDATES},
            "per_cell_caches": {"feature_cache": "checkpoints/<index>-<code>-unknown/feature_cache/features.jsonl",
                                "gate_cache": "checkpoints/<index>-<code>-unknown/gate_cache/states.jsonl",
                                "candidate_detail": "checkpoints/<...>/parts/part-*.jsonl (full Fast result incl. fills)"},
        },
        "joins": ["candidate_cell_results.cell_id = cell_factors.cell_id = cell_results.cell.cell_id",
                  "trades.(cell_id, parameter_identity) -> candidate_cell_results",
                  "parameter_identity -> run_manifest.candidate_family.candidate_order"],
        "available_causal_factors": ["ask_depth_notional_10", "bid_depth_notional_10", "spread_pct", "depth_completeness",
                                     "top_of_book_status", "buy_ratio_by_ticks", "obi_ratio_bid3_ask3",
                                     "recent_volume_by_ticks", "prior_high/breakout (trade pre_entry)",
                                     "execution_gate PASS/FAIL/UNKNOWN", "clock_time_gate", "event/trade/quote activity"],
        "excluded": {"daily_metadata": "NOT_READY (market_cap, trading_value, listed_shares) — not joined",
                     "venue": "literal unknown; no KRX/NXT attribution"},
        "provenance": {
            "run_id": env.manifest["run_id"], "code_revision": env.manifest["code_revision"],
            "source_prefix_digest": env.manifest["source"]["prefix_digest"],
            "event_cache_id": materialization["cache"]["cache_id"],
            "execution_depth_cache_id": env.manifest["shared_cache"]["execution_depth_cache_id"],
            "candidate_family_digest": env.family_digest,
            "population_digest": env.population.ordered_cell_digest,
        },
        "statistical_caveats": ["single trade date 2026-09-21 bounded prefix", "discovery dataset; not validation",
                                "2026-09-18 holdout untouched", "Fast screening; production exact is authoritative"],
    }
    write_json_atomic(run_root / "factor_dataset_manifest.json", factor_manifest)
    marker = {
        "schema": "mwfd_04_summarize_completion_v1",
        "status": "COMPLETED",
        "completed_at": now(),
        "outputs": {
            name: {"bytes": (run_root / name).stat().st_size, "sha256": sha256_file(run_root / name)}
            for name in SUMMARY_OUTPUTS
        },
    }
    write_json_create(marker_path, marker)
    print(json.dumps({"status": "SUMMARIZED", "wall_seconds": time.perf_counter() - started}))
    return 0


def command_parquet(args, env: Env) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq
    target = env.run_root / "candidate_cell_results.parquet"
    if target.exists():
        print(json.dumps({"status": "ALREADY_DONE"}))
        return 0
    temp = env.run_root / ".candidate_cell_results.parquet.tmp"
    if temp.exists():
        temp.unlink()
    started = time.perf_counter()
    writer, batch, rows = None, [], 0

    def flat(row):
        feas = row.pop("feasibility")
        row.pop("candidate_aliases")
        row["gate_evaluated"], row["gate_pass"] = feas["evaluated"], feas["PASS"]
        row["gate_fail"], row["gate_unknown"] = feas["FAIL"], feas["UNKNOWN"]
        row["gate_event_weighted_pass_rate"] = feas["event_weighted_pass_rate"]
        row["gate_clock_time_pass_ratio"] = feas["clock_time"]["pass_ratio"]
        return row

    schema = None
    with (env.run_root / "candidate_cell_results.jsonl").open("rb") as stream:
        for line in stream:
            batch.append(flat(json.loads(line)))
            if len(batch) >= 50000:
                table = pa.Table.from_pylist(batch, schema=schema)
                schema = table.schema
                writer = writer or pq.ParquetWriter(temp, schema, compression="zstd")
                writer.write_table(table)
                rows += len(batch)
                batch = []
    if batch:
        table = pa.Table.from_pylist(batch, schema=schema)
        writer = writer or pq.ParquetWriter(temp, table.schema, compression="zstd")
        writer.write_table(table)
        rows += len(batch)
    writer.close()
    if rows != EXPECTED_CELLS * EXPECTED_CANDIDATES or pq.ParquetFile(temp).metadata.num_rows != rows:
        raise ValueError("parquet row reconciliation failed")
    os.rename(temp, target)
    print(json.dumps({"status": "DONE", "rows": rows, "wall_seconds": time.perf_counter() - started}))
    return 0


def command_manifest(args, env: Env) -> int:
    run_root = env.run_root
    target = run_root / "artifact_manifest.json"
    if target.exists():
        existing = read_json(target)
        if existing.get("status") != "COMPLETE":
            raise ValueError("existing artifact manifest is not COMPLETE")
        print(json.dumps({"status": "ALREADY_DONE", "validation": "PASS"}))
        return 0
    entries = []
    for path in sorted(p for p in run_root.iterdir() if p.is_file() and not p.name.startswith(".")):
        entries.append({"path": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    entries_by_name = {entry["path"]: entry for entry in entries}
    gate_errors = _completion_gate_errors(run_root, entries_by_name)
    if gate_errors:
        print(json.dumps({"status": "FINALIZATION_BLOCKED", "errors": gate_errors}))
        return 3
    checkpoint_bytes = sum(p.stat().st_size for p in env.checkpoints.rglob("*") if p.is_file())
    checkpoint_files = sum(1 for p in env.checkpoints.rglob("*") if p.is_file())
    event_bytes = sum(p.stat().st_size for p in (run_root / "event_cache").iterdir())
    completions = [read_json(env.checkpoints / checkpoint_name(c) / "completion.json") for c in env.population.cells]
    manifest = {
        "schema": "mwfd_04_artifact_manifest_v1", "run_id": env.manifest["run_id"], "generated_at": now(),
        "entries": entries,
        "directories": {
            "checkpoints": {"files": checkpoint_files, "bytes": checkpoint_bytes, "cells": len(completions),
                            "completion_logical_digest": json_digest(completions)},
            "event_cache": {"bytes": event_bytes, "cache_id": read_json(run_root / "event_cache" / "manifest.json")["cache_id"]},
        },
        "logical_digests": {
            "candidate_cell_results": "sha256 of canonical JSONL rows in (cell_index, parameter_identity) order == file sha256",
            "per_cell_economic_digest_list": read_json(run_root / "combine.json")["per_cell_economic_digest_list_digest"],
        },
        "total_bytes": sum(e["bytes"] for e in entries) + checkpoint_bytes + event_bytes,
        "finalization_provenance": {
            "finalizer_sha256": sha256_file(Path(__file__)),
            "calculation_code_revision": env.manifest["code_revision"],
            "calculation_code_provenance": env.manifest["code_provenance"],
            "host_limit_exceptions": read_json(run_root / "runtime_summary.json").get("host_limit_exceptions", {}),
        },
        "completion_gate": {"status": "PASS", "errors": []},
        "status": "COMPLETE",
    }
    write_json_create(target, manifest)
    print(json.dumps({"status": "DONE", "total_bytes": manifest["total_bytes"]}))
    return 0


def main(argv=None) -> int:
    value = argparse.ArgumentParser(description="MWFD-04 finalize")
    value.add_argument("command", choices=["combine", "crosscheck", "resumecheck", "summarize", "parquet", "manifest"])
    value.add_argument("--run-root", required=True)
    value.add_argument("--total-stock-root", required=True)
    value.add_argument("--scratch-dir", default=str(Path.home() / "mwfd04_scratch"))
    value.add_argument("--budget-seconds", type=float, default=150.0)
    args = value.parse_args(argv)
    env = Env(Path(args.run_root).absolute(), Path(args.total_stock_root).resolve(strict=True))
    return {"combine": command_combine, "crosscheck": command_crosscheck, "resumecheck": command_resumecheck,
            "summarize": command_summarize, "parquet": command_parquet, "manifest": command_manifest}[args.command](args, env)


if __name__ == "__main__":
    raise SystemExit(main())
