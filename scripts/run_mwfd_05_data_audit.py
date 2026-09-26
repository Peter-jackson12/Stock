"""MWFD-05 Stage 0 DATA AUDIT runner (read-only on the MWFD-04 run root).

usage:
    uv run python scripts/run_mwfd_05_data_audit.py \
        --mwfd04-run C:\\Projects\\TotalStock\\_data\\mwfd_04\\20260926T084815+0900-1286-cell-full-run \
        --output-root C:\\Projects\\TotalStock\\_data\\mwfd_05

새 create-only run 디렉터리에 DATA_AUDIT.md, data_audit.json, schema_binding.json,
factor_support_registry.json, analysis_population_binding.json 을 쓴다.
MWFD-04 파일은 열기만 하고 쓰지 않는다. 연관 분석은 하지 않는다.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research.mwfd05 import data_audit as audit  # noqa: E402

KST = timezone(timedelta(hours=9))
SMALL_INPUTS = ("run_manifest.json", "factor_dataset_manifest.json", "artifact_manifest.json", "combine.json",
                "full_run_summary.json", "gate_summary.json", "crosscheck.json", "resume_validation.json",
                "candidate_summary.jsonl", "cell_results.jsonl", "cell_factors.jsonl",
                "candidate_cell_results.parquet")
LARGE_INPUTS = ("candidate_cell_results.jsonl", "trades.jsonl")
PARQUET_COLUMNS = ("cell_index", "cell_id", "parameter_identity", "execution_status", "screening_status",
                   "ending_position", "ending_open_order", "completed_trades", "entry_signals", "fills",
                   "fast_net_result", "ending_equity", "ending_equity_status", "no_trade_status", "venue",
                   "fast_gross_result", "ending_cash", "fee", "max_adverse_pct")


def git_revision() -> dict:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT_ROOT, capture_output=True, text=True,
                           check=True).stdout.strip()
    code = {name: audit.sha256_stream(PROJECT_ROOT / name) for name in (
        "research/mwfd05/data_audit.py", "scripts/run_mwfd_05_data_audit.py")}
    return {"commit": head, "clean": not dirty, "code_sha256": code}


def write_create_only(path: Path, value) -> None:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def load_small(run: Path) -> dict:
    data = {}
    for name in SMALL_INPUTS:
        if name.endswith(".json"):
            data[name] = json.loads((run / name).read_text(encoding="utf-8"))
        elif name.endswith(".jsonl"):
            data[name] = [json.loads(line) for line in (run / name).read_text(encoding="utf-8").splitlines()]
    return data


def audit_tables(small: dict) -> dict:
    """candidate / cell 단위 key·identity 대조."""
    manifest = small["run_manifest.json"]
    order = manifest["candidate_family"]["candidate_order"]
    population = manifest["population"]["cells"]
    summary = small["candidate_summary.jsonl"]
    cells = small["cell_results.jsonl"]
    factors = small["cell_factors.jsonl"]
    result = {
        "candidate": {
            "manifest_order_count": len(order), "manifest_order_unique": len(set(order)),
            "manifest_order_sorted": order == sorted(order),
            "candidate_summary_rows": len(summary),
            "candidate_summary_matches_frozen_order": [r["parameter_identity"] for r in summary] == order,
            "unique_candidate_count_field": manifest["candidate_family"]["unique_candidate_count"],
            "source_record_count": manifest["candidate_family"]["source_record_count"],
        },
        "cell": {
            "population_cells": len(population),
            "cell_results_rows": len(cells), "cell_factors_rows": len(factors),
            "cell_results_unique": len({c["cell"]["cell_id"] for c in cells}),
            "cell_factors_unique": len({c["cell_id"] for c in factors}),
            "cell_results_eq_cell_factors_ordered": [c["cell"]["cell_id"] for c in cells] == [c["cell_id"] for c in factors],
            "cell_results_eq_population_ordered": [c["cell"]["cell_id"] for c in cells] == [c["cell_id"] for c in population],
            "cell_index_contiguous": [c["cell"]["index"] for c in cells] == list(range(1, len(cells) + 1)),
            "cell_status": dict(Counter(c["status"] for c in cells)),
            "cell_candidate_count": dict(Counter(c["candidate_count"] for c in cells)),
            "venue": dict(Counter(c["venue"] for c in factors)),
            "daily_metadata": dict(Counter(json.dumps(c["factors"]["daily_metadata"]) for c in factors)),
            "tier": dict(Counter(c["cell"]["tier"] for c in cells)),
        },
    }
    return result


def audit_candidate_cells(run: Path, small: dict, expected_sha: str) -> tuple[dict, dict]:
    """Parquet(projection)으로 계약을 검사하고, JSONL 을 streaming 해 sha256·행 동일성을 대조한다."""
    order = small["run_manifest.json"]["candidate_family"]["candidate_order"]
    rank = {pid: i for i, pid in enumerate(order)}
    n_cand = len(order)
    n_cells = len(small["cell_results.jsonl"])
    total = n_cells * n_cand
    parquet = pq.ParquetFile(run / "candidate_cell_results.parquet")
    arrays = {
        "entry_signals": np.full(total, -1, dtype=np.int64),
        "completed_trades": np.full(total, -1, dtype=np.int64),
        "ending_position": np.full(total, -1, dtype=np.int64),
        "ending_open_order": np.zeros(total, dtype=bool),
        "fast_net_result": np.full(total, np.nan),
        "state": np.full(total, -1, dtype=np.int8),
    }
    seen = np.zeros(total, dtype=bool)
    states, issues, no_trade_status = Counter(), Counter(), Counter()
    state_x_status = Counter()
    duplicates = order_violations = 0
    previous = None
    for batch in parquet.iter_batches(columns=list(PARQUET_COLUMNS), batch_size=65536):
        for row in batch.to_pylist():
            key = (row["cell_index"], rank[row["parameter_identity"]])
            if previous is not None and key <= previous:
                order_violations += 1
            previous = key
            index = (row["cell_index"] - 1) * n_cand + key[1]
            if seen[index]:
                duplicates += 1
            seen[index] = True
            state = audit.classify_candidate_cell(row)
            states[state] += 1
            no_trade_status[row["no_trade_status"]] += 1
            state_x_status[f"{state}|{row['no_trade_status']}"] += 1
            for issue in audit.candidate_cell_contract_issues(row):
                issues[issue] += 1
            for name in ("entry_signals", "completed_trades", "ending_position", "ending_open_order"):
                arrays[name][index] = row[name]
            arrays["fast_net_result"][index] = np.nan if row["fast_net_result"] is None else row["fast_net_result"]
            arrays["state"][index] = audit.STATES.index(state)
    parquet_rows = int(seen.sum())

    # JSONL streaming: sha256 + Parquet 과 같은 행·같은 값인지 (projection 컬럼만 비교)
    digest = hashlib.sha256()
    mismatches = rows = 0
    started = time.perf_counter()
    for row in audit.iter_jsonl(run / "candidate_cell_results.jsonl", digest):
        index = (row["cell_index"] - 1) * n_cand + rank[row["parameter_identity"]]
        expected = (arrays["entry_signals"][index], arrays["completed_trades"][index],
                    arrays["ending_position"][index], bool(arrays["ending_open_order"][index]))
        actual = (row["entry_signals"], row["completed_trades"], row["ending_position"], row["ending_open_order"])
        net = arrays["fast_net_result"][index]
        same_net = (row["fast_net_result"] is None and np.isnan(net)) or (
            row["fast_net_result"] is not None and row["fast_net_result"] == net)
        if expected != actual or not same_net or audit.classify_candidate_cell(row) != audit.STATES[arrays["state"][index]]:
            mismatches += 1
        rows += 1
    result = {
        "expected_rows": total, "parquet_rows": parquet.metadata.num_rows, "parquet_keys_seen": parquet_rows,
        "missing_candidate_cells": int(total - parquet_rows), "duplicate_keys": duplicates,
        "key_order_violations": order_violations,
        "jsonl_rows": rows, "jsonl_sha256": digest.hexdigest(), "jsonl_sha256_expected": expected_sha,
        "jsonl_sha256_match": digest.hexdigest() == expected_sha,
        "jsonl_vs_parquet_row_mismatches": mismatches,
        "jsonl_stream_seconds": round(time.perf_counter() - started, 1),
        "state_counts": {state: states.get(state, 0) for state in audit.STATES},
        "no_trade_status_counts": dict(no_trade_status),
        "state_by_no_trade_status": dict(sorted(state_x_status.items())),
        "contract_issues": dict(issues),
    }
    return result, arrays


def audit_trades(run: Path, small: dict, arrays: dict, expected_sha: str) -> dict:
    manifest = small["run_manifest.json"]
    account = manifest["account"]
    gate = small["gate_summary.json"]
    order = manifest["candidate_family"]["candidate_order"]
    rank = {pid: i for i, pid in enumerate(order)}
    n_cand = len(order)
    session_start = 32400
    digest = hashlib.sha256()
    status, resolution, issues, reconcile = Counter(), Counter(), Counter(), Counter()
    order_violations = rows = 0
    previous = None
    current_index, acc = None, None
    seq_min = seq_max = None
    same_ns = Counter()
    touched = np.zeros(len(arrays["state"]), dtype=bool)
    pre_age_max = 0
    notional_min = None
    decision_state = Counter()

    def close(index, acc):
        expected = {name: (bool(arrays[name][index]) if name == "ending_open_order" else
                           (None if name == "fast_net_result" and np.isnan(arrays[name][index])
                            else arrays[name][index].item()))
                    for name in ("entry_signals", "completed_trades", "ending_position", "ending_open_order",
                                 "fast_net_result")}
        for issue in audit.reconcile_key(acc, expected):
            reconcile[issue] += 1

    started = time.perf_counter()
    for trade in audit.iter_jsonl(run / "trades.jsonl", digest):
        rows += 1
        key = (trade["cell_index"], rank[trade["parameter_identity"]], trade["trade_index"])
        if previous is not None and key <= previous:
            order_violations += 1
        previous = key
        index = (trade["cell_index"] - 1) * n_cand + key[1]
        if index != current_index:
            if acc is not None:
                close(current_index, acc)
            if touched[index]:
                order_violations += 1
            touched[index] = True
            current_index, acc = index, audit.KeyAccumulator()
        acc.add(trade)
        status[trade["status"]] += 1
        resolution[trade["decision_row_resolution"]] += 1
        same_ns[trade["same_ns_trade_rows"]] += 1
        decision_state[audit.STATES[arrays["state"][index]]] += 1
        for issue in audit.trade_contract_issues(
                trade, buy_latency_ns=account["buy_latency_ns"], sell_latency_ns=account["sell_latency_ns"],
                max_quote_age_ns=account["max_quote_age_ns"],
                gate_threshold_krw=gate["threshold_ask10_notional_krw"], session_start_second=session_start):
            issues[issue] += 1
        pre = trade.get("pre_entry")
        if pre:
            seq = pre["seq"]
            seq_min = seq if seq_min is None else min(seq_min, seq)
            seq_max = seq if seq_max is None else max(seq_max, seq)
            pre_age_max = max(pre_age_max, pre["quote_age_ns"] or 0)
            if pre["ask_depth_notional_10"] is not None:
                notional_min = pre["ask_depth_notional_10"] if notional_min is None else min(
                    notional_min, pre["ask_depth_notional_10"])
    if acc is not None:
        close(current_index, acc)
    # 거래 행이 없는 candidate-cell 은 entry_signals 가 0 이어야 한다.
    untouched_with_signals = int(((~touched) & (arrays["entry_signals"] > 0)).sum())
    return {
        "rows": rows, "sha256": digest.hexdigest(), "sha256_expected": expected_sha,
        "sha256_match": digest.hexdigest() == expected_sha,
        "stream_seconds": round(time.perf_counter() - started, 1),
        "key": ["cell_id", "parameter_identity", "trade_index"],
        "key_order_violations": order_violations,
        "key_unique": order_violations == 0,
        "candidate_cells_with_trade_rows": int(touched.sum()),
        "candidate_cells_without_rows_but_entry_signals": untouched_with_signals,
        "entry_signals_total_from_candidate_cells": int(arrays["entry_signals"].sum()),
        "status_counts": dict(status),
        "decision_row_resolution": dict(resolution),
        "same_ns_trade_rows": {str(k): v for k, v in sorted(same_ns.items())},
        "rows_by_parent_candidate_cell_state": dict(decision_state),
        "trade_contract_issues": dict(issues),
        "candidate_cell_reconciliation_issues": dict(reconcile),
        "pre_entry_seq_range": [seq_min, seq_max],
        "pre_entry_quote_age_ns_max": pre_age_max,
        "pre_entry_ask_depth_notional_10_min": notional_min,
    }


FACTOR_REGISTRY = [
    {
        "slot": "F01", "name": "exact ask-side 10-level depth notional",
        "status": "SUPPORTED_DIRECT",
        "source": "trades.pre_entry.ask_depth_notional_10 (decision grain); cell_factors.factors.ask_depth_notional_10 (cell distribution); MWFD-02 execution_depth cache ask_prices/ask_sizes (quote grain)",
        "semantics": "sum(ask_price_i * ask_size_i, i=1..10) from raw FID 41..60 of the latest quote with seq < decision trade seq; null unless all 10 ask levels COMPLETE and strictly ordered (research/fast_backtest/execution_depth.py::_side, feasibility.join_entry_feasibility)",
        "causal_timing": "latest quote strictly before the decision trade in receive order; no future backfill",
        "limitations": [
            "decision grain exists only for signaled entry decisions (trades.jsonl)",
            "every signaled decision required gate PASS, so decision-grain values are truncated at >= 100,000,000 KRW by construction",
        ],
    },
    {
        "slot": "F02", "name": "relative spread",
        "status": "DERIVABLE_CAUSAL_EXISTING_CACHE",
        "source": "pre_entry.bid/ask; feature_cache rows bid/ask",
        "semantics": "existing spread_pct = (ask - bid) / bid on the validated top of book. SAP v1 text is not in the repository; a midpoint-denominated relative spread is derivable from the same bid/ask. Bind SUPPORTED_DIRECT only if the SAP freeze defines the bid denominator.",
        "causal_timing": "latest validated quote strictly before the decision row",
        "limitations": ["denominator must be frozen in SAP schema binding"],
    },
    {
        "slot": "F03", "name": "10-level depth imbalance",
        "status": "DERIVABLE_CAUSAL_EXISTING_CACHE",
        "source": "checkpoints/<cell>/gate_cache/states.jsonl (trade seq -> quote_seq) joined to MWFD-02 execution_depth cache bid/ask 10-level prices and sizes",
        "semantics": "existing obi_ratio = bid3/ask3 (3-level size ratio) is NOT F03. 10-level bid and ask vectors exist per quote; imbalance formula (size vs notional, normalization) must be frozen in SAP.",
        "causal_timing": "quote_seq recorded by the gate join is the latest quote with seq < trade seq",
        "limitations": ["requires a new join/materialization pass over gate cache + MWFD-02 depth cache", "bid side completeness must be required separately (ask-only completeness drives the gate)"],
    },
    {
        "slot": "F04", "name": "recent 30s buy/sell imbalance",
        "status": "DERIVABLE_CAUSAL_EXISTING_CACHE",
        "source": "checkpoints/<cell>/feature_cache/features.jsonl trade rows (received_ns, trade_volume, is_buy)",
        "semantics": "existing buy_ratio_by_ticks['15'] is a 15-trade volume share (None if any unknown direction), NOT a 30-second window. A 30s window is derivable from trade rows.",
        "causal_timing": "rows are in receive order; window end convention (inclusive/exclusive of the decision trade) must be frozen in SAP",
        "limitations": ["unknown-direction handling must be frozen", "window truncated near prefix start (~08:30 KST)"],
    },
    {
        "slot": "F05", "name": "recent 30s traded notional",
        "status": "DERIVABLE_CAUSAL_EXISTING_CACHE",
        "source": "feature_cache trade rows (received_ns, trade_price, trade_volume)",
        "semantics": "existing recent_volume_by_ticks['15'] is 15-trade share volume, NOT 30s notional. sum(price*volume) over 30s is derivable.",
        "causal_timing": "same as F04",
        "limitations": ["window end convention must be frozen"],
    },
    {
        "slot": "F06", "name": "30s midpoint return",
        "status": "DERIVABLE_CAUSAL_EXISTING_CACHE",
        "source": "feature_cache rows bid/ask/quote_eligible/quote_received_ns",
        "semantics": "no midpoint field exists; prior_high/breakout_margin_pct is NOT F06. midpoint at t and t-30s is derivable from validated top-of-book on each event row.",
        "causal_timing": "use the latest validated row with received_ns <= t-30s; staleness rule must be frozen",
        "limitations": ["08:30-09:00 KST rows are pre-open auction quotes; midpoint semantics differ", "window truncated near prefix start"],
    },
    {
        "slot": "F07", "name": "recent 30s midpoint realized volatility",
        "status": "DERIVABLE_CAUSAL_EXISTING_CACHE",
        "source": "feature_cache rows bid/ask",
        "semantics": "no volatility field exists. realized volatility of midpoint changes over 30s is derivable; sampling (event vs clock) must be frozen.",
        "causal_timing": "same as F06",
        "limitations": ["sampling convention and minimum observation count must be frozen", "pre-open auction rows"],
    },
]


def population_binding(cc: dict, trades: dict) -> dict:
    states = cc["state_counts"]
    return {
        "candidate_cell_states": {
            audit.NO_TRADE_FLAT: {"count": states[audit.NO_TRADE_FLAT],
                                  "definition": "EVALUATED, ending_position == 0, completed_trades == 0, ending_open_order == false; fast_net_result == 0 exactly, fills == 0, entry_signals == 0"},
            audit.NO_TRADE_OPEN_ORDER: {"count": states[audit.NO_TRADE_OPEN_ORDER],
                                        "definition": "ending_position == 0, completed_trades == 0, ending_open_order == true (entry decision whose buy never filled before cutoff); fast_net_result == 0"},
            audit.TRADED_FLAT: {"count": states[audit.TRADED_FLAT],
                                "definition": "ending_position == 0, completed_trades >= 1, ending_open_order == false; fast_net_result finite == sum(completed trade net_pnl)"},
            audit.TRADED_FLAT_OPEN_ORDER: {"count": states[audit.TRADED_FLAT_OPEN_ORDER],
                                           "definition": "ending_position == 0, completed_trades >= 1, ending_open_order == true (a later buy decision never filled); realized net finite but an unresolved order remains"},
            audit.OPEN_POSITION: {"count": states[audit.OPEN_POSITION],
                                  "definition": "ending_position != 0; fast_net_result == null, ending_equity == null, ending_equity_status == OPEN_POSITION_UNMARKED (no terminal mark)"},
            audit.ERROR_INCOMPLETE: {"count": states[audit.ERROR_INCOMPLETE],
                                     "definition": "execution_status != EVALUATED or unknown screening_status; economic result null"},
        },
        "populations": {
            "A_participation": {"grain": "cell x candidate", "members": "all EVALUATED candidate-cells (all states except ERROR_INCOMPLETE)",
                                "count": sum(states.values()) - states[audit.ERROR_INCOMPLETE],
                                "outcomes": ["entry_signals", "completed_trades", "no_trade/terminal state"],
                                "note": "no-trade is information here; economic values are not the outcome of this population"},
            "B_conditional_candidate_cell_economics": {"grain": "cell x candidate", "members": audit.TRADED_FLAT,
                                                       "count": states[audit.TRADED_FLAT],
                                                       "outcome": "fast_net_result (KRW, quantity 1)",
                                                       "excluded": [audit.NO_TRADE_FLAT, audit.NO_TRADE_OPEN_ORDER, audit.TRADED_FLAT_OPEN_ORDER, audit.OPEN_POSITION]},
            "C_completed_trade_economics": {"grain": "cell x candidate x entry decision", "members": "trades.status == COMPLETED",
                                            "count": trades["status_counts"].get("COMPLETED", 0),
                                            "outcome": "net_pnl (KRW, quantity 1)",
                                            "factors": "pre_entry (at-decision, includes the triggering trade row)",
                                            "note": "includes completed episodes whose parent candidate-cell later ended OPEN_POSITION; parent state must be carried as a covariate/stratum, not silently mixed"},
            "D_censoring": {"grain": "cell x candidate (+ terminal trade rows)",
                            "members": f"{audit.OPEN_POSITION} candidate-cells; trades.status in (OPEN_POSITION, ENTRY_UNFILLED)",
                            "count_candidate_cells": states[audit.OPEN_POSITION],
                            "count_trade_rows": {k: trades["status_counts"].get(k, 0) for k in ("OPEN_POSITION", "ENTRY_UNFILLED")},
                            "economics": "CENSORED / NOT_ESTIMABLE (no terminal mark)"},
        },
    }


def render_markdown(result: dict) -> str:
    cc, tr, tb = result["candidate_cell"], result["trades"], result["tables"]
    states = result["population_binding"]["candidate_cell_states"]
    lines = [
        "# MWFD-05 Stage 0 — DATA AUDIT",
        "",
        f"- audit run: `{result['provenance']['audit_run_dir']}`",
        f"- audit timestamp: {result['provenance']['audit_timestamp']}",
        f"- master: `{result['provenance']['master_sha']}` / analysis code: `{result['provenance']['analysis_code']['commit']}` (clean={result['provenance']['analysis_code']['clean']})",
        f"- MWFD-04 run: `{result['provenance']['mwfd04_run_id']}` (read-only)",
        "",
        "Descriptive contract audit only. No factor↔outcome association, binning, correlation, regression, bootstrap, ranking, threshold or hypothesis promotion.",
        "",
        "## Verdict",
        "",
        f"**{result['verdict']['overall']}**",
        "",
        "| item | status |",
        "|---|---|",
    ]
    for key, value in result["verdict"]["modules"].items():
        lines.append(f"| {key} | {value} |")
    lines += ["", "## Candidate-cell states", "", "| state | count | definition |", "|---|---:|---|"]
    for name, item in states.items():
        lines.append(f"| {name} | {item['count']:,} | {item['definition']} |")
    lines += ["", "## Key / reconciliation", "",
              f"- candidate-cell rows {cc['parquet_rows']:,} / expected {cc['expected_rows']:,}; missing {cc['missing_candidate_cells']}, duplicate {cc['duplicate_keys']}, key order violations {cc['key_order_violations']}",
              f"- JSONL sha256 match {cc['jsonl_sha256_match']}; JSONL vs Parquet row mismatches {cc['jsonl_vs_parquet_row_mismatches']}; contract issues {cc['contract_issues'] or 'none'}",
              f"- cells: results {tb['cell']['cell_results_rows']}, factors {tb['cell']['cell_factors_rows']}, ordered identity equal {tb['cell']['cell_results_eq_cell_factors_ordered']}",
              f"- candidates: {tb['candidate']['manifest_order_count']} unique {tb['candidate']['manifest_order_unique']}, summary matches frozen order {tb['candidate']['candidate_summary_matches_frozen_order']}",
              f"- trades rows {tr['rows']:,}, sha256 match {tr['sha256_match']}, key unique {tr['key_unique']}, status {tr['status_counts']}",
              f"- trade contract issues {tr['trade_contract_issues'] or 'none'}; candidate-cell reconciliation issues {tr['candidate_cell_reconciliation_issues'] or 'none'}",
              "", "## Factor support", "", "| slot | name | status |", "|---|---|---|"]
    for item in result["factor_registry"]:
        lines.append(f"| {item['slot']} | {item['name']} | {item['status']} |")
    lines += ["", "## Blocking issues before main analysis", ""]
    lines += [f"{i}. {text}" for i, text in enumerate(result["blocking_issues"], 1)]
    lines += ["", "Details: data_audit.json, schema_binding.json, factor_support_registry.json, analysis_population_binding.json", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mwfd04-run", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--master-sha", required=True)
    args = parser.parse_args()
    run = args.mwfd04_run.resolve()
    code = git_revision()
    if not code["clean"]:
        raise SystemExit("analysis code checkout must be clean (commit the audit code first)")
    now = datetime.now(KST)
    out = args.output_root.resolve() / f"{now.strftime('%Y%m%dT%H%M%S%z')}-stage0-data-audit"
    out.mkdir(parents=True, exist_ok=False)

    artifact = json.loads((run / "artifact_manifest.json").read_text(encoding="utf-8"))
    expected = {entry["path"]: entry["sha256"] for entry in artifact["entries"]}
    small = load_small(run)
    inputs = {}
    for name in SMALL_INPUTS:
        identity = audit.file_identity(run / name)
        identity["artifact_manifest_sha256"] = expected.get(name)
        identity["matches_artifact_manifest"] = expected.get(name) in (None, identity["sha256"]) if name != "artifact_manifest.json" else None
        inputs[name] = identity
    tables = audit_tables(small)
    candidate_cell, arrays = audit_candidate_cells(run, small, expected["candidate_cell_results.jsonl"])
    trades = audit_trades(run, small, arrays, expected["trades.jsonl"])
    for name, sha in (("candidate_cell_results.jsonl", candidate_cell["jsonl_sha256"]), ("trades.jsonl", trades["sha256"])):
        identity = audit.file_identity(run / name, sha256=sha)
        identity["artifact_manifest_sha256"] = expected[name]
        identity["matches_artifact_manifest"] = sha == expected[name]
        inputs[name] = identity

    binding = population_binding(candidate_cell, trades)
    integrity_ok = all([
        all(v["matches_artifact_manifest"] in (True, None) for v in inputs.values()),
        candidate_cell["missing_candidate_cells"] == 0, candidate_cell["duplicate_keys"] == 0,
        candidate_cell["key_order_violations"] == 0, candidate_cell["jsonl_vs_parquet_row_mismatches"] == 0,
        not candidate_cell["contract_issues"], trades["key_unique"], not trades["trade_contract_issues"],
        not trades["candidate_cell_reconciliation_issues"],
        trades["candidate_cells_without_rows_but_entry_signals"] == 0,
        tables["cell"]["cell_results_eq_cell_factors_ordered"],
        tables["candidate"]["candidate_summary_matches_frozen_order"],
    ])
    modules = {
        "Module A (cell-candidate participation)": "SUPPORTED",
        "Module B (all at-risk decisions x risk state x pre-decision factor)": "BLOCKED",
        "Module C (completed-trade conditional)": "SUPPORTED",
        "Open-position economics": "CENSORED",
        **{f"{f['slot']} {f['name']}": f["status"] for f in FACTOR_REGISTRY},
        "delta_E": "UNSET (BLOCKED_ECONOMIC_THRESHOLD_UNSET)",
        "daily metadata (market_cap/trading_value/listed_shares)": "NOT_READY_EXCLUDED",
        "venue": "unknown",
    }
    blocking = [
        "Module B: trades.jsonl records only signaled entry decisions; no-signal decisions, gate-fail decisions and per-candidate flat/open/cooldown risk state at each trade row are not in any ledger. A new at-risk decision ledger materialization (replay over feature/gate caches with candidate state) and an SAP decision are required; trade-only reconstruction is not allowed.",
        "F02-F07 are not stored as defined; each needs a frozen formula (denominator, window end inclusivity, unknown-direction rule, staleness, sampling) and a new causal materialization from existing caches before any analysis. Similar existing fields (spread_pct, obi_ratio bid3/ask3, buy_ratio/recent_volume by 15 ticks, prior_high) must not be substituted.",
        "F01 at decision grain is range-truncated (>= 100,000,000 KRW) because every signaled decision passed the gate; its use in Module C must account for that selection.",
        "Open-position economics is CENSORED: OPEN_POSITION candidate-cells have fast_net_result = null and no terminal mark; flat-only results must not be called policy PnL. Any mark-to-market rule is a separate SAP change.",
        "delta_E is UNSET: PnL is KRW for quantity 1; cost = 0.1% of fill notional per side (~0.20% round trip, no separate tax schedule, no slippage beyond crossing the quote); no capital-return denominator is defined. Economic hypothesis promotion stays blocked.",
        "Dependence: 663 candidates and 852,618 candidate-cells are not independent samples; the discovery set is one date (2026-09-21) bounded prefix ~08:30-10:00 KST with entries only from 09:00 (session_start 32400). SAP must define the clustering unit before inference.",
        "SAP v1 text is not present in the repository; F01-F07 semantics were bound from the control-tower instruction text and must be frozen in the SAP schema binding step.",
        "venue remains unknown and daily market_cap/trading_value/listed_shares remain NOT_READY_EXCLUDED.",
    ]
    result = {
        "schema": audit.SCHEMA_VERSION,
        "provenance": {
            "audit_run_dir": str(out), "audit_timestamp": now.isoformat(), "master_sha": args.master_sha,
            "analysis_code": code, "mwfd04_run_id": artifact["run_id"], "mwfd04_run_root": str(run),
            "mwfd04_artifact_manifest_status": artifact["status"],
            "mwfd04_artifact_manifest_sha256": inputs["artifact_manifest.json"]["sha256"],
            "mwfd04_code_revision": small["run_manifest.json"]["code_revision"],
            "inputs": inputs,
        },
        "tables": tables, "candidate_cell": candidate_cell, "trades": trades,
        "population_binding": binding, "factor_registry": FACTOR_REGISTRY,
        "integrity_ok": integrity_ok,
        "verdict": {"overall": ("AUDIT_PASS_WITH_BLOCKED_MODULES" if integrity_ok else "AUDIT_FAIL"), "modules": modules},
        "blocking_issues": blocking,
    }
    provenance = result["provenance"]
    write_create_only(out / "data_audit.json", result)
    write_create_only(out / "schema_binding.json", {"schema": "mwfd_05_schema_binding_v1", "provenance": provenance,
                                                   "tables": SCHEMA_BINDING})
    write_create_only(out / "factor_support_registry.json", {"schema": "mwfd_05_factor_support_registry_v1",
                                                            "provenance": provenance, "factors": FACTOR_REGISTRY})
    write_create_only(out / "analysis_population_binding.json", {"schema": "mwfd_05_population_binding_v1",
                                                                "provenance": provenance, **binding})
    write_create_only(out / "DATA_AUDIT.md", render_markdown(result))
    print(json.dumps({"out": str(out), "verdict": result["verdict"]["overall"], "integrity_ok": integrity_ok,
                      "states": candidate_cell["state_counts"], "trades": trades["status_counts"]}, ensure_ascii=False))
    return 0 if integrity_ok else 1


SCHEMA_BINDING = {
    "candidate_cell_results": {
        "canonical_file": "candidate_cell_results.jsonl", "analysis_file": "candidate_cell_results.parquet (flattened derivative; candidate_aliases dropped, feasibility -> gate_* columns)",
        "grain": "cell x candidate (incl. no-trade)", "key": ["cell_id", "parameter_identity"], "order": ["cell_index", "parameter_identity"],
        "fields": {
            "entry_signals": "count of entry decisions (all entry conditions incl. gate PASS true while flat, no pending, not cooling down)",
            "submitted_intents": "entry_signals + exit_signals (decision count, not broker submissions)",
            "fills": "buy + sell fills", "completed_trades": "round trips returning position to 0",
            "fast_net_result": "cash - initial cash when ending_position == 0 else null (KRW, quantity 1)",
            "ending_equity_status": "FLAT_CASH | OPEN_POSITION_UNMARKED",
            "ending_open_order": "pending order unfilled at cutoff (orders never expire in Fast)",
            "no_trade_status": "TRADED | OPEN_POSITION | OPEN_ORDER | NO_ENTRY_SIGNAL (TRADED takes precedence even if the cell ends open)",
            "gate_context": "cell-level: GATE_HAS_PASS if the cell had any PASS",
        },
    },
    "trades": {
        "canonical_file": "trades.jsonl", "grain": "cell x candidate x signaled entry decision",
        "key": ["cell_id", "parameter_identity", "trade_index"],
        "content_type": "SIGNALED_ENTRY_DECISIONS (option 2): one row per entry decision that met every entry condition incl. gate PASS; not all at-risk decisions, not fill-only, not completed-only",
        "status": {"COMPLETED": "entry and exit filled", "OPEN_POSITION": "entry filled, no exit fill by cutoff", "ENTRY_UNFILLED": "buy pending at cutoff (last row of its key only)"},
        "decision_id": "pre_entry.seq = collector seq of the decision trade row (unique within the cell source/session)",
        "pre_entry": "values of the decision trade row itself (includes the triggering trade in 15-tick windows) plus gate state from the latest quote with seq < decision seq",
        "generator": "scripts/run_mwfd_04_full.py::trade_records at MWFD-04 code revision 6436255 (unchanged on master)",
    },
    "cell_results": {"grain": "cell", "key": ["cell.cell_id"], "rows": 1286},
    "cell_factors": {"grain": "cell", "key": ["cell_id"], "rows": 1286, "daily_metadata": "NOT_READY_EXCLUDED", "venue": "unknown"},
    "candidate_summary": {"grain": "candidate", "key": ["parameter_identity"], "rows": 663, "order": "frozen manifest order (sorted parameter_identity), not ranked"},
    "per_cell_caches": {
        "feature_cache": "checkpoints/<index>-<code>-unknown/feature_cache/features.jsonl: every event row in receive order (seq, received_ns, kind, trade price/volume/is_buy, validated top of book, 15-tick windows, prior_high 30/60/120s)",
        "gate_cache": "checkpoints/<...>/gate_cache/states.jsonl: per trade seq gate status, quote_seq, ask_depth_notional_10",
        "execution_depth": "MWFD-02 shared cache: per quote seq 10-level ask/bid prices and sizes, notionals, completeness",
    },
}


if __name__ == "__main__":
    raise SystemExit(main())
