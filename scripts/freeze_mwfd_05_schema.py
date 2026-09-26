"""MWFD-05 Statistical Analysis Plan — schema binding freeze writer.

Writes a create-only schema-freeze run next to the Stage 0 audit. It reads MWFD-04 and
Stage 0 artifacts only to record identity, and checks the fee-included net semantics on a
bounded prefix of trades.jsonl (contract pass/fail counts only; no return values or
distributions are computed or emitted).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from research.mwfd05 import data_audit as audit  # noqa: E402
from research.mwfd05 import economic_outcomes as eo  # noqa: E402
from research.mwfd05 import factor_contract as fc  # noqa: E402

KST = timezone(timedelta(hours=9))
FEE_CHECK_ROWS = 20_000
CONTRACT_FILES = ("research/mwfd05/economic_outcomes.py", "research/mwfd05/factor_contract.py",
                  "scripts/freeze_mwfd_05_schema.py", "tests/test_mwfd_05_schema_freeze.py")


def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True).stdout.strip()


def write_x(path: Path, value) -> None:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def bounded_fee_check(trades_path: Path, rows: int) -> dict:
    """First `rows` lines only. Counts contract pass/fail; never returns outcome values."""
    checked = passed = 0
    failures: dict[str, int] = {}
    with trades_path.open("rb") as stream:
        for index, line in enumerate(stream):
            if index >= rows:
                break
            trade = json.loads(line)
            if trade["status"] != "COMPLETED":
                continue
            checked += 1
            try:
                eo.trade_outcome(trade)
                passed += 1
            except eo.OutcomeContractError as exc:
                failures[str(exc)] = failures.get(str(exc), 0) + 1
    return {"scope": f"first {rows} lines of trades.jsonl (bounded; not a full scan)", "completed_checked": checked,
            "contract_pass": passed, "contract_failures": failures,
            "emitted": "counts only; no return values or distributions"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mwfd04-run", required=True, type=Path)
    parser.add_argument("--stage0-audit", required=True, type=Path)
    parser.add_argument("--review-packet", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--master-sha", required=True)
    args = parser.parse_args()
    if git("status", "--porcelain"):
        raise SystemExit("commit the schema-freeze code first (clean checkout required)")
    now = datetime.now(KST)
    out = args.output_root.resolve() / f"{now.strftime('%Y%m%dT%H%M%S%z')}-schema-freeze"
    out.mkdir(parents=True, exist_ok=False)
    run, stage0 = args.mwfd04_run.resolve(), args.stage0_audit.resolve()
    stage0_audit = json.loads((stage0 / "data_audit.json").read_text(encoding="utf-8"))
    artifact = json.loads((run / "artifact_manifest.json").read_text(encoding="utf-8"))
    trades_sha = next(e["sha256"] for e in artifact["entries"] if e["path"] == "trades.jsonl")
    review_stat = args.review_packet.stat()
    provenance = {
        "freeze_timestamp": now.isoformat(), "freeze_run_dir": str(out), "master_sha": args.master_sha,
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"), "analysis_code_commit": git("rev-parse", "HEAD"),
        "contract_code_sha256": {name: audit.sha256_stream(PROJECT_ROOT / name) for name in CONTRACT_FILES},
        "factor_contract_version": fc.CONTRACT_VERSION,
        "stage0_audit_dir": str(stage0), "stage0_verdict": stage0_audit["verdict"]["overall"],
        "stage0_analysis_code_commit": stage0_audit["provenance"]["analysis_code"]["commit"],
        "stage0_files_sha256": {p.name: audit.sha256_stream(p) for p in sorted(stage0.iterdir()) if p.is_file()},
        "mwfd04_run_id": artifact["run_id"],
        "mwfd04_artifact_manifest_sha256": audit.sha256_stream(run / "artifact_manifest.json"),
        "mwfd04_trades_sha256_manifest": trades_sha,
        "prior_outcome_exposure": {
            "note": "MWFD-04 descriptive statistics (KRW PnL distributions by state/stratum/candidate) were viewed before this freeze",
            "review_packet": str(args.review_packet), "review_packet_mtime": datetime.fromtimestamp(
                review_stat.st_mtime, KST).isoformat(),
            "review_packet_sha256": audit.sha256_stream(args.review_packet),
            "stage0_audit_timestamp": stage0_audit["provenance"]["audit_timestamp"],
            "not_viewed": "no %/bps return distribution and no F02-F07 value has been computed or viewed",
        },
    }
    states = stage0_audit["candidate_cell"]["state_counts"]
    fee_check = bounded_fee_check(run / "trades.jsonl", FEE_CHECK_ROWS)

    economic = {
        "schema": "mwfd_05_economic_outcome_contract_v1", "provenance": provenance,
        "estimand": "net return relative to executed entry notional (equal capital per new order in intended operation)",
        "trade_grain": {
            "population": "C: trades.status == COMPLETED (1,189,003), parent candidate-cell state carried",
            "entry_executed_notional_krw": "entry_fill.price * entry_fill.quantity (fees excluded)",
            "net_trade_pnl_krw": "stored net_pnl, verified == (exit_price - entry_price) * quantity - (entry_fee + exit_fee)",
            "net_trade_return": "net_trade_pnl_krw / entry_executed_notional_krw",
            "net_trade_return_pct": "100 * net_trade_return", "net_trade_return_bps": "10000 * net_trade_return",
            "decomposition": {"gross_trade_return_bps": "10000 * gross_pnl / entry notional",
                              "transaction_cost_bps": "10000 * (entry_fee + exit_fee) / entry notional",
                              "identity": "net = gross - cost"},
            "primary": "net_trade_return_bps", "secondary": ["net_trade_pnl_krw"],
            "requirements": ["status COMPLETED", "entry and exit fill present", "integer quantity > 0 equal on both fills",
                             "prices positive finite", "fee == fill price * quantity * 0.001 per side",
                             "net_pnl == gross - fees"],
            "forbidden_denominators": ["cash", "account equity", "100,000,000 KRW gate threshold", "market cap"],
        },
        "candidate_cell_grain": {
            "population": f"B: TRADED_FLAT only ({states['TRADED_FLAT']:,})",
            "cell_total_entry_notional_krw": "sum of completed trade entry notional",
            "cell_total_net_pnl_krw": "sum of completed trade net_pnl (must reconcile with fast_net_result)",
            "net_return_on_entry_notional": "cell_total_net_pnl_krw / cell_total_entry_notional_krw",
            "net_return_on_entry_notional_bps": "10000 * net_return_on_entry_notional",
            "primary": "net_return_on_entry_notional_bps", "secondary": ["cell_total_net_pnl_krw"],
            "also_kept": ["completed_trade_count", "mean_trade_return_bps", "median_trade_return_bps",
                          "positive_trade_rate", "cell_total_entry_notional_krw"],
            "naming_prohibited": ["account return", "compounded return", "portfolio return", "daily capital return"],
            "reason": "the same capital can be reused across trades of a cell",
        },
        "participation_grain": {
            "population": "A: all EVALUATED candidate-cells (852,618)",
            "outcomes": ["any_entry_signal (binary)", "any_completed_trade (binary)", "entry_signals (count)",
                         "completed_trades (count)", "terminal state (categorical)"],
            "rule": "no-trade is participation information; it is never a 0% return",
        },
        "state_handling": {
            "NO_TRADE_FLAT": {"count": states["NO_TRADE_FLAT"], "participation": "yes", "conditional_economics": "excluded (not 0%)"},
            "NO_TRADE_OPEN_ORDER": {"count": states["NO_TRADE_OPEN_ORDER"], "participation": "yes", "conditional_economics": "excluded"},
            "TRADED_FLAT": {"count": states["TRADED_FLAT"], "participation": "yes", "conditional_economics": "PRIMARY population B"},
            "TRADED_FLAT_OPEN_ORDER": {"count": states["TRADED_FLAT_OPEN_ORDER"], "participation": "yes",
                                       "conditional_economics": "excluded from B (unresolved order); its completed trades enter C"},
            "OPEN_POSITION": {"count": states["OPEN_POSITION"], "participation": "yes",
                              "conditional_economics": "CENSORED / NOT_ESTIMABLE; never 0%; never called episode return from realized trades",
                              "population_c": "completed trades before the open episode enter C with parent_state = OPEN_POSITION"},
            "ERROR_INCOMPLETE": {"count": states["ERROR_INCOMPLETE"], "economic_result": "null"},
        },
        "cost_model": {"fast": "0.1% of fill notional per side (~0.20% round trip), no separate tax schedule, no slippage beyond crossing the quote",
                       "note": "a zero-price-move round trip is about -20 bps net; this is an arithmetic property, not an expected result or threshold"},
        "fee_included_verification": {
            "generator": "scripts/run_mwfd_04_full.py::trade_records: gross = (sell.price - buy.price) * quantity; fees = buy.fee + sell.fee; net = gross - fees; research/fast_backtest/sweep.py: fee = price * quantity * fee_rate per fill",
            "stage0_full_reconciliation": "sum of completed net_pnl == fast_net_result for every TRADED_FLAT candidate-cell (Stage 0, all rows)",
            "bounded_check": fee_check,
        },
        "delta_E": {"status": "UNSET", "discovery": "NOT_REQUIRED_FOR_DISCOVERY", "confirmation": "REQUIRED_BEFORE_CONFIRMATION",
                    "rule": "must be justified ex ante (costs, capacity, operation) before confirmation; must not be derived from observed returns"},
        "sizing": "no order-sizing parameter is introduced; %/bps normalization only",
    }

    factor = {
        "schema": "mwfd_05_factor_registry_frozen_v1", "provenance": provenance,
        "decision_point": "every trade row of a cell stream (seq s_d, received_ns t_d); signaled decisions join via trades.pre_entry.seq",
        "causal_cutoff": "only events with seq < s_d; the decision trade row is excluded from every window",
        "clock": "collector receipt received_ns (monotonic ns)",
        "quote_freshness_ns": fc.MAX_QUOTE_AGE_NS, "window_ns": fc.WINDOW_NS,
        "regular_session_start_market_second": fc.REGULAR_SESSION_START_SECOND,
        "factors": fc.FACTOR_SPEC,
        "grains": {"module_A": "cell summaries of factor values over all trade rows of the cell (summary statistic to be frozen in Stage 1 plan before values are viewed)",
                   "module_C": "factor value at the signaled decision trade row", "module_B": "BLOCKED (no candidate risk-state ledger)"},
        "excluded": {"daily_metadata": "market_cap, trading_value, listed_shares: NOT_READY_EXCLUDED; no proxy may be named market cap",
                     "venue": "unknown; no KRX/NXT inference"},
        "future_extension": "size-factor slot reserved for a Historical PIT Market Metadata track (not active)",
    }

    schema = {
        "schema": "mwfd_05_schema_binding_frozen_v1", "provenance": provenance,
        "stage0_binding": "unchanged; see stage0 schema_binding.json",
        "populations": {
            "A_participation": {"grain": "cell x candidate", "count": 852618, "key": ["cell_id", "parameter_identity"]},
            "B_conditional_candidate_cell_economics": {"grain": "cell x candidate", "state": "TRADED_FLAT",
                                                       "count": states["TRADED_FLAT"], "primary": "net_return_on_entry_notional_bps"},
            "C_completed_trade_economics": {"grain": "cell x candidate x signaled entry decision", "count": 1189003,
                                            "key": ["cell_id", "parameter_identity", "trade_index"],
                                            "primary": "net_trade_return_bps", "carry": "parent_candidate_cell_state"},
            "D_censoring": {"state": "OPEN_POSITION", "count": states["OPEN_POSITION"], "economics": "CENSORED / NOT_ESTIMABLE"},
        },
        "modules": {"A": "SUPPORTED", "B": "BLOCKED", "C": "SUPPORTED", "open_position_economics": "CENSORED"},
        "discovery_principle": "association discovery is not filtered-strategy PnL; any candidate cut must be applied to the strategy and fully replayed (position state, cooldown, later signals, order sequence) before its economics are stated",
        "dependence": "candidates and candidate-cells are not independent samples; single date bounded prefix; clustering unit to be frozen in Stage 1 plan",
    }

    write_x(out / "economic_outcome_contract.json", economic)
    write_x(out / "factor_registry_frozen.json", factor)
    write_x(out / "schema_binding_frozen.json", schema)
    write_x(out / "SCHEMA_BINDING_FREEZE.md", render_freeze(provenance, states, fee_check))
    write_x(out / "analysis_plan_delta.md", render_delta(provenance))
    print(json.dumps({"out": str(out), "fee_check": fee_check}, ensure_ascii=False))
    return 0 if not fee_check["contract_failures"] else 1


def render_freeze(p: dict, states: dict, fee_check: dict) -> str:
    rows = "\n".join(f"| {slot} | `{spec['name']}` | {spec['formula']} |" for slot, spec in fc.FACTOR_SPEC.items())
    return f"""# MWFD-05 SAP — Schema Binding Freeze

