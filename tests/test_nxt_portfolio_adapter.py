"""기존 NXT 규칙 하나를 공유계좌/순수 intent 경계에 연결하는 합성 회귀."""
from dataclasses import replace
from decimal import Decimal
import json

import pytest

from engine.nxt_portfolio_research import NxtPortfolioRunFailed, run_nxt_portfolio
from engine.portfolio_session import replay_portfolio_chunk
from engine.tick_ordering import OrderedTick
from engine.tick_session import replay_chunk
from execution.portfolio_simulator import PortfolioSimulator, RiskLimits
from execution.tick_simulator import TickSimulator
from strategies.nxt_breakout.direction_window import (
    POLICY as QUARANTINE_UNKNOWN_DIRECTION_POLICY,
)
from strategies.nxt_breakout.portfolio_adapter import NxtPortfolioStrategy
from strategies.nxt_breakout.tick_research import NxtResearchStrategy


def quote(seq=1, ns=0, code="A", second=32399, **changes):
    event = OrderedTick(
        "test", "s", seq, ns, code, "unknown", "quote",
        bid=10000, ask=10001, bid_size=10, ask_size=3,
        market_second=second,
        bid_sizes=(10, 10, 10), ask_sizes=(3, 3, 3),
    )
    return replace(event, **changes)


def trade(seq=2, ns=1, code="A", second=32400, **changes):
    base = quote(seq, ns, code, second)
    return replace(
        base,
        **(dict(kind="trade", price=10001, volume=30, is_buy=True) | changes),
    )


def legacy_sim(code="A", cash=1_000_000, fee_rate="0.001"):
    return TickSimulator(
        source="test", session_id="s", code=code, venue="unknown",
        cash=cash, fee_rate=fee_rate, max_quote_age_ns=100,
        buy_latency_ns=5, sell_latency_ns=5, cancel_latency_ns=2,
    )


def portfolio_config(cash=1_000_000, instruments=None, fee_rate="0.001"):
    return dict(
        source="test", session_id="s",
        instruments=instruments or {"A": "unknown"},
        cash=cash, fee_rate=fee_rate, max_quote_age_ns=100,
        buy_latency_ns=5, sell_latency_ns=5, cancel_latency_ns=2,
    )


def portfolio_sim(**changes):
    return PortfolioSimulator(**(portfolio_config() | changes))


def economic_fill(fill):
    return (fill.time_ns, fill.side, fill.quantity, fill.price, fill.fee, fill.quote_seq)


def test_single_symbol_adapter_matches_existing_nxt_economics():
    old = legacy_sim()
    new = portfolio_sim()
    old_strategy = NxtResearchStrategy(quantity=2)
    new_strategy = NxtPortfolioStrategy(instruments={"A": "unknown"}, quantity=2)

    prefix = [quote(), trade()]
    replay_chunk(old, prefix, old_strategy)
    replay_portfolio_chunk(new, prefix, new_strategy)
    old.advance(6)
    new.advance(6)

    exit_quote = quote(3, 7, bid=10100, ask=10101, second=32401)
    replay_chunk(old, [exit_quote], old_strategy)
    replay_portfolio_chunk(new, [exit_quote], new_strategy)
    old.advance(12)
    new.advance(12)

    assert [economic_fill(fill) for fill in old.fills] == [
        economic_fill(fill) for fill in new.fills
    ]
    assert old.cash == new.snapshot().cash
    assert old.position == dict(new.snapshot().positions)["A"] == 0
    assert [(s.side, s.quantity, s.reason) for s in new_strategy.signals] == [
        ("buy", 2, "breakout"),
        ("sell", 2, "fixed"),
    ]


def test_one_strategy_keeps_per_symbol_state_but_competes_for_shared_cash():
    sim = PortfolioSimulator(**portfolio_config(
        cash=10001,
        fee_rate="0",
        instruments={"A": "unknown", "B": "unknown"},
    ))
    strategy = NxtPortfolioStrategy(
        instruments={"A": "unknown", "B": "unknown"},
        quantity=1,
    )
    events = [
        quote(1, 0, "A"),
        quote(2, 0, "B"),
        trade(3, 1, "A"),
        trade(4, 1, "B"),
    ]
    replay_portfolio_chunk(sim, events, strategy)
    orders = {order.code: order for order in sim.orders}

    assert orders["A"].status == "pending"
    assert orders["B"].status == "rejected"
    assert orders["B"].reason == "admission:insufficient_cash"
    assert [signal.code for signal in strategy.signals] == ["A", "B"]
    assert sim.snapshot().available_cash == 0

    sim.advance(6)
    assert [(fill.code, fill.quantity) for fill in sim.fills] == [("A", 1)]
    assert dict(sim.snapshot().positions) == {"A": 1, "B": 0}


