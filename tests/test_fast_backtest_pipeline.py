from dataclasses import asdict, replace
import json

from engine.tick_ordering import OrderedTick
from research.fast_backtest.pipeline import run_fast_backtest_pipeline
from research.fast_backtest.plan import FastBacktestPlan
from research.fast_backtest.sweep import parameter_identity


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


def test_first_usable_pipeline_writes_create_only_manifest_and_exact_parity(tmp_path):
    params = {
        "spread_max_pct": 0.02,
        "buy_ratio_min": 0.5,
        "obi_min_ratio": 1.0,
        "min_vol_15t": 10,
        "recent_ticks": 2,
        "breakout_window_sec": 30,
        "session_start_sec": 32400,
        "exit_rule": "fixed",
        "take_profit_pct": 0.01,
        "stop_loss_pct": -0.02,
    }
    candidate_records = [{"candidate_id": "reference", "params": params}]
    metadata = tmp_path / "metadata.csv"
    metadata.write_text(
        "as_of_date,code,name,market,close_krw,volume_shares,trading_value_krw,market_cap_krw,listed_shares,float_ratio_pct,float_shares,float_market_cap_krw,source_upstream,validation_status\n"
        "2026-09-18,005930,삼성전자,KOSPI,261000,17489615,4554291006250,1525878716688000,5846278608,,,,fixture,VALID\n",
        encoding="utf-8",
    )
    events = [
        quote(),
        replace(quote(2, 1, 32400), kind="trade", price=100, volume=10, is_buy=True),
        quote(3, 2, 32401, bid=105, ask=106),
    ]
    event_path = tmp_path / "events.jsonl"
    event_path.write_text(
        "".join(json.dumps(asdict(event), ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )
    output = tmp_path / "run"
    plan = FastBacktestPlan(
        trade_dates=("2026-09-21",),
        instruments=("005930",),
        universe_source="fixture",
        universe_mode="causal_preopen",
        historical_metadata_source=str(metadata),
        cheap_filter_spec={},
        tick_input_provenance={"source_dataset_identity": "fixture-events", "policy": "strict"},
        cutoff_market_second_exclusive=36000,
        account_assumptions={
            "cash": 1000,
            "fee_rate": 0.001,
            "quantity": 1,
            "buy_latency_ns": 0,
            "sell_latency_ns": 0,
            "cancel_latency_ns": 0,
            "max_quote_age_ns": 100,
            "cooldown_ns": 10,
            "close_ns": 50,
        },
        candidate_source="fixture",
        parameter_identities=(parameter_identity(params),),
        exact_top_n=1,
        exact_ranking=("net_pnl_desc", "parameter_identity_asc"),
        output_directory=str(output),
        code_revision="deadbeef",
    )

    manifest_path = run_fast_backtest_pipeline(
        plan=plan,
        metadata_csv=metadata,
        events_jsonl=event_path,
        candidate_records=candidate_records,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["engine"] == "fast_backtest_v1"
    assert manifest["screening_only"] is True
    assert manifest["metadata_as_of_date"] == "2026-09-18"
    assert manifest["size_filter_basis"] == "total_market_cap_proxy"
    assert manifest["candidate_count"] == 1
    assert manifest["deduplicated_count"] == 1
    assert manifest["parity_status"] == "PASS"
    assert manifest["exact_replay_candidate_ids"] == ["reference"]
    assert (output / "input_cache").is_dir()
    assert (output / "feature_cache" / "manifest.json").is_file()
    assert (output / "fast_results.json").is_file()
    assert (output / "exact_bridge.json").is_file()