- freeze: {p['freeze_timestamp']} · run `{p['freeze_run_dir']}`
- master `{p['master_sha']}` · branch `{p['branch']}` · code `{p['analysis_code_commit']}`
- Stage 0: `{p['stage0_audit_dir']}` ({p['stage0_verdict']}, code `{p['stage0_analysis_code_commit']}`)
- MWFD-04 run `{p['mwfd04_run_id']}` (read-only)
- 결과 노출 기록: MWFD-04 KRW 기술통계(Review Packet, {p['prior_outcome_exposure']['review_packet_mtime']})를 본 뒤의 freeze다. %/bps return 분포와 F02–F07 값은 계산·열람하지 않았다.

이번 단계는 계약 동결만 한다. factor materialization, bin, 상관, 회귀, bootstrap, multiple-testing, cut 선정, 순위화, replay, production exact는 하지 않았다.

## Primary economic outcome (estimand: 투입 명목금액 대비 순수익률)

| grain | primary | secondary |
|---|---|---|
| completed trade (Population C) | `net_trade_return_bps = 10000 × net_pnl / (entry_price × quantity)` | `net_trade_pnl_krw` |
| TRADED_FLAT candidate-cell (Population B) | `net_return_on_entry_notional_bps = 10000 × Σnet_pnl / Σ(entry_price × quantity)` | `cell_total_net_pnl_krw` |
| participation (Population A) | binary/count (신호·완결 거래 여부·건수, terminal state) | — |

