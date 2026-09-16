import pytest

from collector.kiwoom.tick_normalizer import normalize_tick


def normalize(fids, **changes):
    defaults = dict(source="kiwoom_fixture", session_id="s", seq=1, received_ns=1,
                    received_at_utc="2026-09-16T00:00:00.123456+00:00", code="005930", venue="unknown",
                    real_type="주식체결", price_policy="signed_magnitude")
    return normalize_tick(fids=fids, **(defaults | changes))


def test_turnover_never_determines_direction_and_raw_sign_is_preserved():
    source = {"10": "-10001", "15": "-30", "14": "+999999", "20": "090000"}
    result = normalize(source)
    assert result.event.price == 10001 and result.event.volume == 30
    assert result.event.is_buy is None
    assert result.raw_fields["fids"] == source
    assert "trade_direction_unverified" in result.issues
    assert result.event.market_second == 32400
    assert result.exchange_ts_raw == "090000" and result.source_time_precision == "second"


@pytest.mark.parametrize("volume,direction", [("+30", True), ("-30", False), ("30", None), ("+0", None)])
def test_signed_volume_requires_opt_in_and_explicit_nonzero_sign(volume, direction):
    result = normalize({"10": "10001", "15": volume, "14": "+9999", "20": "090000"}, direction_policy="signed_volume")
    assert result.event.is_buy is direction


def test_price_sign_policy_is_explicit():
    result = normalize({"10": "-10001", "15": "30"}, price_policy="positive_only")
    assert result.event.price is None


@pytest.mark.parametrize("value", ["", "NaN", "1.5", "1,000", None])
def test_bad_numeric_field_is_null_with_reason(value):
    result = normalize({"10": value, "15": "-30"})
    assert result.event.price is None and "invalid_or_missing_fid_10" in result.issues


def test_quote_depth_and_missing_size_never_become_fake_zero():
    fids = {"21": "085959", "41": "+10001", "51": "10000",
            **{str(i): "10" for i in range(61, 81)}}
    result = normalize(fids, real_type="주식호가잔량")
    assert result.event.ask_sizes == (10,) * 10
    assert result.event.ask == 10001
    del fids["61"]
    missing = normalize(fids, real_type="주식호가잔량")
    assert missing.event.ask_size is None and missing.event.ask_sizes is None


def test_missing_deep_level_keeps_valid_top_three_and_raw_fields():
    fids = {"41": "10001", "51": "10000", "21": "090000",
            **{str(i): "10" for i in (61, 62, 63, 71, 72, 73)}}
    result = normalize(fids, real_type="주식호가잔량")
    assert result.event.bid_sizes == (10, 10, 10)
    assert "64" not in result.raw_fields["fids"]


def test_receipt_and_exchange_times_are_not_conflated():
    result = normalize({"10": "10001", "15": "30", "20": "085959"})
    assert result.event.market_second == 32400 and result.exchange_ts_raw == "085959"


def test_unknown_event_and_missing_timezone_rejected():
    with pytest.raises(ValueError):
        normalize({}, real_type="접속종료")
    with pytest.raises(ValueError):
        normalize({}, received_at_utc="2026-09-16T09:00:00")


def test_normalized_fids_to_raw_v2_to_research_result(tmp_path):
    import json
    from collector.raw_v2 import RawV2Writer
    from engine.tick_research_run import run_raw_v2
    q = normalize({"21": "085959", "41": "10001", "51": "10000",
                   **{str(i): "3" for i in range(61, 71)}, **{str(i): "10" for i in range(71, 81)}},
                  real_type="주식호가잔량", received_ns=0,
                  received_at_utc="2026-09-15T23:59:59+00:00")
    t = normalize({"20": "090000", "10": "10001", "15": "+30", "14": "1234"},
                  seq=2, direction_policy="signed_volume")
    path = tmp_path / "normalized.db"
    with RawV2Writer(path, source="kiwoom_fixture", session_id="s", market_date="2026-09-16", feed_scope="fixture") as w:
        for packet in (q, t):
            w.append(packet.event, received_at_utc=packet.received_at_utc, raw_fields=packet.raw_fields,
                     exchange_ts_raw=packet.exchange_ts_raw, source_time_precision=packet.source_time_precision)
        w.finish(close_ns=20)
    config = dict(source="kiwoom_fixture", session_id="s", code="005930", venue="unknown", cash=100000,
                  buy_latency_ns=5, sell_latency_ns=5, cancel_latency_ns=2, max_quote_age_ns=100, fee_rate="0.001")
    output = run_raw_v2(path, output_root=tmp_path / "results", simulator_config=config, quantity=2)
    saved = json.loads(output.read_bytes())
    assert saved["status"] == "completed_with_open_position"
    assert saved["fills"][0]["quantity"] == 2
