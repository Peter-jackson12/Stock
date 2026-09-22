"""실제 연구 함수/전략의 작은 합성 통합 감사. 운영 raw 접근 없음."""
from dataclasses import replace
from decimal import Context, ROUND_HALF_EVEN, ROUND_FLOOR, localcontext
from fractions import Fraction
import hashlib
import json

import pytest

from collector.raw_v2 import RawV2Writer
from engine.tick_research_run import ResearchRunFailed, run_raw_v2, run_research
from engine.tick_session import replay_chunk
from execution.tick_simulator import TickSimulator
from execution_audit_support import config, observable, quote
from execution_oracle import Specification
from strategies.nxt_breakout.tick_research import NxtResearchStrategy
from test_execution_metamorphic import partitions


def settings():
    return config(cash=100000, fee_rate="0.001", buy_latency_ns=5, sell_latency_ns=5,
                  cancel_latency_ns=2, max_quote_age_ns=100)


def market_events():
    q = quote(bid=10000, ask=10001, bid_size=10, ask_size=3,
              market_second=32399, bid_sizes=(10, 10, 10), ask_sizes=(3, 3, 3))
    trade = replace(q, seq=2, received_ns=1, kind="trade", market_second=32400,
                    price=10001, volume=30, is_buy=True)
    stop = replace(q, seq=3, received_ns=7, market_second=32401, bid=9900, ask=9901)
    return [q, trade, stop, replace(trade, seq=4, received_ns=9, market_second=32401, price=9900),
            replace(stop, seq=5, received_ns=15, market_second=32402)]


def run(root, stream=None, **changes):
    args = dict(output_root=root, dataset_label="oracle-fixture", simulator_config=settings(),
                close_ns=20, quantity=2)
    path = run_research(market_events() if stream is None else stream, **(args | changes))
    return json.loads(path.read_bytes())


def fill_projection(rows):
    return tuple((f["order_id"], f["side"], f["quantity"], Fraction(f["price"]),
                  f["time_ns"], Fraction(f["fee"]), f["quote_seq"]) for f in rows)


def economic_report(report):
    return {key: report[key] for key in ("status", "final_cash", "open_quantity", "orders", "fills", "signals")}


def assert_identity(report):
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
                                         sort_keys=True).encode("utf-8")).hexdigest()
    contracts = {key: report[key] for key in ("simulation_contract", "input_capabilities")}
    assert report["contract_sha256"] == digest(contracts)
    assert report["reproducibility_key"] == digest(dict(events=report["event_sha256"],
        settings=report["settings"], code=report["code_sha256"], provenance=report["input_provenance"],
        contracts=report["contract_sha256"]))


@pytest.mark.parametrize("rule", ("fixed", "tick_trail", "step_trail"))
def test_research_and_all_real_strategy_chunks_match_prescribed_oracle(tmp_path, rule):
    # 전략을 oracle에 복제하지 않는다. 이 fixture의 의도는 수동 지정한다:
    # 개장 첫 매수 조건 -> 1ns 주문, 충분한 손절 하락 -> 7ns 매도 주문.
    ref = Specification(settings())
    events = market_events()
    for event in events:
        ref.apply(("event", event))
        if event.seq == 2:
            ref.apply(("submit", f"nxt-{rule}-1", "buy", 2))
        if event.seq == 3:
            ref.apply(("submit", f"nxt-{rule}-2", "sell", 2))
    ref.apply(("close", 20))
    assert ref.state.cash == Fraction("99758.198") and ref.state.position == 0
    for chunks in partitions(events):
        sim, strategy = TickSimulator(**settings()), NxtResearchStrategy(quantity=2, exit_rule=rule)
        for chunk in chunks:
            replay_chunk(sim, chunk, strategy)
        sim.close(20)
        assert observable(sim) == ref.export()
        assert strategy.signals == [(1, "buy", 2, "breakout"), (7, "sell", 2, rule)]
    report = run(tmp_path, (event for event in events), exit_rule=rule)
    assert fill_projection(report["fills"]) == ref.state.fills
    assert Fraction(report["final_cash"]) == ref.state.cash
    assert report["open_quantity"] == 0 and report["status"] == "completed_flat"
    assert report["input_complete"] is True and report["diagnostics_only"] is False
    assert_identity(report)
    print(f"RESEARCH_ORACLE rule={rule} chunk_partitions=16 persisted_runs=1")