- net_pnl은 비용 포함이다: `(exit − entry) × q − (entry_fee + exit_fee)`, fee = fill notional × 0.001(편도). 분모는 수수료를 뺀 체결 명목금액이며 현금·equity·1억 gate·시총을 쓰지 않는다.
- 비용 포함 검증: 생성 코드 확인 + Stage 0 전 행 재조정(Σnet = fast_net_result) + trades.jsonl 앞 {FEE_CHECK_ROWS:,}줄 bounded 계약 검사 {fee_check['contract_pass']:,}/{fee_check['completed_checked']:,} PASS (값은 출력하지 않음).
- candidate-cell 값은 계좌·복리·포트폴리오·일일 자본 수익률이 아니다(같은 자본 재사용).

## 상태 처리

| state | count | participation | conditional economics |
|---|---:|---|---|
| NO_TRADE_FLAT | {states['NO_TRADE_FLAT']:,} | 포함 | 제외 (0% 아님) |
| NO_TRADE_OPEN_ORDER | {states['NO_TRADE_OPEN_ORDER']:,} | 포함 | 제외 |
| TRADED_FLAT | {states['TRADED_FLAT']:,} | 포함 | **Population B** |
| TRADED_FLAT_OPEN_ORDER | {states['TRADED_FLAT_OPEN_ORDER']:,} | 포함 | B 제외, 완결 거래는 C |
| OPEN_POSITION | {states['OPEN_POSITION']:,} | 포함 | CENSORED / NOT_ESTIMABLE; 이전 완결 거래는 C(parent state 보존) |
| ERROR_INCOMPLETE | {states['ERROR_INCOMPLETE']:,} | — | null |

