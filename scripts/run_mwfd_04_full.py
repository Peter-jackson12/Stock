"""MWFD-04 1,286-cell full Fast run (single worker, 시간 예산 단위 재개 실행).

한 번의 호출은 --budget-seconds 안에서 가능한 만큼 셀 단계를 진행하고 종료한다.
셀 단계: P1 계산(local scratch) → P2 게시(checkpoint building) → sweep part(후보 청크) → 확정.
각 단계는 create-only/atomic rename으로 끝나며, 중단되면 다음 호출이 미완 단계만 다시 한다.
후보 청크 분할은 후보별 평가가 서로 독립(evaluate_candidate)이므로 경제 결과를 바꾸지 않는다.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import pickle
try:
    import resource
except ImportError:  # Windows
    resource = None
import shutil
import statistics
import sys
import time
import traceback
import tracemalloc
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research.fast_backtest.execution_depth import (
    load_cell_execution_depth_from_manifest,
    load_execution_depth_cache,
)
from research.fast_backtest.feasibility import feasibility_summary, join_entry_feasibility
from research.fast_backtest.features import build_causal_features, load_feature_cache, write_feature_cache
from research.fast_backtest.full_run import (
    EXPECTED_CANDIDATES,
    EXPECTED_CELLS,
    ResumableSha256,
    checkpoint_name,
    load_full_population,
    read_json,
    write_json_atomic,
)
from research.fast_backtest.plan import canonical_json
from research.fast_backtest.runtime_probe import (
    assert_finite,
    json_digest,
    load_candidate_family,
    load_gate_cache,
    load_probe_cell_events,
    load_probe_event_cache,
    sha256_file,
    write_gate_cache,
    write_json_create,
)
from research.fast_backtest.sweep import (
    FastAccountAssumptions,
    FastFill,
    FastSweepResult,
    feature_config_for_candidates,
    materialize_strategy_params,
    result_record,
    run_fast_sweep,
)
from scripts.materialize_mwfd_04_events import verify_code_provenance, win_to_local
from scripts.run_mwfd_03_probe import candidate_cell_record, economic_record, percentile

KST = timezone(timedelta(hours=9))
PUBLISH_BYTES_PER_SECOND = 6_000_000
SWEEP_SECONDS_PER_EVENT_CANDIDATE = 4.3e-6
PART_TARGET_SECONDS = 50.0
MAX_CONSECUTIVE_FAILURES = 3


def now() -> str:
    return datetime.now(KST).isoformat()


def peak_rss_bytes() -> int | None:
    if resource is None:
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def part_size(event_count: int) -> int:
    per_candidate = max(event_count, 1) * SWEEP_SECONDS_PER_EVENT_CANDIDATE
    return max(1, min(EXPECTED_CANDIDATES, int(PART_TARGET_SECONDS // per_candidate)))


def result_from_record(value: dict) -> FastSweepResult:
    data = dict(value)
    data["aliases"] = tuple(data["aliases"])
    data["entry_decision_ns"] = tuple(data["entry_decision_ns"])
    data["exit_decision_ns"] = tuple(data["exit_decision_ns"])
    data["fill_records"] = tuple(FastFill(**fill) for fill in data["fill_records"])
    return FastSweepResult(**data)


def distribution(values) -> dict:
    values = [value for value in values if value is not None]
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": min(values), "p10": percentile(values, 0.1), "p50": percentile(values, 0.5),
        "p90": percentile(values, 0.9), "max": max(values), "mean": statistics.fmean(values),
    }


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------

class Context:
    def __init__(self, run_root: Path, total_stock_root: Path):
        self.run_root = run_root
        self.root = total_stock_root
        self.manifest = read_json(run_root / "run_manifest.json")
        verify_code_provenance(self.manifest)
        if not (run_root / "event_materialization.json").exists():
            raise ValueError("event cache is not verified yet")
        mwfd02 = win_to_local(self.manifest["mwfd02_root"], total_stock_root)
        self.population = load_full_population(
            mwfd02 / "market_inventory.json", expected_sha256=self.manifest["population"]["inventory_sha256"]
        )
        if self.population.ordered_cell_digest != self.manifest["population"]["ordered_cell_digest"]:
            raise ValueError("POPULATION_DRIFT")
        family_source = win_to_local(self.manifest["candidate_family"]["source_path"], total_stock_root)
        self.family = load_candidate_family(family_source)
        if self.family.canonical_family_digest != self.manifest["candidate_family"]["canonical_family_digest"]:
            raise ValueError("CANDIDATE_IDENTITY_DRIFT")
        self.candidate_order = [candidate.parameter_identity for candidate in self.family.candidates]
        if self.candidate_order != sorted(self.candidate_order):
            raise ValueError("candidate family must be ordered by parameter identity")
        source_digest = self.manifest["source"]["prefix_digest"]
        self.event_cache = load_probe_event_cache(
            run_root / "event_cache", expected_source_prefix_digest=source_digest,
            expected_sample_digest=self.population.ordered_cell_digest, verify_payload=False,
        )
        materialization = read_json(run_root / "event_materialization.json")
        if materialization["cache"]["cache_id"] != self.event_cache.manifest["cache_id"]:
            raise ValueError("EVENT_CACHE_IDENTITY_DRIFT")
        depth_path = win_to_local(self.manifest["shared_cache"]["execution_depth_path"], total_stock_root)
        self.depth = load_execution_depth_cache(depth_path, expected_source_prefix_digest=source_digest,
                                                verify_payload=False)
        if self.depth.cache_id != self.manifest["shared_cache"]["execution_depth_cache_id"]:
            raise ValueError("DEPTH_CACHE_IDENTITY_DRIFT")
        inventory = read_json(mwfd02 / "market_inventory.json")
        self.inventory = {cell["cell_id"]: cell for cell in inventory["cells"]}
        self.account = FastAccountAssumptions(**self.manifest["account"])
        self.feature_config = feature_config_for_candidates(self.family.candidates, self.account.max_quote_age_ns)
        self.params = {
            candidate.parameter_identity: materialize_strategy_params(candidate.params)
            for candidate in self.family.candidates
        }
        self.checkpoints = run_root / "checkpoints"
        self.failures = run_root / "failures"


# ---------------------------------------------------------------------------
# trade/opportunity records
# ---------------------------------------------------------------------------

def _static_entry_ok(row, params, state, account) -> bool:
    entry = params["entry"]
    recent_key = str(entry["recent_ticks"])
    ratio = row.buy_ratio_by_ticks.get(recent_key)
    volume = row.recent_volume_by_ticks.get(recent_key)
    prior_high = row.prior_high_by_window.get(str(entry["breakout_window_sec"]))
    open_price = row.open_price_by_session.get(str(entry["session_start_sec"]))
    fresh = bool(row.quote_eligible and row.quote_received_ns is not None
                 and 0 <= row.received_ns - row.quote_received_ns <= account.max_quote_age_ns)
    return bool(
        state is not None and state.status == "PASS" and fresh and open_price is not None
        and row.bid3 is not None and row.ask3 is not None and row.ask3 > 0
        and ratio is not None and volume is not None and prior_high is not None and row.spread_pct is not None
        and row.trade_price >= open_price and row.trade_price >= prior_high
        and row.spread_pct <= float(entry["spread_max_pct"]) and ratio >= float(entry["buy_ratio_min"])
        and row.bid3 > float(entry["obi_min_ratio"]) * row.ask3 and volume >= int(entry["min_vol_15t"])
    )


def trade_records(result, *, cell, params, trades_by_ns, gate_states, account) -> list[dict]:
    params_value, exit_rule = params
    entry = params_value["entry"]
    buys = [fill for fill in result.fill_records if fill.side == "buy"]
    sells = [fill for fill in result.fill_records if fill.side == "sell"]
    records = []
    for index, decision_ns in enumerate(result.entry_decision_ns):
        rows = trades_by_ns.get(decision_ns, [])
        chosen, resolution = None, "UNRESOLVED"
        if len(rows) == 1:
            chosen, resolution = rows[0], "UNIQUE_TRADE_ROW"
        elif rows:
            matches = [row for row in rows if _static_entry_ok(row, params_value, gate_states.get(row.seq), account)]
            if matches:
                chosen, resolution = matches[0], "FIRST_STATIC_ELIGIBLE_SAME_NS"
        buy = buys[index] if index < len(buys) else None
        sell = sells[index] if index < len(sells) else None
        exit_ns = result.exit_decision_ns[index] if index < len(result.exit_decision_ns) else None
        status = "COMPLETED" if sell else "OPEN_POSITION" if buy else "ENTRY_UNFILLED"
        pre = None
        if chosen is not None:
            state = gate_states.get(chosen.seq)
            recent_key = str(entry["recent_ticks"])
            prior_high = chosen.prior_high_by_window.get(str(entry["breakout_window_sec"]))
            pre = {
                "seq": chosen.seq, "market_second": chosen.market_second, "trade_price": chosen.trade_price,
                "trade_volume": chosen.trade_volume, "spread_pct": chosen.spread_pct,
                "bid": chosen.bid, "ask": chosen.ask, "bid_size": chosen.bid_size, "ask_size": chosen.ask_size,
                "bid3": chosen.bid3, "ask3": chosen.ask3,
                "obi_ratio": (chosen.bid3 / chosen.ask3) if chosen.bid3 is not None and chosen.ask3 else None,
                "quote_age_ns": chosen.quote_age_ns,
                "buy_ratio": chosen.buy_ratio_by_ticks.get(recent_key),
                "recent_volume": chosen.recent_volume_by_ticks.get(recent_key),
                "unknown_direction": chosen.unknown_direction_by_ticks.get(recent_key),
                "prior_high": prior_high,
                "breakout_margin_pct": ((chosen.trade_price / prior_high) - 1) if prior_high else None,
                "gate_status": state.status if state else None,
                "gate_reason": state.reason if state else None,
                "ask_depth_notional_10": state.ask_depth_notional_10 if state else None,
            }
        gross = fees = net = None
        if buy and sell:
            gross = (sell.price - buy.price) * buy.quantity
            fees = buy.fee + sell.fee
            net = gross - fees
        record = {
            "cell_index": cell.index, "cell_id": cell.cell_id, "code": cell.code, "venue": cell.venue,
            "activity_stratum": cell.tier,
            "candidate_id": result.candidate_id, "parameter_identity": result.parameter_identity,
            "exit_rule": exit_rule, "trade_index": index, "status": status,
            "entry_decision_ns": decision_ns, "decision_row_resolution": resolution,
            "same_ns_trade_rows": len(rows),
            "entry_fill": asdict(buy) if buy else None,
            "exit_decision_ns": exit_ns,
            "exit_fill": asdict(sell) if sell else None,
            "holding_ns": (sell.time_ns - buy.time_ns) if buy and sell else None,
            "gross_pnl": gross, "fees": fees, "net_pnl": net,
            "pre_entry": pre,
            "screening_only": True,
        }
        assert_finite(record)
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# cell factor summary (현재 causal하게 존재하는 값만; daily metadata 제외)
# ---------------------------------------------------------------------------

def cell_factor_summary(features, gate, inventory_cell, recent_keys) -> dict:
    trade_rows = [row for row in features.rows if row.kind == "trade"]
    fresh = [row for row in trade_rows if row.quote_eligible and row.spread_pct is not None]
    value = {
        "trade_rows": len(trade_rows),
        "quote_rows": len(features.rows) - len(trade_rows),
        "trade_quote_eligible_rate": (len(fresh) / len(trade_rows)) if trade_rows else None,
        "trade_value_krw": sum(row.trade_price * row.trade_volume for row in trade_rows),
        "trade_price": distribution([row.trade_price for row in trade_rows]),
        "spread_pct": distribution([row.spread_pct for row in fresh]),
        "obi_ratio_bid3_ask3": distribution([row.bid3 / row.ask3 for row in fresh
                                             if row.bid3 is not None and row.ask3]),
        "buy_ratio_by_ticks": {key: distribution([row.buy_ratio_by_ticks.get(key) for row in trade_rows])
                               for key in recent_keys},
        "recent_volume_by_ticks": {key: distribution([row.recent_volume_by_ticks.get(key) for row in trade_rows])
                                   for key in recent_keys},
        "execution_gate": {k: gate[k] for k in ("evaluated", "PASS", "FAIL", "UNKNOWN", "pass_rate", "reasons")},
        "clock_time_gate": inventory_cell["clock_time_gate"],
        "activity": inventory_cell["activity"],
        "quality": inventory_cell["quality"],
        "daily_metadata": "NOT_READY_EXCLUDED",
    }
    assert_finite(value)
    return value


# ---------------------------------------------------------------------------
# resumable directory publish (local scratch → checkpoint building, FUSE)
# ---------------------------------------------------------------------------

def publish_tree(src: Path, dst: Path, state_path: Path, deadline: float) -> bool:
    files = sorted(p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file())
    state = read_json(state_path) if state_path.exists() else {"done": {}, "current": None}
    for name in files:
        if name in state["done"]:
            continue
        source, target = src / name, dst / name
        target.parent.mkdir(parents=True, exist_ok=True)
        current = state["current"] if state["current"] and state["current"]["name"] == name else None
        if current is None:
            if target.exists():
                target.unlink()
            target.touch(exist_ok=False)
            current = {"name": name, "offset": 0, "sha": ResumableSha256().state().hex()}
        digest = ResumableSha256(bytes.fromhex(current["sha"]))
        total = source.stat().st_size
        with source.open("rb") as reader, target.open("r+b") as writer:
            writer.truncate(current["offset"])
            reader.seek(current["offset"])
            writer.seek(current["offset"])
            while current["offset"] < total:
                if time.perf_counter() >= deadline:
                    state["current"] = current
                    write_json_atomic(state_path, state)
                    return False
                block = reader.read(min(16 << 20, total - current["offset"]))
                writer.write(block)
                digest.update(block)
                current["offset"] += len(block)
                current["sha"] = digest.state().hex()
            writer.flush()
            os.fsync(writer.fileno())
        if digest.hexdigest() != sha256_file(source) or sha256_file(target) != digest.hexdigest():
            raise ValueError(f"publish digest mismatch: {name}")
        state["done"][name] = digest.hexdigest()
        state["current"] = None
        write_json_atomic(state_path, state)
    return True


# ---------------------------------------------------------------------------
# cell stages
# ---------------------------------------------------------------------------

class CellRunner:
    def __init__(self, ctx: Context, scratch_root: Path, clock_deadline: float):
        self.ctx = ctx
        self.scratch_root = scratch_root
        self.deadline = clock_deadline
        self.did_work = False
        self.steps = []
        self.memo = {}
        self.work_memo = {}
        self.call_id = f"{os.getpid()}-{uuid4().hex[:8]}"

    def affordable(self, estimate: float) -> bool:
        remaining = self.deadline - time.perf_counter()
        return estimate <= remaining or (not self.did_work and estimate <= remaining + 25)

    def record_step(self, cell, stage, wall, cpu, **extra):
        self.did_work = True
        self.steps.append({"cell_index": cell.index, "cell_id": cell.cell_id, "stage": stage,
                           "wall_seconds": wall, "cpu_seconds": cpu, **extra})

    def paths(self, cell):
        name = checkpoint_name(cell)
        return (self.ctx.checkpoints / name, self.ctx.checkpoints / f".building-{name}",
                self.scratch_root / name)

    # P1 (staged local compute) -------------------------------------------
    STEPS = ("events", "gate", "features", "write")
    STEP_RATE = {"events": 0.00036, "gate": 0.00055, "features": 0.00072, "write": 0.00036}
    UNPICKLE_RATE = 0.00006

    def work_dir(self, cell) -> Path:
        return self.scratch_root / f".work-{checkpoint_name(cell)}"

    def _dump(self, path: Path, value) -> None:
        temp = path.with_name(f".{path.name}.tmp")
        with temp.open("wb") as stream:
            pickle.dump(value, stream, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temp, path)

    def _load(self, path: Path):
        with path.open("rb") as stream:
            return pickle.load(stream)

    def compute_next_step(self, cell, building: Path, scratch: Path) -> str | None:
        """다음 미완 compute 단계를 반환(모두 끝났으면 None)."""
        work = self.work_dir(cell)
        for step in self.STEPS:
            if not (work / f"step-{step}.done.json").exists():
                return step
        return None

    def compute_estimate(self, cell, step: str) -> float:
        memo = self.work_memo.get(cell.cell_id, {})
        need = {"gate": "events", "features": "events", "write": "features"}.get(step)
        extra = cell.event_count * self.UNPICKLE_RATE if need and need not in memo else 0.0
        return cell.event_count * self.STEP_RATE[step] + extra + 1.0

    def compute_step(self, cell, building: Path, scratch: Path, step: str) -> None:
        wall, cpu = time.perf_counter(), time.process_time()
        tracemalloc.reset_peak()
        work = self.work_dir(cell)
        out = work / "out"
        memo = self.work_memo.setdefault(cell.cell_id, {})
        event_meta = self.ctx.event_cache.manifest["cells"][cell.cell_id]
        inventory_cell = self.ctx.inventory[cell.cell_id]

        def need(name):
            if name not in memo:
                path = work / f"{name}.pkl"
                if not path.exists():
                    raise LookupError(name)
                memo[name] = self._load(path)
            return memo[name]

        info = {}
        if step == "events":
            if building.exists():
                shutil.rmtree(building)
            if scratch.exists():
                shutil.rmtree(scratch)
            if work.exists():
                shutil.rmtree(work)
            out.mkdir(parents=True)
            events = load_probe_cell_events(self.ctx.event_cache, cell.cell_id)
            if len(events) != cell.event_count:
                raise ValueError("cell event count differs from MWFD-02 inventory")
            memo["events"] = events
            info = {"source": events[0].source, "session_id": events[0].session_id}
        elif step == "gate":
            events = need("events")
            depths = load_cell_execution_depth_from_manifest(self.ctx.depth, code=cell.code, venue=cell.venue)
            gate_states = join_entry_feasibility(events, depths, max_quote_age_ns=self.ctx.account.max_quote_age_ns)
            gate = feasibility_summary(gate_states)
            expected_gate = inventory_cell["event_weighted_gate"]
            for key in ("evaluated", "PASS", "FAIL", "UNKNOWN"):
                if gate[key] != expected_gate[key]:
                    raise ValueError("MWFD-02 gate reconciliation failed")
            gate_cache = out / "gate_cache"
            if gate_cache.exists():
                shutil.rmtree(gate_cache)
            gate_manifest = write_gate_cache(gate_cache, states=gate_states, event_digest=event_meta["event_digest"],
                                             depth_cache_id=self.ctx.depth.cache_id)
            memo["gate_states"] = gate_states
            info = {
                "gate": gate, "gate_digest": gate_manifest["state_digest"], "quote_count": len(depths),
                "depth_factors": {
                    "ask_depth_notional_10": distribution([d.ask_depth_notional_10 for d in depths if d.ask_status == "COMPLETE"]),
                    "bid_depth_notional_10": distribution([d.bid_depth_notional_10 for d in depths if d.bid_status == "COMPLETE"]),
                    "depth_completeness": dict(sorted(Counter(d.completeness for d in depths).items())),
                    "top_of_book_status": dict(sorted(Counter(d.top_status for d in depths).items())),
                },
            }
        elif step == "features":
            events = need("events")
            features = build_causal_features(events, config=self.ctx.feature_config,
                                             input_event_digest=event_meta["event_digest"])
            if len(features.rows) != len(events) or any(r.seq != e.seq for r, e in zip(features.rows, events)):
                raise ValueError("feature rows must preserve every input event")
            memo["features"] = features
            memo.pop("events", None)
            info = {"feature_digest": features.feature_digest}
        elif step == "write":
            features = need("features")
            feature_cache = out / "feature_cache"
            if feature_cache.exists():
                shutil.rmtree(feature_cache)
            write_feature_cache(features, feature_cache)
            done = {name: read_json(work / f"step-{name}.done.json") for name in ("events", "gate", "features")}
            gate = done["gate"]["info"]["gate"]
            factors = cell_factor_summary(features, gate, inventory_cell,
                                          [str(key) for key in self.ctx.feature_config.recent_ticks])
            factors.update(done["gate"]["info"]["depth_factors"])
            timing_steps = {name: done[name]["wall_seconds"] for name in done}
            timing_steps["write"] = time.perf_counter() - wall
            pending = {
                "schema": "mwfd_04_cell_prepare_v1",
                "cell": cell.to_dict(),
                "event": {"count": cell.event_count, "trade_count": gate["evaluated"],
                          "quote_count": done["gate"]["info"]["quote_count"],
                          "digest": event_meta["event_digest"], "bytes": event_meta["event_bytes"],
                          "source": done["events"]["info"]["source"], "session_id": done["events"]["info"]["session_id"]},
                "gate": gate,
                "gate_digest": done["gate"]["info"]["gate_digest"],
                "feature_digest": features.feature_digest,
                "part_size": part_size(cell.event_count),
                "factors": factors,
                "timing": {
                    "event_access": timing_steps["events"], "execution_feasibility": timing_steps["gate"],
                    "causal_feature": timing_steps["features"], "local_cache_write": timing_steps["write"],
                    "compute_wall_seconds": sum(timing_steps.values()),
                    "compute_cpu_seconds": sum(done[name]["cpu_seconds"] for name in done) + (time.process_time() - cpu),
                    "compute_invocation_split": len({done[name]["pid_call"] for name in done} | {self.call_id}),
                },
                "memory": {
                    "tracemalloc_peak_bytes": max([done[n]["tracemalloc_peak_bytes"] for n in done]
                                                  + [tracemalloc.get_traced_memory()[1]]),
                    "process_peak_rss_bytes": peak_rss_bytes(),
                },
            }
            write_json_create(out / "prepare_pending.json", pending)
            if scratch.exists():
                shutil.rmtree(scratch)
            out.rename(scratch)
            self.memo[cell.cell_id] = (features, memo.get("gate_states") or load_gate_cache(
                scratch / "gate_cache", expected_event_digest=event_meta["event_digest"],
                expected_depth_cache_id=self.ctx.depth.cache_id))
            shutil.rmtree(work)
            self.work_memo.pop(cell.cell_id, None)
            self.record_step(cell, "compute_write", time.perf_counter() - wall, time.process_time() - cpu)
            return
        _, trace_peak = tracemalloc.get_traced_memory()
        write_json_create(work / f"step-{step}.done.json", {
            "step": step, "wall_seconds": time.perf_counter() - wall, "cpu_seconds": time.process_time() - cpu,
            "tracemalloc_peak_bytes": trace_peak, "info": info, "pid_call": self.call_id,
        })
        self.record_step(cell, f"compute_{step}", time.perf_counter() - wall, time.process_time() - cpu,
                         tracemalloc_peak_bytes=trace_peak)

    def persist_for_defer(self, cell, next_step: str) -> None:
        """다음 호출로 넘기기 전에 다음 단계가 필요로 하는 객체만 local scratch에 보존한다."""
        work = self.work_dir(cell)
        memo = self.work_memo.get(cell.cell_id, {})
        name = {"gate": "events", "features": "events", "write": "features"}.get(next_step)
        if name and name in memo and not (work / f"{name}.pkl").exists():
            self._dump(work / f"{name}.pkl", memo[name])

    # P2 ------------------------------------------------------------------
    def publish(self, cell, building: Path, scratch: Path) -> bool:
        wall, cpu = time.perf_counter(), time.process_time()
        building.mkdir(parents=True, exist_ok=True)
        state = building / ".publish_state.json"
        done = publish_tree(scratch, building, state, self.deadline)
        if done:
            pending = read_json(scratch / "prepare_pending.json")
            if (building / "prepare_pending.json").exists():
                (building / "prepare_pending.json").unlink()
            pending["timing"]["publish_wall_seconds"] = time.perf_counter() - wall
            write_json_create(building / "prepare.json", pending)
            state.unlink()
            shutil.rmtree(scratch)
        self.record_step(cell, "publish" if done else "publish_partial",
                         time.perf_counter() - wall, time.process_time() - cpu)
        return done

    # parts -----------------------------------------------------------------
    def load_prepared(self, cell, building: Path, prepare: dict):
        if cell.cell_id in self.memo:
            return self.memo[cell.cell_id]
        features = load_feature_cache(building / "feature_cache", expected_input_event_digest=prepare["event"]["digest"])
        if features.feature_digest != prepare["feature_digest"] or features.config != self.ctx.feature_config:
            raise ValueError("feature cache identity mismatch")
        gate_states = load_gate_cache(building / "gate_cache", expected_event_digest=prepare["event"]["digest"],
                                      expected_depth_cache_id=self.ctx.depth.cache_id)
        self.memo = {cell.cell_id: (features, gate_states)}
        return features, gate_states

    def sweep_part(self, cell, building: Path, prepare: dict, index: int):
        wall, cpu = time.perf_counter(), time.process_time()
        tracemalloc.reset_peak()
        features, gate_states = self.load_prepared(cell, building, prepare)
        size = prepare["part_size"]
        candidates = self.ctx.family.candidates[index * size:(index + 1) * size]
        parts = building / "parts"
        parts.mkdir(exist_ok=True)
        stem = f"part-{index:03d}"
        for leftover in parts.glob(f".{stem}*"):
            leftover.unlink()
        for name in (f"{stem}.jsonl", f"trades-{index:03d}.jsonl"):
            if (parts / name).exists():
                (parts / name).unlink()
        t = time.perf_counter()
        results = sorted(run_fast_sweep(features, candidates, account=self.ctx.account,
                                        entry_feasibility=gate_states),
                         key=lambda item: item.parameter_identity)
        sweep_seconds = time.perf_counter() - t
        if [item.parameter_identity for item in results] != [c.parameter_identity for c in candidates]:
            raise ValueError("candidate identities changed during sweep")
        trades_by_ns = {}
        if any(result.entry_decision_ns for result in results):
            for row in features.rows:
                if row.kind == "trade":
                    trades_by_ns.setdefault(row.received_ns, []).append(row)
        result_tmp = parts / f".{stem}.jsonl.tmp"
        trade_tmp = parts / f".trades-{index:03d}.jsonl.tmp"
        result_digest, trade_digest, trade_count = hashlib.sha256(), hashlib.sha256(), 0
        with result_tmp.open("x", encoding="utf-8", newline="\n") as rs, trade_tmp.open("x", encoding="utf-8", newline="\n") as ts:
            for result in results:
                record = result_record(result)
                assert_finite(record)
                line = canonical_json(record) + "\n"
                rs.write(line)
                result_digest.update(line.encode("utf-8"))
                for trade in trade_records(result, cell=cell, params=self.ctx.params[result.parameter_identity],
                                           trades_by_ns=trades_by_ns, gate_states=gate_states, account=self.ctx.account):
                    tline = canonical_json(trade) + "\n"
                    ts.write(tline)
                    trade_digest.update(tline.encode("utf-8"))
                    trade_count += 1
            rs.flush(); os.fsync(rs.fileno()); ts.flush(); os.fsync(ts.fileno())
        result_tmp.rename(parts / f"{stem}.jsonl")
        trade_tmp.rename(parts / f"trades-{index:03d}.jsonl")
        _, trace_peak = tracemalloc.get_traced_memory()
        write_json_create(parts / f"{stem}.meta.json", {
            "index": index, "candidate_start": index * size, "candidate_count": len(candidates),
            "result_file_digest": result_digest.hexdigest(), "trade_file_digest": trade_digest.hexdigest(),
            "trade_records": trade_count, "sweep_wall_seconds": sweep_seconds,
            "part_wall_seconds": time.perf_counter() - wall, "tracemalloc_peak_bytes": trace_peak,
            "process_peak_rss_bytes": peak_rss_bytes(),
        })
        self.record_step(cell, f"sweep_part_{index}", time.perf_counter() - wall, time.process_time() - cpu,
                         candidates=len(candidates), sweep_seconds=sweep_seconds, tracemalloc_peak_bytes=trace_peak)

    # finalize ----------------------------------------------------------------
    def finalize(self, cell, final: Path, building: Path, prepare: dict, part_count: int):
        wall, cpu = time.perf_counter(), time.process_time()
        parts = building / "parts"
        records, metas = [], []
        for index in range(part_count):
            meta = read_json(parts / f"part-{index:03d}.meta.json")
            data = (parts / f"part-{index:03d}.jsonl").read_bytes()
            if hashlib.sha256(data).hexdigest() != meta["result_file_digest"]:
                raise ValueError("part result digest mismatch")
            tdata = (parts / f"trades-{index:03d}.jsonl").read_bytes()
            if hashlib.sha256(tdata).hexdigest() != meta["trade_file_digest"]:
                raise ValueError("part trade digest mismatch")
            records.extend(json.loads(line) for line in data.decode("utf-8").splitlines())
            metas.append(meta)
        results = [result_from_record(value) for value in records]
        if [item.parameter_identity for item in results] != self.ctx.candidate_order:
            raise ValueError("candidate-cell reconciliation failed (identity/order)")
        economic_digest = json_digest([economic_record(result) for result in results])
        sweep_wall = sum(meta["sweep_wall_seconds"] for meta in metas)
        inventory_cell = self.ctx.inventory[cell.cell_id]
        gate = prepare["gate"]
        result_path = building / "candidate_results.jsonl"
        if result_path.exists():
            result_path.unlink()
        file_digest = hashlib.sha256()
        no_trade, trade_candidates, pnl = Counter(), 0, []
        with result_path.open("x", encoding="utf-8", newline="\n") as stream:
            for result in results:
                row = candidate_cell_record(result, cell=cell, gate=gate, inventory_cell=inventory_cell,
                                            sweep_seconds=sweep_wall, candidate_count=len(results))
                row["source"] = prepare["event"]["source"]
                row["session_id"] = prepare["event"]["session_id"]
                ending_cash = self.ctx.account.cash + sum(
                    (-fill.price * fill.quantity - fill.fee) if fill.side == "buy" else (fill.price * fill.quantity - fill.fee)
                    for fill in result.fill_records)
                row.update({
                    "cell_index": cell.index,
                    "execution_status": "EVALUATED",
                    "gate_context": "GATE_NO_PASS" if gate["PASS"] == 0 else "GATE_HAS_PASS",
                    "max_adverse_pct": result.max_adverse_pct,
                    "exit_signals": result.exit_signals,
                    "ending_cash": ending_cash,
                    "ending_equity": ending_cash if result.terminal_position == 0 else None,
                    "ending_equity_status": "FLAT_CASH" if result.terminal_position == 0 else "OPEN_POSITION_UNMARKED",
                })
                assert_finite(row)
                no_trade[row["no_trade_status"]] += 1
                trade_candidates += int(result.trades > 0)
                if result.net_pnl is not None:
                    pnl.append(result.net_pnl)
                line = canonical_json(row) + "\n"
                stream.write(line)
                file_digest.update(line.encode("utf-8"))
            stream.flush(); os.fsync(stream.fileno())
        trades_path = building / "trades.jsonl"
        if trades_path.exists():
            trades_path.unlink()
        trade_digest, trade_count = hashlib.sha256(), 0
        with trades_path.open("xb") as stream:
            for index in range(part_count):
                data = (parts / f"trades-{index:03d}.jsonl").read_bytes()
                stream.write(data)
                trade_digest.update(data)
                trade_count += data.count(b"\n")
            stream.flush(); os.fsync(stream.fileno())
        timing = dict(prepare["timing"])
        timing.update({"candidate_sweep": sweep_wall, "parts": len(metas),
                       "part_wall_seconds": sum(m["part_wall_seconds"] for m in metas),
                       "finalize_wall_seconds": time.perf_counter() - wall})
        cell_result = {
            "schema": "mwfd_04_cell_result_v1",
            "status": "COMPLETED",
            "cell": cell.to_dict(),
            "event": prepare["event"],
            "gate": gate,
            "clock_time_gate": inventory_cell["clock_time_gate"],
            "candidate_count": len(results),
            "candidate_results_digest": economic_digest,
            "candidate_results_file_digest": file_digest.hexdigest(),
            "trade_records": trade_count,
            "trade_records_file_digest": trade_digest.hexdigest(),
            "candidate_outcomes": {
                "trade_candidate_count": trade_candidates,
                "no_trade_status_counts": dict(sorted(no_trade.items())),
                "completed_trades": sum(r.trades for r in results),
                "fills": sum(r.fills for r in results),
                "entry_signals": sum(r.entry_signals for r in results),
                "signals": sum(r.signals for r in results),
                "net_pnl_min": min(pnl) if pnl else None,
                "net_pnl_max": max(pnl) if pnl else None,
                "net_pnl_mean": statistics.fmean(pnl) if pnl else None,
                "net_pnl_sum_flat": sum(pnl) if pnl else 0.0,
            },
            "cache": {"event": "HIT_SHARED_MWFD04", "execution_depth": "HIT_SHARED_MWFD02",
                      "gate": "MISS_BUILT", "feature": "MISS_BUILT",
                      "gate_digest": prepare["gate_digest"], "feature_digest": prepare["feature_digest"]},
            "timing": timing,
            "memory": {
                "tracemalloc_peak_bytes": max([prepare["memory"]["tracemalloc_peak_bytes"]]
                                              + [m["tracemalloc_peak_bytes"] for m in metas]),
                "process_peak_rss_bytes": max([v for v in [prepare["memory"]["process_peak_rss_bytes"]]
                                              + [m["process_peak_rss_bytes"] for m in metas] if v is not None],
                                             default=None),
            },
            "screening_only": True,
        }
        assert_finite(cell_result)
        for name in ("cell_result.json", "completion.json"):
            if (building / name).exists():
                (building / name).unlink()
        write_json_create(building / "cell_result.json", cell_result)
        completion = {
            "schema": "mwfd_04_cell_completion_v1",
            "status": "COMPLETED",
            "cell_id": cell.cell_id,
            "cell_index": cell.index,
            "event_digest": prepare["event"]["digest"],
            "candidate_family_digest": self.ctx.family.canonical_family_digest,
            "candidate_count": len(results),
            "candidate_results_digest": economic_digest,
            "candidate_results_file_digest": file_digest.hexdigest(),
            "trade_records_file_digest": trade_digest.hexdigest(),
            "code_revision": self.ctx.manifest["code_revision"],
            "completed_at": now(),
        }
        write_json_create(building / "completion.json", completion)
        if final.exists():
            raise FileExistsError("final checkpoint already exists")
        building.rename(final)
        self.memo.pop(cell.cell_id, None)
        self.record_step(cell, "finalize", time.perf_counter() - wall, time.process_time() - cpu)

    # driver -----------------------------------------------------------------
    def advance(self, cell) -> str:
        final, building, scratch = self.paths(cell)
        if (final / "completion.json").exists():
            return "COMPLETED"
        prepare_path = building / "prepare.json"
        if not prepare_path.exists():
            if not (scratch / "prepare_pending.json").exists():
                while True:
                    step = self.compute_next_step(cell, building, scratch)
                    if step is None:
                        break
                    if not self.affordable(self.compute_estimate(cell, step)):
                        self.persist_for_defer(cell, step)
                        return "DEFERRED"
                    try:
                        self.compute_step(cell, building, scratch, step)
                    except LookupError:
                        # 이전 호출이 보존하지 않은 중간 객체 → compute를 처음부터 다시 한다.
                        shutil.rmtree(self.work_dir(cell))
                        self.work_memo.pop(cell.cell_id, None)
                        continue
                    if step == "write":
                        break
            pending_bytes = sum(p.stat().st_size for p in scratch.rglob("*") if p.is_file())
            copied = 0
            state = building / ".publish_state.json"
            if state.exists():
                copied = sum((scratch / name).stat().st_size for name in read_json(state)["done"])
            estimate = (pending_bytes - copied) / PUBLISH_BYTES_PER_SECOND
            if not self.affordable(min(estimate, 30.0)):
                return "DEFERRED"
            if not self.publish(cell, building, scratch):
                return "DEFERRED"
        prepare = read_json(prepare_path)
        size = prepare["part_size"]
        part_count = math.ceil(EXPECTED_CANDIDATES / size)
        for index in range(part_count):
            if (building / "parts" / f"part-{index:03d}.meta.json").exists():
                continue
            count = len(self.ctx.family.candidates[index * size:(index + 1) * size])
            estimate = cell.event_count * count * SWEEP_SECONDS_PER_EVENT_CANDIDATE
            if cell.cell_id not in self.memo:
                estimate += cell.event_count * 0.00012
            if not self.affordable(estimate):
                return "DEFERRED"
            self.sweep_part(cell, building, prepare, index)
        if not self.affordable(2.0 + cell.event_count * 0.00002):
            return "DEFERRED"
        self.finalize(cell, final, building, prepare, part_count)
        return "COMPLETED"


def load_progress(ctx: Context) -> dict:
    path = ctx.run_root / "progress.json"
    if path.exists():
        return read_json(path)
    return {
        "schema": "mwfd_04_progress_v1", "run_id": ctx.manifest["run_id"], "started_at": now(),
        "total_cells": EXPECTED_CELLS, "expected_work_units": EXPECTED_CELLS * EXPECTED_CANDIDATES,
        "first_incomplete_index": 1, "completed_cells": 0, "completed_candidate_cells": 0,
        "failures": 0, "invocations": 0, "active_process_seconds": 0.0, "status": "RUNNING",
    }


def write_progress(ctx: Context, progress: dict, recent: list) -> None:
    progress["updated_at"] = now()
    progress["checkpoint_timestamp"] = progress["updated_at"]
    if recent:
        wall = sum(step["wall_seconds"] for step in recent)
        progress["recent_throughput"] = {"steps": len(recent), "wall_seconds": wall}
    started = datetime.fromisoformat(progress["started_at"])
    elapsed = (datetime.now(KST) - started).total_seconds()
    progress["elapsed_seconds"] = elapsed
    done = progress["completed_candidate_cells"]
    if done:
        rate = done / max(progress["active_process_seconds"], 1e-9)
        progress["eta_seconds_active_reference"] = (progress["expected_work_units"] - done) / rate
        progress["eta_note"] = "observed active-process rate; reference only, not used for correctness"
    write_json_atomic(ctx.run_root / "progress.json", progress)


def command_run(args, run_root: Path, root: Path) -> int:
    started_wall, started_cpu = time.perf_counter(), time.process_time()
    deadline = started_wall + args.budget_seconds
    tracemalloc.start()
    ctx = Context(run_root, root)
    startup = time.perf_counter() - started_wall
    ctx.checkpoints.mkdir(exist_ok=True)
    ctx.failures.mkdir(exist_ok=True)
    progress = load_progress(ctx)
    if progress.get("status") == "COMPLETE_ALL_CELLS":
        print(json.dumps({"status": "COMPLETE_ALL_CELLS"}))
        return 0
    runner = CellRunner(ctx, Path(args.scratch_dir), deadline)
    consecutive_failures = 0
    last_progress_write = time.perf_counter()
    stopped_reason = "BUDGET"
    completed_now = 0
    first_incomplete = None
    for cell in ctx.population.cells[progress["first_incomplete_index"] - 1:]:
        final, building, scratch = runner.paths(cell)
        if (final / "completion.json").exists():
            continue
        failure = ctx.failures / f"{checkpoint_name(cell)}.json"
        if failure.exists() and not args.retry_failures:
            if first_incomplete is None:
                first_incomplete = cell.index
            continue
        try:
            status = runner.advance(cell)
            consecutive_failures = 0
        except Exception as exc:  # 개별 셀 오류: 기록 후 다음 독립 셀로 진행
            consecutive_failures += 1
            if failure.exists():
                failure = ctx.failures / f"{checkpoint_name(cell)}-{uuid4().hex[:8]}.json"
            write_json_create(failure, {
                "schema": "mwfd_04_cell_failure_v1", "cell": cell.to_dict(), "at": now(),
                "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(),
                "code_revision": ctx.manifest["code_revision"],
            })
            for path in (building, scratch):
                if path.exists():
                    shutil.rmtree(path)
            runner.memo.clear()
            progress["failures"] = len(list(ctx.failures.glob("*.json")))
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                stopped_reason = "SYSTEMIC_FAILURE_STOP"
                break
            if first_incomplete is None:
                first_incomplete = cell.index
            continue
        if status == "COMPLETED":
            completed_now += 1
            progress["completed_cells"] += 1
            progress["completed_candidate_cells"] += EXPECTED_CANDIDATES
            progress["current_cell_index"] = cell.index
        else:
            if first_incomplete is None:
                first_incomplete = cell.index
            progress["current_cell_index"] = cell.index
            break
        if time.perf_counter() - last_progress_write > 60:
            progress["active_process_seconds_partial"] = time.perf_counter() - started_wall
            write_progress(ctx, progress, runner.steps[-10:])
            last_progress_write = time.perf_counter()
    else:
        stopped_reason = "REACHED_END"
    if first_incomplete is None:
        first_incomplete = EXPECTED_CELLS + 1 if stopped_reason == "REACHED_END" else progress["first_incomplete_index"]
    progress["first_incomplete_index"] = first_incomplete
    progress["invocations"] += 1
    wall = time.perf_counter() - started_wall
    progress["active_process_seconds"] += wall
    progress.pop("active_process_seconds_partial", None)
    progress["output_bytes_checkpoints_estimate"] = None
    progress["failures"] = len(list(ctx.failures.glob("*.json")))
    completed_total = sum(1 for cell in ctx.population.cells if (runner.paths(cell)[0] / "completion.json").exists()) \
        if stopped_reason == "REACHED_END" else progress["completed_cells"]
    progress["completed_cells"] = completed_total
    progress["completed_candidate_cells"] = completed_total * EXPECTED_CANDIDATES
    if stopped_reason == "REACHED_END" and completed_total == EXPECTED_CELLS:
        progress["status"] = "COMPLETE_ALL_CELLS"
    elif stopped_reason == "SYSTEMIC_FAILURE_STOP":
        progress["status"] = "STOPPED_SYSTEMIC_FAILURE"
    elif stopped_reason == "REACHED_END":
        progress["status"] = "END_REACHED_WITH_FAILURES"
    write_progress(ctx, progress, runner.steps)
    _, trace_peak = tracemalloc.get_traced_memory()
    log = {
        "at": now(), "wall_seconds": wall, "cpu_seconds": time.process_time() - started_cpu,
        "startup_seconds": startup, "stopped_reason": stopped_reason, "cells_completed": completed_now,
        "steps": runner.steps, "tracemalloc_peak_bytes": trace_peak, "process_peak_rss_bytes": peak_rss_bytes(),
        "first_incomplete_index": first_incomplete,
    }
    with (run_root / "run_log.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(log, sort_keys=True) + "\n")
    print(json.dumps({k: log[k] for k in ("wall_seconds", "stopped_reason", "cells_completed", "first_incomplete_index")}
                     | {"completed_cells": progress["completed_cells"], "status": progress["status"],
                        "failures": progress["failures"], "last_steps": [(s["cell_index"], s["stage"], round(s["wall_seconds"], 1)) for s in runner.steps[-4:]]}))
    return 0 if stopped_reason != "SYSTEMIC_FAILURE_STOP" else 3


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="MWFD-04 1,286-cell full Fast run")
    value.add_argument("command", choices=["run"])
    value.add_argument("--run-root", required=True)
    value.add_argument("--total-stock-root", required=True)
    value.add_argument("--scratch-dir", required=True)
    value.add_argument("--budget-seconds", type=float, default=145.0)
    value.add_argument("--retry-failures", action="store_true")
    return value


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    run_root = Path(args.run_root).absolute()
    root = Path(args.total_stock_root).resolve(strict=True)
    return command_run(args, run_root, root)


if __name__ == "__main__":
    raise SystemExit(main())
