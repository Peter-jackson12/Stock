from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

import pytest

from engine.tick_ordering import OrderedTick
from research.fast_backtest.full_run import (
    ResumableConcatWriter,
    ResumableEventCacheBuilder,
    ResumableSha256,
    load_full_population,
)
from research.fast_backtest.input_cache import event_record
from research.fast_backtest.plan import canonical_json
from research.fast_backtest.runtime_probe import ProbeCell, ProbeEventCacheWriter, ProbeSample, json_digest
from research.fast_backtest.sweep import (
    FastAccountAssumptions,
    deduplicate_candidates,
    feature_config_for_candidates,
    run_fast_sweep,
)
from research.fast_backtest.features import build_causal_features
from scripts.run_mwfd_03_probe import economic_record
from scripts.run_mwfd_04_full import publish_tree, result_from_record, trade_records
from research.fast_backtest.sweep import materialize_strategy_params, result_record


def inventory(tmp_path, count=9, extra=()):
    cells = []
    for index in range(count):
        code = f"{index:06d}"
        cells.append({"cell_id": f"{code}=unknown", "code": code, "venue": "unknown",
                      "admission": "ELIGIBLE_FOR_FAST_PROBE", "activity": {"event_count": 100 - index}})
    cells.extend(extra)
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"session_id": "s", "cells": cells,
                                "admission_counts": {"ELIGIBLE_FOR_FAST_PROBE": count}}), encoding="utf-8")
    return path


def test_full_population_keeps_inventory_order_tiers_and_rejects_count_drift(tmp_path):
    extra = [{"cell_id": "999999=unknown", "code": "999999", "venue": "unknown",
              "admission": "NO_EXECUTABLE_OPPORTUNITY", "activity": {"event_count": 5}}]
    path = inventory(tmp_path, extra=extra)
    population = load_full_population(path, expected_count=9)
    assert [cell.index for cell in population.cells] == list(range(1, 10))
    assert [cell.code for cell in population.cells] == [f"{i:06d}" for i in range(9)]
    assert [cell.tier for cell in population.cells] == ["high"] * 3 + ["medium"] * 3 + ["low"] * 3
    assert population.manifest()["selection_used_pnl"] is False
    with pytest.raises(ValueError, match="POPULATION_INVALID"):
        load_full_population(path, expected_count=10)
    with pytest.raises(ValueError, match="digest mismatch"):
        load_full_population(path, expected_sha256="0" * 64, expected_count=9)


def test_resumable_sha256_survives_state_roundtrip():
    blocks = [bytes([i % 256]) * (i * 13 % 900) for i in range(3000)]
    reference, digest = hashlib.sha256(), ResumableSha256()
    for index, block in enumerate(blocks):
        reference.update(block)
        digest.update(block)
        if index % 700 == 0:
            digest = ResumableSha256(digest.state())
    assert digest.hexdigest() == reference.hexdigest()


def test_concat_writer_truncates_uncommitted_bytes_on_resume(tmp_path):
    target = tmp_path / "out.jsonl"
    writer = ResumableConcatWriter(target)
    writer.open()
    writer.write_lines([b"a\n", b"b\n"])
    writer.commit_cell(1)
    writer.write_lines([b"garbage-not-committed\n"])
    writer.close()
    resumed = ResumableConcatWriter(target)
    assert resumed.open()["next_cell"] == 1
    resumed.write_lines([b"c\n"])
    resumed.commit_cell(2)
    result = resumed.finalize()
    assert target.read_bytes() == b"a\nb\nc\n"
    assert result["sha256"] == hashlib.sha256(b"a\nb\nc\n").hexdigest() and result["rows"] == 3
    with pytest.raises(FileExistsError):
        ResumableConcatWriter(target).open()


def _tick(seq, code, kind="quote"):
    base = OrderedTick(source="fixture", session_id="session", seq=seq, received_ns=seq * 10,
                       code=code, venue="unknown", kind=kind, market_second=32400,
                       bid=99, ask=100, bid_size=1, ask_size=1, bid_sizes=(1, 1, 1), ask_sizes=(1, 1, 1))
    return replace(base, price=100, volume=1, is_buy=True) if kind == "trade" else base