## Factor registry (frozen, `{fc.CONTRACT_VERSION}`)

공통: decision point = cell stream의 trade row(seq s_d, received_ns t_d). 모든 factor는 seq < s_d 이벤트만 쓰고 decision trade 자신은 모든 창에서 제외한다. 시계는 received_ns. quote 신선도 2초. 창 30초 `[t_d − 30s, t_d]`(시작 포함) ∩ seq < s_d.

| slot | name | formula |
|---|---|---|
{rows}

missing·최소 지원 규칙은 `factor_registry_frozen.json`과 `research/mwfd05/factor_contract.py`가 정본이다.

## δ_E

`UNSET` — `NOT_REQUIRED_FOR_DISCOVERY`, `REQUIRED_BEFORE_CONFIRMATION`. 관측 return으로 δ_E를 만들지 않는다.
"""


def render_delta(p: dict) -> str:
    return f"""# MWFD-05 analysis plan delta — schema binding freeze

- 기록 시각: {p['freeze_timestamp']} · code `{p['analysis_code_commit']}`
- 사전계획(SAP v1) 원문은 저장소에 없다. 컨트롤타워가 제시한 사전계획 항목을 기준으로 변경점을 구분한다.

## 1. 기존 사전계획 유지

- 질문: 어떤 시간적으로 유효한 시장 상태가 참여와 경제적 결과 차이를 설명하고, 어떤 관계를 별도 날짜에서 검증할 가치가 있는가. 최고 PnL 후보 찾기가 아니다.
- Fast = screening/research evaluator, production exact = 최종 evaluator.
- 663 후보·852,618 candidate-cell은 독립 표본이 아니다. discovery는 2026-09-21 한 날짜 bounded prefix다.
- venue `unknown` 유지, daily market_cap/trading_value/listed_shares `NOT_READY_EXCLUDED` 유지.
- primary factor slot F01–F07의 의미(이름)는 유지하고 다른 factor로 교체하지 않는다.
- factor/bin/robustness/multiple-testing 기준을 결과에 맞춰 바꾸지 않는다.

