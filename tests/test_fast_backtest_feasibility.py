from dataclasses import replace

import pytest

from engine.tick_ordering import OrderedTick
from research.fast_backtest.execution_depth import ExecutionDepthRecord
from research.fast_backtest.feasibility import feasibility_summary, join_entry_feasibility
from research.fast_backtest.features import (
    build_causal_features,
    load_feature_cache,
    write_feature_cache,
)
from research.fast_backtest.sweep import (
    FastAccountAssumptions,
    deduplicate_candidates,
    evaluate_candidate,
    feature_config_for_candidates,
)


DIGEST = "0" * 64


def quote(seq=1, ns=0, *, bid=99, ask=100):
    return OrderedTick(
        source="fixture", session_id="session", seq=seq, received_ns=ns,
        code="005930", venue="unknown", kind="quote", bid=bid, ask=ask,
        bid_size=10, ask_size=1, market_second=32400,
        bid_sizes=(10, 10, 10), ask_sizes=(1, 1, 1),
    )


def trade(seq=2, ns=1, *, price=100):
    return replace(quote(seq, ns), kind="trade", price=price, volume=10, is_buy=True)


def depth(seq=1, ns=0, *, notional=100_000_000, ask_status="COMPLETE", ask_reason=None):
    return ExecutionDepthRecord(
        source="fixture", session_id="session", seq=seq, received_ns=ns,
        code="005930", venue="unknown",
        ask_prices=tuple(range(100, 110)), ask_sizes=(100_000,) + (0,) * 9,
        bid_prices=tuple(range(99, 89, -1)), bid_sizes=(1,) * 10,
        ask_status=ask_status, ask_reason=ask_reason,
        bid_status="COMPLETE", bid_reason=None,
        ask_depth_notional_10=notional if ask_status == "COMPLETE" else None,
        bid_depth_notional_10=1_000,
        top_status="VALID", top_reason=None,
    )


def candidate():
    return deduplicate_candidates([{
        "candidate_id": "c",
        "params": {
            "spread_max_pct": 0.02, "buy_ratio_min": 0.5,
            "obi_min_ratio": 1.0, "min_vol_15t": 10,
            "recent_ticks": 2, "breakout_window_sec": 30,
            "session_start_sec": 32400, "exit_rule": "fixed",
            "stop_loss_pct": -0.02,
        },
    }])[0]


def account():
    return FastAccountAssumptions(
        cash=1_000, fee_rate=0.001, quantity=1,
        buy_latency_ns=0, sell_latency_ns=0, max_quote_age_ns=100,
        cooldown_ns=10, close_ns=100,
    )


def features(events):
    selected = candidate()
    return build_causal_features(
        events,
        config=feature_config_for_candidates([selected], 100),
        input_event_digest=DIGEST,
    )


@pytest.mark.parametrize(
    ("notional", "expected"),
    [(99_999_999, "FAIL"), (100_000_000, "PASS"), (100_000_001, "PASS")],
)
def test_entry_feasibility_exact_boundary(notional, expected):
    states = join_entry_feasibility([quote(), trade()], [depth(notional=notional)], max_quote_age_ns=100)
    assert states[2].status == expected
    summary = feasibility_summary(states)
    assert summary[expected] == 1
    assert summary["PASS"] + summary["FAIL"] + summary["UNKNOWN"] == summary["evaluated"]


def test_no_prior_stale_and_invalid_new_quote_are_unknown_without_backfill():
    events = [
        trade(1, 0),
        quote(2, 1),
        trade(3, 200),
        quote(4, 201),
        trade(5, 202),
    ]
    states = join_entry_feasibility(
        events,
        [depth(2, 1), depth(4, 201, ask_status="PARTIAL", ask_reason="zero_fid_44")],
        max_quote_age_ns=100,
    )
    assert {seq: state.reason for seq, state in states.items()} == {
        1: "missing_quote", 3: "stale_quote", 5: "zero_fid_44",
    }


def test_future_quote_does_not_change_past_gate_and_generator_chunking_is_identical():
    prefix_events = [quote(), trade()]
    prefix_depth = [depth(notional=99_999_999)]
    before = join_entry_feasibility(prefix_events, prefix_depth, max_quote_age_ns=100)
    extended = join_entry_feasibility(
        prefix_events + [quote(3, 2), trade(4, 3)],
        prefix_depth + [depth(3, 2, notional=100_000_001)],
        max_quote_age_ns=100,
    )
    generated = join_entry_feasibility(
        (item for chunk in ([quote()], [trade()], [quote(3, 2), trade(4, 3)]) for item in chunk),
        (item for chunk in ([depth(notional=99_999_999)], [depth(3, 2, notional=100_000_001)]) for item in chunk),
        max_quote_age_ns=100,
    )
    assert extended[2] == before[2]
    assert generated == extended


def test_gate_blocks_only_new_entry_and_preserves_history_and_exit_processing():
    selected = candidate()
    events = [quote(), trade(), quote(3, 2, bid=95, ask=96)]
    built = features(events)
    blocked = join_entry_feasibility(events, [depth(notional=99_999_999), depth(3, 2)], max_quote_age_ns=100)
    allowed = join_entry_feasibility(events, [depth(), depth(3, 2)], max_quote_age_ns=100)

    blocked_result = evaluate_candidate(built, selected, account=account(), entry_feasibility=blocked)
    allowed_result = evaluate_candidate(built, selected, account=account(), entry_feasibility=allowed)

    assert len(built.rows) == len(events)
    assert blocked_result.entry_signals == blocked_result.fills == blocked_result.trades == 0
    assert allowed_result.entry_signals == 1
    assert allowed_result.fills == 2
    assert allowed_result.trades == 1
    assert allowed_result.terminal_position == 0


def test_feasibility_coverage_is_exact_and_feature_cache_roundtrips(tmp_path):
    selected = candidate()
    built = features([quote(), trade()])
    with pytest.raises(ValueError, match="every and only trade"):
        evaluate_candidate(built, selected, account=account(), entry_feasibility={})

    write_feature_cache(built, tmp_path / "features")
    loaded = load_feature_cache(tmp_path / "features", expected_input_event_digest=DIGEST)
    assert loaded == built
    with pytest.raises(ValueError, match="input digest mismatch"):
        load_feature_cache(tmp_path / "features", expected_input_event_digest="1" * 64)
