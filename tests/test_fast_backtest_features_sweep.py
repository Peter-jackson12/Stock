from dataclasses import replace

import pytest

from engine.tick_ordering import OrderedTick
from research.fast_backtest.exact import compare_fast_exact, production_exact_runner
from research.fast_backtest.features import FeatureConfig, build_causal_features
from research.fast_backtest.sweep import (
    FastAccountAssumptions,
    deduplicate_candidates,
    evaluate_candidate,
    feature_config_for_candidates,
    materialize_strategy_params,
    rank_results,
)


DIGEST = "0" * 64


def quote(seq=1, ns=0, second=32399, **changes):
    base = OrderedTick(
        source="fixture",
        session_id="session",
        seq=seq,
        received_ns=ns,
        code="005930",
        venue="unknown",
        kind="quote",
        bid=99,
        ask=100,
        bid_size=10,
        ask_size=1,
        market_second=second,
        bid_sizes=(10, 10, 10),
        ask_sizes=(1, 1, 1),
    )
    return replace(base, **changes)


def trade(seq=2, ns=1, second=32400, **changes):
    return replace(
        quote(seq, ns, second),
        **(dict(kind="trade", price=100, volume=10, is_buy=True) | changes),
    )


def candidate(exit_rule="fixed", **changes):
    params = {
        "spread_max_pct": 0.02,
        "buy_ratio_min": 0.5,
        "obi_min_ratio": 1.0,
        "min_vol_15t": 10,
        "recent_ticks": 2,
        "breakout_window_sec": 30,
        "session_start_sec": 32400,
        "exit_rule": exit_rule,
        "stop_loss_pct": -0.02,
    }
    params.update(changes)
    return deduplicate_candidates([{"candidate_id": "c", "params": params}])[0]


def account(**changes):
    base = dict(
        cash=1000,
        fee_rate=0.001,
        quantity=1,
        buy_latency_ns=0,
        sell_latency_ns=0,
        max_quote_age_ns=100,
        cooldown_ns=10,
        close_ns=50,
    )
    return FastAccountAssumptions(**(base | changes))


def cache(events, selected=None, max_quote_age_ns=100):
    selected = selected or candidate()
    config = feature_config_for_candidates([selected], max_quote_age_ns)
    return build_causal_features(events, config=config, input_event_digest=DIGEST)


def test_feature_cache_has_no_future_leakage():
    prefix = [quote(), trade()]
    left = cache(prefix + [trade(3, 2, 32401, price=105)])
    right = cache(prefix + [trade(3, 2, 32401, price=999)])
    assert left.rows[1] == right.rows[1]


def test_rolling_boundary_includes_exact_second_and_excludes_older():
    config = FeatureConfig((2,), (30,), (32400,), 100)
    built = build_causal_features(
        [quote(), trade(2, 1, 32400, price=110), trade(3, 2, 32430, price=100), trade(4, 3, 32431, price=90)],
        config=config,
        input_event_digest=DIGEST,
    )
    assert built.rows[2].prior_high_by_window["30"] == 110
    assert built.rows[3].prior_high_by_window["30"] == 100


def test_recent_window_includes_current_and_only_first_available_events():
    config = FeatureConfig((3,), (30,), (32400,), 100)
    built = build_causal_features(
        [quote(), trade(volume=4), trade(3, 2, 32401, volume=6, is_buy=False)],
        config=config,
        input_event_digest=DIGEST,
    )
    assert built.rows[1].recent_volume_by_ticks["3"] == 4
    assert built.rows[1].buy_ratio_by_ticks["3"] == 1.0
    assert built.rows[2].recent_volume_by_ticks["3"] == 10
    assert built.rows[2].buy_ratio_by_ticks["3"] == 0.4


def test_unknown_direction_suppresses_buy_ratio_until_it_ages_out():
    config = FeatureConfig((2,), (30,), (32400,), 100)
    built = build_causal_features(
        [
            quote(),
            trade(is_buy=None),
            trade(3, 2, 32401, is_buy=True),
            trade(4, 3, 32402, is_buy=True),
        ],
        config=config,
        input_event_digest=DIGEST,
    )
    assert built.rows[2].buy_ratio_by_ticks["2"] is None
    assert built.rows[3].buy_ratio_by_ticks["2"] == 1.0


@pytest.mark.parametrize(
    ("events", "expected_entry"),
    [
        ([quote(ask=110), trade()], 0),
        ([quote(bid_sizes=(1, 1, 1), ask_sizes=(10, 10, 10), bid_size=1, ask_size=10), trade()], 0),
        ([quote(), trade(is_buy=False)], 0),
        ([quote(), trade(volume=1)], 0),
        ([quote(), trade(price=110), trade(3, 2, 32401, price=100)], 1),
        ([quote(ns=0), trade(ns=101)], 0),
    ],
    ids=["spread", "obi", "buy-ratio", "volume", "breakout-prior-entry-only", "stale-quote"],
)
def test_entry_rejections_are_fail_closed(events, expected_entry):
    selected = candidate()
    built = cache(events, selected, max_quote_age_ns=100)
    result = evaluate_candidate(built, selected, account=account())
    assert result.entry_signals == expected_entry


def test_session_boundary_and_no_fill_are_explicit():
    selected = candidate(min_vol_15t=1)
    events = [quote(second=32398), trade(second=32399), trade(3, 2, 32400)]
    result = evaluate_candidate(cache(events, selected), selected, account=account(buy_latency_ns=100))
    assert result.entry_decision_ns == (2,)
    assert result.fills == 0
    assert result.trades == 0