def test_interleaved_symbols_do_not_share_open_or_tick_window_state():
    sim = PortfolioSimulator(**portfolio_config(
        instruments={"A": "unknown", "B": "unknown"},
    ))
    strategy = NxtPortfolioStrategy(
        instruments={"A": "unknown", "B": "unknown"},
        quantity=1,
    )
    replay_portfolio_chunk(sim, [
        quote(1, 0, "A"),
        trade(2, 1, "A", price=10001),
        quote(3, 2, "B"),
        trade(4, 3, "B", price=20000),
    ], strategy)
    assert [signal.code for signal in strategy.signals] == ["A", "B"]


def test_portfolio_adapter_chunking_matches_single_pass():
    events = [
        quote(1, 0, "A"),
        quote(2, 0, "B"),
        trade(3, 1, "A"),
        trade(4, 1, "B"),
        quote(5, 7, "A", bid=10100, ask=10101, second=32401),
    ]
    a = PortfolioSimulator(**portfolio_config(instruments={"A": "unknown", "B": "unknown"}))
    b = PortfolioSimulator(**portfolio_config(instruments={"A": "unknown", "B": "unknown"}))
    sa = NxtPortfolioStrategy(instruments={"A": "unknown", "B": "unknown"}, quantity=1)
    sb = NxtPortfolioStrategy(instruments={"A": "unknown", "B": "unknown"}, quantity=1)

    replay_portfolio_chunk(a, events, sa)
    for event in events:
        replay_portfolio_chunk(b, [event], sb)

    assert a.snapshot() == b.snapshot()
    assert a.transitions == b.transitions
    assert sa.signals == sb.signals


def test_adapter_rejects_event_outside_declared_universe():
    strategy = NxtPortfolioStrategy(instruments={"A": "unknown"}, quantity=1)
    sim = PortfolioSimulator(**portfolio_config())
    view = sim.on_event(quote(code="A"))
    bad = replace(view.event, code="B")
    with pytest.raises(ValueError, match="declared"):
        strategy(replace(view, event=bad), sim.snapshot())


