"""MWFD-05 Stage 0 — blind binding + DATA AUDIT (descriptive contract checks only).

MWFD-04 산출물을 읽기 전용으로 대조해 table grain, key, 상태 분류, 인과 시점, factor 지원 여부를
기록한다. factor↔outcome 연관 분석, bin, 상관, 회귀, bootstrap, 후보 순위화는 하지 않는다.

대형 파일은 한 번씩 streaming 으로만 읽는다.
  - candidate_cell_results.jsonl: sha256 과 Parquet 파생본의 key·상태 컬럼 행 단위 대조
  - trades.jsonl: sha256, key 단조성(=유일성), 상태/fill/인과 시점 계약, candidate-cell 재조정
메모리는 candidate-cell 수 크기의 정수·실수 배열과 현재 key 누산기뿐이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "mwfd_05_data_audit_v1"

# ── candidate-cell 상태 (상호 배타) ────────────────────────────────────────
NO_TRADE_FLAT = "NO_TRADE_FLAT"
NO_TRADE_OPEN_ORDER = "NO_TRADE_OPEN_ORDER"
TRADED_FLAT = "TRADED_FLAT"
TRADED_FLAT_OPEN_ORDER = "TRADED_FLAT_OPEN_ORDER"
OPEN_POSITION = "OPEN_POSITION"
ERROR_INCOMPLETE = "ERROR_INCOMPLETE"
STATES = (NO_TRADE_FLAT, NO_TRADE_OPEN_ORDER, TRADED_FLAT, TRADED_FLAT_OPEN_ORDER, OPEN_POSITION, ERROR_INCOMPLETE)

TRADE_STATUSES = ("COMPLETED", "OPEN_POSITION", "ENTRY_UNFILLED")
PRE_ENTRY_FIELDS = frozenset({
    "seq", "market_second", "trade_price", "trade_volume", "spread_pct", "bid", "ask", "bid_size",
    "ask_size", "bid3", "ask3", "obi_ratio", "quote_age_ns", "buy_ratio", "recent_volume",
    "unknown_direction", "prior_high", "breakout_margin_pct", "gate_status", "gate_reason",
    "ask_depth_notional_10",
})
FLOAT_TOLERANCE = 1e-6


def finite_or_none(value) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))


def classify_candidate_cell(row: Mapping[str, Any]) -> str:
    """MWFD-04 candidate-cell 행을 경제 결과 모집단 상태로 분류한다.

    근거 계약(scripts/run_mwfd_04_full.py, research/fast_backtest/sweep.py):
      - ending_position != 0 → fast_net_result=None, ending_equity_status=OPEN_POSITION_UNMARKED
      - ending_open_order = 종료 시 미체결 pending 주문(취소 규칙 없음). flat 이면 현금 영향 없음.
      - completed_trades = 포지션이 0으로 돌아온 왕복 수.
    """
    if row.get("execution_status") != "EVALUATED" or row.get("screening_status") not in (
            "COMPLETE_FLAT", "OPEN_POSITION_UNRANKED"):
        return ERROR_INCOMPLETE
    if row["ending_position"] != 0:
        return OPEN_POSITION
    traded = row["completed_trades"] > 0
    if row["ending_open_order"]:
        return TRADED_FLAT_OPEN_ORDER if traded else NO_TRADE_OPEN_ORDER
    return TRADED_FLAT if traded else NO_TRADE_FLAT


def candidate_cell_contract_issues(row: Mapping[str, Any]) -> list[str]:
    """상태별 null/finite 계약 위반 목록(없으면 빈 목록)."""
    issues = []
    state = classify_candidate_cell(row)
    for name in ("fast_net_result", "fast_gross_result", "ending_equity", "ending_cash", "fee", "max_adverse_pct"):
        if not finite_or_none(row.get(name)):
            issues.append(f"nonfinite:{name}")
    if state == ERROR_INCOMPLETE:
        return issues + ["error_incomplete"]
    flat = state != OPEN_POSITION
    if flat != (row["fast_net_result"] is not None):
        issues.append("net_null_contract")
    if flat != (row["ending_equity"] is not None):
        issues.append("equity_null_contract")
    expected_equity_status = "FLAT_CASH" if flat else "OPEN_POSITION_UNMARKED"
    if row.get("ending_equity_status") != expected_equity_status:
        issues.append("equity_status_contract")
    expected_screening = "COMPLETE_FLAT" if flat else "OPEN_POSITION_UNRANKED"
    if row["screening_status"] != expected_screening:
        issues.append("screening_status_contract")
    if state in (NO_TRADE_FLAT, NO_TRADE_OPEN_ORDER):
        if row["fills"] != 0 or row["fast_net_result"] != 0:
            issues.append("no_trade_nonzero")
    if state == NO_TRADE_FLAT and row["entry_signals"] != 0:
        issues.append("no_trade_flat_with_signal")
    return issues


def trade_contract_issues(trade: Mapping[str, Any], *, buy_latency_ns: int, sell_latency_ns: int,
                          max_quote_age_ns: int, gate_threshold_krw: int, session_start_second: int) -> list[str]:
    """trade ledger 한 행의 상태·fill·인과 시점 계약 위반 목록."""
    issues = []
    status = trade.get("status")
    if status not in TRADE_STATUSES:
        return ["unknown_status"]
    entry_fill, exit_fill = trade.get("entry_fill"), trade.get("exit_fill")
    decision = trade["entry_decision_ns"]
    if status == "COMPLETED":
        if entry_fill is None or exit_fill is None or trade.get("net_pnl") is None:
            issues.append("completed_missing_fill_or_pnl")
    elif status == "OPEN_POSITION":
        if entry_fill is None or exit_fill is not None or trade.get("net_pnl") is not None:
            issues.append("open_position_contract")
    elif entry_fill is not None or exit_fill is not None or trade.get("net_pnl") is not None:
        issues.append("entry_unfilled_contract")
    if not finite_or_none(trade.get("net_pnl")) or not finite_or_none(trade.get("gross_pnl")):
        issues.append("nonfinite_pnl")
    # 인과 시점: fill 은 결정 + latency 이후, 청산 결정은 진입 fill 이후.
    if entry_fill is not None and entry_fill["time_ns"] < decision + buy_latency_ns:
        issues.append("entry_fill_before_latency")
    if trade.get("exit_decision_ns") is not None:
        if entry_fill is None or trade["exit_decision_ns"] < entry_fill["time_ns"]:
            issues.append("exit_decision_before_entry_fill")
    if exit_fill is not None and exit_fill["time_ns"] < trade["exit_decision_ns"] + sell_latency_ns:
        issues.append("exit_fill_before_latency")
    pre = trade.get("pre_entry")
    if trade.get("decision_row_resolution") != "UNIQUE_TRADE_ROW":
        issues.append("non_unique_decision_row")
    if pre is None:
        issues.append("pre_entry_missing")
        return issues
    if set(pre) != PRE_ENTRY_FIELDS:
        issues.append("pre_entry_field_set")
    if pre.get("gate_status") != "PASS":
        issues.append("decision_gate_not_pass")
    notional = pre.get("ask_depth_notional_10")
    if notional is None or notional < gate_threshold_krw:
        issues.append("decision_depth_below_gate")
    age = pre.get("quote_age_ns")
    if age is None or not 0 <= age <= max_quote_age_ns:
        issues.append("decision_quote_not_fresh")
    if pre.get("market_second") is None or pre["market_second"] < session_start_second:
        issues.append("decision_before_session_start")
    # pre_entry 는 결정 행 자체의 값만 담아야 한다(미래 fill/exit 값이 섞이면 안 된다).
    for key in ("exit_fill", "entry_fill", "net_pnl", "holding_ns", "exit_decision_ns"):
        if key in pre:
            issues.append(f"pre_entry_contains_{key}")
    return issues


@dataclass
class KeyAccumulator:
    """(cell, candidate) 하나의 trade ledger 누산기."""
    rows: int = 0
    completed: int = 0
    open_position: int = 0
    entry_unfilled: int = 0
    completed_net: float = 0.0
    last_index: int = -1
    index_gap: bool = False
    unfilled_not_last: bool = False
    statuses: list[str] = field(default_factory=list)

    def add(self, trade: Mapping[str, Any]) -> None:
        if trade["trade_index"] != self.last_index + 1:
            self.index_gap = True
        self.last_index = trade["trade_index"]
        # 종료 시점 상태(OPEN_POSITION/ENTRY_UNFILLED)는 그 key 의 마지막 행이어야 한다.
        if self.entry_unfilled or self.open_position:
            self.unfilled_not_last = True
        self.rows += 1
        status = trade["status"]
        if status == "COMPLETED":
            self.completed += 1
            self.completed_net += trade["net_pnl"]
        elif status == "OPEN_POSITION":
            self.open_position += 1
        else:
            self.entry_unfilled += 1


def reconcile_key(acc: KeyAccumulator, expected: Mapping[str, Any]) -> list[str]:
    """trade ledger 누산 결과를 candidate-cell 행과 대조한다."""
    issues = []
    if acc.rows != expected["entry_signals"]:
        issues.append("rows_ne_entry_signals")
    if acc.completed != expected["completed_trades"]:
        issues.append("completed_ne_candidate_cell")
    if acc.index_gap:
        issues.append("trade_index_gap")
    if acc.unfilled_not_last or acc.open_position > 1 or acc.entry_unfilled > 1:
        issues.append("terminal_episode_not_last")
    if (acc.open_position == 1) != (expected["ending_position"] != 0):
        issues.append("open_position_mismatch")
    if acc.entry_unfilled == 1 and not expected["ending_open_order"]:
        issues.append("unfilled_without_open_order")
    net = expected.get("fast_net_result")
    if net is not None and abs(acc.completed_net - net) > FLOAT_TOLERANCE * max(1.0, abs(net)):
        issues.append("completed_net_ne_fast_net")
    return issues


def sha256_stream(path: Path, chunk: int = 16 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def file_identity(path: Path, *, sha256: str | None = None) -> dict[str, Any]:
    stat = path.stat()
    return {"path": str(path), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "sha256": sha256 if sha256 is not None else sha256_stream(path)}


def iter_jsonl(path: Path, digest=None) -> Iterable[dict]:
    with path.open("rb") as stream:
        for line in stream:
            if digest is not None:
                digest.update(line)
            yield json.loads(line)
