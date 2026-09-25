from dataclasses import replace
import json

import pytest

from engine.tick_ordering import OrderedTick
from research.fast_backtest.execution_depth import (
    ExecutionDepthSpec,
    ExecutionDepthWriter,
    MarketInventoryAccumulator,
    execution_depth_record,
    load_execution_depth_cache,
)


def quote(seq=1, ns=100, *, target=100_000_000, missing=None, invalid=None):
    ask_prices = list(range(1000, 1010))
    if target == 99_999_999:
        ask_sizes = [99_000, 999] + [0] * 8
    elif target == 100_000_000:
        ask_sizes = [100_000, 0] + [0] * 8
    elif target == 100_000_001:
        ask_sizes = [99_999, 1] + [0] * 8
    else:
        raise ValueError("fixture target")
    bid_prices = list(range(999, 989, -1))
    bid_sizes = [1] * 10
    fids = {
        **{str(41 + index): str(value) for index, value in enumerate(ask_prices)},
        **{str(51 + index): str(value) for index, value in enumerate(bid_prices)},
        **{str(61 + index): str(value) for index, value in enumerate(ask_sizes)},
        **{str(71 + index): str(value) for index, value in enumerate(bid_sizes)},
    }
    if missing is not None:
        del fids[str(missing)]
    if invalid is not None:
        fids[str(invalid)] = "bad"
    event = OrderedTick(
        source="fixture", session_id="session", seq=seq, received_ns=ns,
        code="005930", venue="unknown", kind="quote",
        bid=999, ask=1000, bid_size=1, ask_size=ask_sizes[0],
        market_second=32400, bid_sizes=tuple(bid_sizes), ask_sizes=tuple(ask_sizes),
    )
    return {
        "event": event, "received_at_utc": "2026-09-21T00:00:00+00:00",
        "raw_fields": {"fids": fids, "issues": [], "price_policy": "positive_only"},
    }


def trade(seq, ns, *, code="005930"):
    return {
        "event": OrderedTick(
            source="fixture", session_id="session", seq=seq, received_ns=ns,
            code=code, venue="unknown", kind="trade", price=1000, volume=1,
            is_buy=True, market_second=32400,
        ),
        "received_at_utc": "2026-09-21T00:00:01+00:00",
        "raw_fields": {"fids": {"15": "+1"}, "issues": [], "price_policy": "positive_only"},
    }


@pytest.mark.parametrize(
    ("target", "status"),
    [(99_999_999, "FAIL"), (100_000_000, "PASS"), (100_000_001, "PASS")],
)
def test_exact_ten_level_boundary(target, status):
    acc = MarketInventoryAccumulator()
    acc.accept(quote(target=target))
    acc.accept(trade(2, 101))
    result = acc.result()["cells"][0]["event_weighted_gate"]
    assert result[status] == 1
    assert result["evaluated"] == 1


def test_missing_zero_and_invalid_depth_are_not_filled_or_inferred():
    missing = execution_depth_record(quote(missing=44))
    zero = quote()
    zero["raw_fields"]["fids"]["44"] = "0"
    zero = execution_depth_record(zero)
    invalid = execution_depth_record(quote(invalid=65))
    assert (missing.ask_status, missing.ask_reason) == ("PARTIAL", "missing_fid_44")
    assert (zero.ask_status, zero.ask_reason) == ("PARTIAL", "zero_fid_44")
    assert (invalid.ask_status, invalid.ask_reason) == ("INVALID", "invalid_fid_65")
    assert missing.ask_depth_notional_10 is None
    assert zero.ask_depth_notional_10 is None
    assert invalid.ask_depth_notional_10 is None


def test_no_prior_quote_and_invalid_new_quote_replace_prior_state():
    acc = MarketInventoryAccumulator()
    acc.accept(trade(1, 100))
    acc.accept(quote(2, 101))
    partial = quote(3, 102)
    partial["raw_fields"]["fids"]["44"] = "0"
    acc.accept(partial)
    acc.accept(trade(4, 103))
    cell = acc.result()["cells"][0]
    assert cell["event_weighted_gate"]["UNKNOWN"] == 2
    assert cell["event_weighted_gate"]["reasons"] == {"missing_quote": 1, "zero_fid_44": 1}


def test_receive_order_future_append_and_chunk_independence():
    prefix = [quote(1, 100, target=99_999_999), trade(2, 100)]
    future = quote(3, 100, target=100_000_001)

    def run(chunks):
        acc = MarketInventoryAccumulator()
        for chunk in chunks:
            for envelope in chunk:
                acc.accept(envelope)
        return acc.result()

    before = run([prefix])
    extended = run([prefix, [future]])
    chunked = run([[prefix[0]], [prefix[1], future]])
    assert before["cells"][0]["event_weighted_gate"]["FAIL"] == 1
    assert extended["cells"][0]["event_weighted_gate"] == chunked["cells"][0]["event_weighted_gate"]
    assert extended["cells"][0]["event_weighted_gate"]["FAIL"] == 1


def test_per_code_and_gate_reconciliation():
    acc = MarketInventoryAccumulator()
    acc.accept(quote())
    acc.accept(trade(2, 101))
    second = trade(3, 102, code="000660")
    acc.accept(second)
    result = acc.result()
    assert result["source_relevant_counts"] == {
        "tick_records": 3, "control_records": 0, "per_code_event_sum": 3, "reconciled": True,
    }
    for cell in result["cells"]:
        gate = cell["event_weighted_gate"]
        assert gate["PASS"] + gate["FAIL"] + gate["UNKNOWN"] == gate["evaluated"]


def test_depth_cache_roundtrip_and_source_digest_mismatch(tmp_path):
    spec = ExecutionDepthSpec(
        source="fixture", session_id="session", market_date="2026-09-21",
        cutoff_market_second_exclusive=36000, source_prefix_digest="a" * 64,
        source_path_identity={"size": 1, "mtime_ns": 2}, code_provenance={"code": "hash"},
    )
    writer = ExecutionDepthWriter(tmp_path / "cache", spec)
    writer.append(execution_depth_record(quote()))
    manifest = writer.finish(observed_prefix_digest="a" * 64)
    loaded = load_execution_depth_cache(manifest.cache_dir, expected_source_prefix_digest="a" * 64)
    assert loaded.quote_count == 1
    with pytest.raises(ValueError, match="source prefix digest mismatch"):
        load_execution_depth_cache(manifest.cache_dir, expected_source_prefix_digest="b" * 64)


def test_writer_fails_closed_before_publish_on_observed_digest_mismatch(tmp_path):
    spec = ExecutionDepthSpec(
        source="fixture", session_id="session", market_date="2026-09-21",
        cutoff_market_second_exclusive=36000, source_prefix_digest="a" * 64,
        source_path_identity={}, code_provenance={},
    )
    writer = ExecutionDepthWriter(tmp_path / "cache", spec)
    writer.append(execution_depth_record(quote()))
    with pytest.raises(ValueError, match="source prefix digest mismatch"):
        writer.finish(observed_prefix_digest="b" * 64)
    assert not (tmp_path / "cache" / spec.cache_id).exists()