def test_persisted_runner_records_strategy_identity_and_signals(tmp_path):
    path = run_nxt_portfolio(
        [quote(), trade()],
        output_root=tmp_path,
        dataset_label="synthetic-one-strategy",
        simulator_config=portfolio_config(),
        close_ns=20,
        quantity=2,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved["schema"] == "portfolio_research_result_v1"
    assert saved["status"] == "completed_with_open_position"
    assert saved["strategy"] == {
        "strategy_id": "nxt_breakout",
        "adapter_version": "nxt_portfolio_adapter_v1",
    }
    assert saved["settings"]["strategy"]["quantity"] == 2
    assert saved["settings"]["strategy"]["unknown_direction_policy"] == "strict"
    assert saved["strategy_signals"][0]["reason"] == "breakout"
    assert saved["raw_identity_verified"] is False
    assert saved["realized_pnl"] is None
    assert "strategies/nxt_breakout/portfolio_adapter.py" in saved["code_sha256"]
    assert "strategies/nxt_breakout/direction_window.py" in saved["code_sha256"]
    assert len(saved["reproducibility_key"]) == 64


def test_runner_rerun_preserves_files_and_reproducibility_key(tmp_path):
    kwargs = dict(
        events=[quote(), trade()],
        output_root=tmp_path,
        dataset_label="synthetic",
        simulator_config=portfolio_config(),
        close_ns=20,
        quantity=1,
    )
    first = run_nxt_portfolio(**kwargs)
    original = first.read_bytes()
    second = run_nxt_portfolio(**kwargs)
    assert first != second and first.read_bytes() == original
    a, b = json.loads(original), json.loads(second.read_bytes())
    assert a["reproducibility_key"] == b["reproducibility_key"]
    assert a["account"]["fills"] == b["account"]["fills"]


def test_strategy_setting_changes_reproducibility_even_when_intents_match(tmp_path):
    common = dict(
        events=[quote()],
        output_root=tmp_path,
        dataset_label="synthetic",
        simulator_config=portfolio_config(),
        close_ns=20,
        quantity=1,
    )
    a = json.loads(run_nxt_portfolio(**common, cooldown_ns=10).read_bytes())
    b = json.loads(run_nxt_portfolio(**common, cooldown_ns=11).read_bytes())
    assert a["order_intents"] == b["order_intents"] == []
    assert a["reproducibility_key"] != b["reproducibility_key"]


def test_runner_failure_persists_diagnostics_and_prior_intent(tmp_path):
    stream = [
        quote(),
        trade(),
        replace(trade(3, 2), session_id="wrong"),
    ]
    with pytest.raises(NxtPortfolioRunFailed) as caught:
        run_nxt_portfolio(
            stream,
            output_root=tmp_path,
            dataset_label="bad-stream",
            simulator_config=portfolio_config(),
            close_ns=20,
            quantity=1,
        )
    saved = json.loads(caught.value.path.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["diagnostics_only"]
    assert not saved["input_complete"]
    assert len(saved["order_intents"]) == 1
    assert saved["strategy_signals"][0]["reason"] == "breakout"


def test_runner_requires_explicit_cost_and_does_not_create_output(tmp_path):
    config = portfolio_config()
    del config["fee_rate"]
    with pytest.raises(ValueError, match="fee_rate"):
        run_nxt_portfolio(
            [],
            output_root=tmp_path,
            dataset_label="synthetic",
            simulator_config=config,
            close_ns=20,
            quantity=1,
        )
    assert list(tmp_path.iterdir()) == []


def test_runner_output_cash_matches_fill_ledger(tmp_path):
    path = run_nxt_portfolio(
        [quote(), trade()],
        output_root=tmp_path,
        dataset_label="synthetic",
        simulator_config=portfolio_config(cash=100000, fee_rate="0.001"),
        close_ns=20,
        quantity=2,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    fill = saved["account"]["fills"][0]
    expected = Decimal("100000") - Decimal(fill["price"]) * fill["quantity"] - Decimal(fill["fee"])
    assert Decimal(saved["account"]["cash"]) == expected


def test_runner_accepts_explicit_risk_limits_object(tmp_path):
    config = portfolio_config()
    config["risk"] = RiskLimits(
        max_position_per_symbol={"A": 2},
        max_gross_exposure="50000",
        max_open_orders=1,
    )
    path = run_nxt_portfolio(
        [quote()],
        output_root=tmp_path,
        dataset_label="risk-config",
        simulator_config=config,
        close_ns=20,
        quantity=1,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["settings"]["risk"]["max_open_orders"] == 1
    assert saved["settings"]["risk"]["max_position_per_symbol"] == {"A": 2}



def test_portfolio_quarantine_mode_accepts_unknown_direction_without_intent():
    sim = portfolio_sim()
    strategy = NxtPortfolioStrategy(
        instruments={"A": "unknown"},
        quantity=1,
        unknown_direction_policy=QUARANTINE_UNKNOWN_DIRECTION_POLICY,
    )

    replay_portfolio_chunk(sim, [
        quote(),
        trade(is_buy=None, volume=237016),
    ], strategy)

    assert strategy.signals == []
    assert sim.orders == []
    assert strategy.settings()["unknown_direction_policy"] == QUARANTINE_UNKNOWN_DIRECTION_POLICY


def test_runner_records_direction_policy_and_changes_reproducibility(tmp_path):
    common = dict(
        events=[quote()],
        output_root=tmp_path,
        dataset_label="direction-policy",
        simulator_config=portfolio_config(),
        close_ns=20,
        quantity=1,
    )
    strict = json.loads(run_nxt_portfolio(**common).read_bytes())
    quarantine = json.loads(run_nxt_portfolio(
        **common,
        unknown_direction_policy=QUARANTINE_UNKNOWN_DIRECTION_POLICY,
    ).read_bytes())

    assert strict["order_intents"] == quarantine["order_intents"] == []
    assert strict["settings"]["strategy"]["unknown_direction_policy"] == "strict"
    assert (
        quarantine["settings"]["strategy"]["unknown_direction_policy"]
        == QUARANTINE_UNKNOWN_DIRECTION_POLICY
    )
    assert strict["reproducibility_key"] != quarantine["reproducibility_key"]


def test_runner_quarantine_mode_accepts_unknown_direction_stream(tmp_path):
    path = run_nxt_portfolio(
        [quote(), trade(is_buy=None, volume=237016)],
        output_root=tmp_path,
        dataset_label="unknown-direction-quarantine",
        simulator_config=portfolio_config(),
        close_ns=20,
        quantity=1,
        unknown_direction_policy=QUARANTINE_UNKNOWN_DIRECTION_POLICY,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved["status"] == "completed"
    assert saved["order_intents"] == []
    assert saved["strategy_signals"] == []
    assert (
        saved["settings"]["strategy"]["unknown_direction_policy"]
        == QUARANTINE_UNKNOWN_DIRECTION_POLICY
    )


def test_invalid_runner_direction_policy_fails_before_output(tmp_path):
    with pytest.raises(ValueError, match="unknown-direction policy"):
        run_nxt_portfolio(
            [quote()],
            output_root=tmp_path,
            dataset_label="invalid-direction-policy",
            simulator_config=portfolio_config(),
            close_ns=20,
            quantity=1,
            unknown_direction_policy="guess-direction",
        )
    assert list(tmp_path.iterdir()) == []