## 2. Stage 0 schema 때문에 확정된 항목

- Module A SUPPORTED, Module B BLOCKED(신호 발생 decision만 ledger에 있음), Module C SUPPORTED, open-position economics CENSORED.
- candidate-cell 상태 6종과 count, Population A/B/C/D binding.
- trades key `(cell_id, parameter_identity, trade_index)`, 내용 = signaled entry decisions.
- F01 = 기존 저장값(SUPPORTED_DIRECT). decision grain에서는 gate 때문에 ≥ 1억 KRW로 절단됨.
- F02–F07의 정확한 공식, endpoint, freshness, missing, unknown side, 최소 지원 규칙 동결(`{p['factor_contract_version']}`). 기존 유사 필드(spread_pct, obi bid3/ask3, 15틱 buy_ratio/volume, prior_high)는 대체 사용 금지.

## 3. 연구 목적 명확화로 변경된 항목

- **primary economic scale: KRW PnL → 체결 진입 명목금액 대비 순수익률(%/bps).** trade grain `net_trade_return_bps`, TRADED_FLAT candidate-cell grain `net_return_on_entry_notional_bps`. KRW PnL은 secondary descriptive로 보존.
- 근거: 실제 운용은 신규 주문마다 대략 같은 자본을 배정할 예정이라, 1주 기준 KRW PnL은 종목 가격 수준을 그대로 반영해 가격 수준 효과를 alpha로 오인하게 만든다. 이것은 결과가 좋아 보이는 쪽을 고른 사후 tuning이 아니라 연구 estimand의 명확화다.
- **투명성:** 이 변경은 MWFD-04 KRW 기술통계(Review Packet {p['prior_outcome_exposure']['review_packet_mtime']}, sha256 `{p['prior_outcome_exposure']['review_packet_sha256']}`)와 Stage 0 audit({p['prior_outcome_exposure']['stage0_audit_timestamp']})을 본 **이후**에 결정됐다. %/bps return 분포와 F02–F07 값은 이 freeze 전까지 계산·열람하지 않았다.
- **δ_E:** 절대 KRW δ_E를 discovery의 blocking requirement에서 내린다 → `NOT_REQUIRED_FOR_DISCOVERY`, `REQUIRED_BEFORE_CONFIRMATION`. 관측 결과로 δ_E를 만들지 않는다.
- no-trade는 participation 정보로만 쓰고 0% return으로 섞지 않는다. OPEN_POSITION은 0%·episode return으로 쓰지 않는다.
- sizing parameter는 도입하지 않는다(%/bps 정규화만).
- association discovery ≠ filtered strategy PnL: cut 후보는 전략에 적용해 전체 replay한 뒤에만 경제성을 말한다.

## 4. 여전히 차단된 항목

- Module B (candidate risk-state를 포함한 at-risk decision ledger 없음).
- open-position economics (terminal mark 없음; mark-to-market 규칙은 별도 SAP 변경 대상).
- δ_E (confirmation 전 필수).
- market cap 등 PIT daily metadata (Historical PIT Market Metadata 트랙 후 size-factor extension으로만).
- venue 귀속.
- 독립성·clustering 단위와 multiple-testing 절차는 Stage 1 plan에서 값 열람 전에 동결해야 한다.
"""


if __name__ == "__main__":
    raise SystemExit(main())