def _raw(tmp_path, events, sentinel_seq):
    conn = sqlite3.connect(tmp_path / "raw.sqlite3")
    conn.execute("CREATE TABLE events(seq INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
    for event in events:
        conn.execute("INSERT INTO events VALUES (?,?)", (event.seq, json.dumps(event_record(event))))
    conn.execute("INSERT INTO events VALUES (?,?)", (sentinel_seq, json.dumps(event_record(_tick(sentinel_seq, "SENT")))))
    conn.commit()
    return conn


def _decode(seq, payload, manifest, expected_seq, last_ns):
    value = json.loads(payload)
    for name in ("bid_sizes", "ask_sizes"):
        if value.get(name) is not None:
            value[name] = tuple(value[name])
    event = OrderedTick(**value)
    assert event.seq == expected_seq == seq and event.received_ns >= last_ns
    utc = datetime.fromtimestamp(event.received_ns, tz=timezone.utc)
    return {"received_at_utc": utc.isoformat()}, event, utc


def test_chunked_event_cache_matches_one_pass_probe_writer(tmp_path):
    events = [_tick(seq, code, "trade" if seq % 3 == 0 else "quote")
              for seq, code in enumerate(["A", "B", "C", "A", "X", "B", "A", "C", "C", "A"] * 30, start=1)]
    selected = [event for event in events if event.code in {"A", "B", "C"}]
    counts = {code: sum(e.code == code for e in selected) for code in "ABC"}
    cells = tuple(ProbeCell(i + 1, f"{c}=unknown", c, "unknown", "high", counts[c], "h") for i, c in enumerate("ABC"))
    population = ProbeSample(cells, "a" * 64, "b" * 64, json_digest([c.to_dict() for c in cells]), "rule", 3)
    prefix = hashlib.sha256()
    for event in events:
        prefix.update(json.dumps(event_record(event)).encode("utf-8") + b"\n")
    raw = _raw(tmp_path, events, len(events) + 1)
    cutoff = datetime.fromtimestamp((len(events) + 1) * 10, tz=timezone.utc)
    builder = ResumableEventCacheBuilder(tmp_path / "build", population=population,
                                         source_prefix_digest=prefix.hexdigest(), expected_prefix_records=len(events))
    ticks = iter(range(10**6))
    state = None
    for _ in range(100):  # deadline이 매 50000 레코드 검사 → 작은 fixture는 한 번에 끝나므로 강제 분할
        state = builder.run_chunk(raw, manifest={}, cutoff_utc=cutoff, decode_record=_decode,
                                  deadline=0, clock=lambda: next(ticks))
        if state["status"] != "BUILDING":
            break
    assert state["status"] == "SOURCE_PASS_COMPLETE"
    chunked = builder.finish(state)
    writer = ProbeEventCacheWriter(tmp_path / "probe", source_prefix_digest=prefix.hexdigest(), sample=population)
    for event in events:
        writer.append(event)
    reference = writer.finish(observed_prefix_digest=prefix.hexdigest()).manifest
    assert chunked["cells"] == reference["cells"]
    assert chunked["cache_id"] == reference["cache_id"]


def test_event_cache_builder_deletes_rows_past_state_after_interrupted_commit(tmp_path):
    events = [_tick(seq, "A") for seq in range(1, 21)]
    cells = (ProbeCell(1, "A=unknown", "A", "unknown", "high", 20, "h"),)
    population = ProbeSample(cells, "a" * 64, "b" * 64, json_digest([c.to_dict() for c in cells]), "rule", 1)
    prefix = hashlib.sha256()
    for event in events:
        prefix.update(json.dumps(event_record(event)).encode("utf-8") + b"\n")
    raw = _raw(tmp_path, events, 21)
    builder = ResumableEventCacheBuilder(tmp_path / "build", population=population,
                                         source_prefix_digest=prefix.hexdigest(), expected_prefix_records=20)
    builder.load_state()
    conn = sqlite3.connect(builder.db_path)
    conn.execute("INSERT INTO events VALUES (1, 10, 'A=unknown', 'stale')")  # commit 후 상태 미기록 흉내
    conn.commit()
    conn.close()
    cutoff = datetime.fromtimestamp(210, tz=timezone.utc)
    state = builder.run_chunk(raw, manifest={}, cutoff_utc=cutoff, decode_record=_decode, deadline=10**9,
                              clock=lambda: 0)
    manifest = builder.finish(state)
    assert manifest["event_count"] == 20


def _candidate(**changes):
    params = {"spread_max_pct": 0.02, "buy_ratio_min": 0.5, "obi_min_ratio": 1.0, "min_vol_15t": 10,
              "recent_ticks": 2, "breakout_window_sec": 30, "session_start_sec": 32400,
              "exit_rule": "fixed", "stop_loss_pct": -0.02}
    params.update(changes)
    return params


def test_candidate_chunking_and_record_roundtrip_preserve_economic_digest():
    def q(seq=1, ns=0, second=32399, bid=99, ask=100):
        return OrderedTick(source="fixture", session_id="session", seq=seq, received_ns=ns, code="005930",
                           venue="unknown", kind="quote", bid=bid, ask=ask, bid_size=10, ask_size=1,
                           market_second=second, bid_sizes=(10, 10, 10), ask_sizes=(1, 1, 1))

    def t(seq=2, ns=1, second=32400, price=100):
        return replace(q(seq, ns, second), kind="trade", price=price, volume=10, is_buy=True)

    events = [q(), t(), q(3, 2, 32401, bid=105, ask=106), t(4, 3, 32401, price=106),
              q(5, 14, 32402, bid=105, ask=106), t(6, 15, 32402, price=106), q(7, 16, 32403, bid=95, ask=96)]
    records = [{"candidate_id": str(i), "params": _candidate(take_profit_pct=0.01 + i * 0.01)}
               for i in range(7)]
    candidates = deduplicate_candidates(records)
    candidates = tuple(sorted(candidates, key=lambda c: c.parameter_identity))
    account = FastAccountAssumptions(cash=1000, fee_rate=0.001, quantity=1, buy_latency_ns=0, sell_latency_ns=0,
                                     max_quote_age_ns=100, cooldown_ns=1, close_ns=50)
    features = build_causal_features(events, config=feature_config_for_candidates(candidates, 100),
                                     input_event_digest="0" * 64)
    whole = sorted(run_fast_sweep(features, candidates, account=account), key=lambda r: r.parameter_identity)
    chunked = []
    for start in range(0, len(candidates), 3):
        chunked.extend(sorted(run_fast_sweep(features, candidates[start:start + 3], account=account),
                              key=lambda r: r.parameter_identity))
    roundtrip = [result_from_record(json.loads(canonical_json(result_record(r)))) for r in chunked]
    digest = lambda rs: json_digest([economic_record(r) for r in rs])
    assert digest(whole) == digest(chunked) == digest(roundtrip)
    assert any(r.trades for r in whole)
    trades_by_ns = {}
    for row in features.rows:
        if row.kind == "trade":
            trades_by_ns.setdefault(row.received_ns, []).append(row)
    cell = ProbeCell(1, "005930=unknown", "005930", "unknown", "high", len(events), "h")
    traded = next(r for r in whole if r.trades)
    params = materialize_strategy_params(next(c for c in candidates if c.parameter_identity == traded.parameter_identity).params)
    records = trade_records(traded, cell=cell, params=params, trades_by_ns=trades_by_ns, gate_states={}, account=account)
    assert len(records) == len(traded.entry_decision_ns)
    completed = [r for r in records if r["status"] == "COMPLETED"]
    assert len(completed) == traded.trades
    assert sum(r["gross_pnl"] for r in completed) == pytest.approx(traded.gross_pnl)
    assert all(r["pre_entry"]["seq"] for r in records if r["decision_row_resolution"] == "UNIQUE_TRADE_ROW")


def test_publish_tree_resumes_and_verifies(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "a").mkdir(parents=True)
    (src / "a" / "x.bin").write_bytes(b"1" * 100_000)
    (src / "y.json").write_text("{}")
    state = tmp_path / "state.json"
    assert publish_tree(src, dst, state, deadline=0.0) is False
    assert publish_tree(src, dst, state, deadline=float("inf")) is True
    assert (dst / "a" / "x.bin").read_bytes() == b"1" * 100_000 and (dst / "y.json").read_text() == "{}"