@pytest.mark.parametrize(
    ("selected", "events"),
    [
        (candidate(take_profit_pct=0.01), [quote(), trade(), quote(3, 2, 32401, bid=105, ask=106)]),
        (candidate(stop_loss_pct=-0.02), [quote(), trade(), quote(3, 2, 32401, bid=95, ask=96)]),
        (
            candidate(exit_rule="tick_trail", trail_ticks=5),
            [
                quote(),
                trade(),
                quote(3, 2, 32401, bid=109, ask=110),
                trade(4, 3, 32401, price=110),
                quote(5, 4, 32402, bid=104, ask=105),
            ],
        ),
    ],
    ids=["single-buy-sell", "stop-loss", "trailing-exit"],
)
def test_fast_matches_production_exact_on_core_synthetic_paths(tmp_path, selected, events):
    assumptions = account()
    fast = evaluate_candidate(cache(events, selected), selected, account=assumptions)
    runner = production_exact_runner(
        events,
        output_root=tmp_path,
        dataset_label="synthetic-parity",
        simulator_config={
            "source": "fixture",
            "session_id": "session",
            "instruments": {"005930": "unknown"},
            "cash": assumptions.cash,
            "fee_rate": assumptions.fee_rate,
            "max_quote_age_ns": assumptions.max_quote_age_ns,
            "buy_latency_ns": assumptions.buy_latency_ns,
            "sell_latency_ns": assumptions.sell_latency_ns,
            "cancel_latency_ns": 0,
        },
        close_ns=assumptions.close_ns,
        quantity=assumptions.quantity,
        cooldown_ns=assumptions.cooldown_ns,
        unknown_direction_policy="strict",
        input_provenance={"kind": "synthetic"},
    )
    exact = runner(selected)
    assert compare_fast_exact(fast, exact) == ("PASS", ())


@pytest.mark.parametrize(
    ("selected", "events", "account_changes"),
    [
        (candidate(), [quote()], {}),
        (candidate(), [quote(ask=110), trade()], {}),
        (
            candidate(),
            [quote(bid_sizes=(1, 1, 1), ask_sizes=(10, 10, 10), bid_size=1, ask_size=10), trade()],
            {},
        ),
        (candidate(), [quote(), trade(is_buy=False)], {}),
        (candidate(), [quote(), trade(volume=1)], {}),
        (candidate(min_vol_15t=20), [quote(), trade(price=110), trade(3, 2, 32401, price=100)], {}),
        (
            candidate(min_vol_15t=1),
            [quote(second=32398), trade(second=32399), trade(3, 2, 32400)],
            {"buy_latency_ns": 100},
        ),
        (candidate(), [quote(ns=0), trade(ns=101)], {"max_quote_age_ns": 10, "close_ns": 200}),
        (candidate(), [quote(), trade()], {"buy_latency_ns": 100}),
    ],
    ids=[
        "no-signal", "spread-rejection", "obi-rejection", "buy-ratio-rejection",
        "volume-rejection", "breakout-rejection", "session-boundary", "stale-quote", "no-fill",
    ],
)
def test_fast_matches_exact_on_rejection_and_boundary_paths(tmp_path, selected, events, account_changes):
    assumptions = account(**account_changes)
    built = cache(events, selected, max_quote_age_ns=assumptions.max_quote_age_ns)
    fast = evaluate_candidate(built, selected, account=assumptions)
    runner = production_exact_runner(
        events,
        output_root=tmp_path,
        dataset_label="synthetic-rejection-parity",
        simulator_config={
            "source": "fixture",
            "session_id": "session",
            "instruments": {"005930": "unknown"},
            "cash": assumptions.cash,
            "fee_rate": assumptions.fee_rate,
            "max_quote_age_ns": assumptions.max_quote_age_ns,
            "buy_latency_ns": assumptions.buy_latency_ns,
            "sell_latency_ns": assumptions.sell_latency_ns,
            "cancel_latency_ns": 0,
        },
        close_ns=assumptions.close_ns,
        quantity=assumptions.quantity,
        cooldown_ns=assumptions.cooldown_ns,
        unknown_direction_policy="strict",
        input_provenance={"kind": "synthetic"},
    )
    assert compare_fast_exact(fast, runner(selected)) == ("PASS", ())


def test_candidate_dedup_and_deterministic_tie_ranking():
    one = {"candidate_id": "one", "params": {"spread_max_pct": 0.5}}
    alias = {"candidate_id": "alias", "params": {"spread_max_pct": 0.5000}}
    other = {"candidate_id": "other", "params": {"spread_max_pct": 0.6}}
    candidates = deduplicate_candidates([one, alias, other])
    assert len(candidates) == 2
    assert any(candidate.aliases == ("one", "alias") for candidate in candidates)

    results = [
        replace(
            evaluate_candidate(cache([quote(), trade(is_buy=False)], candidate()), candidate(), account=account()),
            parameter_identity=identity,
            candidate_id=identity,
        )
        for identity in ("b", "a")
    ]
    assert [result.parameter_identity for result in rank_results(results)] == ["a", "b"]


def test_materialize_accepts_existing_search_top_level_fee_rate():
    params, exit_rule = materialize_strategy_params(
        {"fee_rate": 0.001, "exit_rule": "tick_trail", "trail_ticks": 5}
    )

    assert params["fee_rate"] == 0.001
    assert params["exits"]["tick_trail"]["trail_ticks"] == 5
    assert exit_rule == "tick_trail"