def test_unused_provenance_changes_identity_not_execution(tmp_path):
    a = run(tmp_path, input_provenance={"note": "A", "assumed_impact": 0})
    b = run(tmp_path, input_provenance={"note": "B", "assumed_impact": 999})
    assert economic_report(a) == economic_report(b)
    assert a["event_sha256"] == b["event_sha256"]
    assert a["contract_sha256"] == b["contract_sha256"]
    assert a["reproducibility_key"] != b["reproducibility_key"]
    assert_identity(a)
    assert_identity(b)


def test_unused_raw_metadata_does_not_change_normalized_execution(tmp_path):
    reports = []
    for label in ("A", "B"):
        path = tmp_path / (label + ".db")
        with RawV2Writer(path, source="oracle", session_id="synthetic", market_date="2026-09-22",
                         feed_scope="synthetic-only") as writer:
            for event in market_events():
                writer.append(event, received_at_utc="2026-09-22T00:00:00+00:00",
                              raw_fields={"unused_note": label}, exchange_ts_raw=label,
                              source_time_precision="unknown")
            writer.finish(close_ns=20)
        result = run_raw_v2(path, output_root=tmp_path / "results", simulator_config=settings(), quantity=2)
        reports.append(json.loads(result.read_bytes()))
    a, b = reports
    assert economic_report(a) == economic_report(b)
    assert a["event_sha256"] == b["event_sha256"]
    assert a["input_provenance"]["raw_manifest"]["payload_sha256"] != b["input_provenance"]["raw_manifest"]["payload_sha256"]
    assert a["reproducibility_key"] != b["reproducibility_key"]
    assert a["input_capabilities"]["market_by_order"]["supported"] is False
    assert_identity(a)
    assert_identity(b)


def test_stable_decimal_context_changes_identity_even_when_economics_are_exact(tmp_path):
    reports = []
    for precision, rounding in ((28, ROUND_HALF_EVEN), (40, ROUND_HALF_EVEN), (28, ROUND_FLOOR)):
        with localcontext(Context(prec=precision, rounding=rounding)):
            reports.append(run(tmp_path))
    assert all(economic_report(report) == economic_report(reports[0]) for report in reports)
    assert len({report["contract_sha256"] for report in reports}) == 3
    assert len({report["reproducibility_key"] for report in reports}) == 3
    for report in reports:
        assert_identity(report)


def test_late_iterator_failure_invalidates_existing_fills(tmp_path):
    def broken():
        yield from market_events()[:3]
        raise RuntimeError("synthetic late failure")
    with pytest.raises(ResearchRunFailed) as caught:
        run(tmp_path, broken())
    report = json.loads(caught.value.path.read_bytes())
    assert report["status"] == "failed" and report["diagnostics_only"] is True
    assert report["input_complete"] is False and len(report["fills"]) == 1
    assert "synthetic late failure" in report["error"]
    assert_identity(report)


def test_known_null_raw_manifest_diagnostic_counterexample(tmp_path):
    with pytest.raises(AttributeError):
        run(tmp_path, [], input_provenance={"raw_manifest": None})
    paths = list(tmp_path.glob("*/result.json"))
    assert len(paths) == 1
    assert json.loads(paths[0].read_bytes())["status"] == "running"


@pytest.mark.xfail(strict=True, raises=AttributeError,
                   reason="RUN-1: null raw_manifest가 완료/실패 기록 밖에서 예외를 발생시켜 running만 남김; 미수정")
def test_known_invalid_provenance_should_be_rejected_before_output(tmp_path):
    with pytest.raises(ValueError):
        run(tmp_path, [], input_provenance={"raw_manifest": None})
    assert list(tmp_path.iterdir()) == []
