import pytest

from collector.kiwoom.nxt_probe import observation, prepare_plan


def test_plan_is_offline_and_retains_suffixes_after_production_cutoff():
    plan = prepare_plan("005930", server="mock", start_at="2026-09-18T15:40:00+09:00")
    assert plan["subscription_codes"] == ["005930", "005930_NX", "005930_AL"]
    assert not plan["production_cutoff_compatible"]
    assert plan["execution"] == "offline_plan_only"
    assert plan["nxt_coverage"] == "unconfirmed"


@pytest.mark.parametrize("code", ["005930_NX", "005930_AL", "005930", "unexpected"])
def test_observation_preserves_raw_without_certifying_venue(code):
    fids = {"15": " -30 ", "10": "+100", "extra": "original"}
    result = observation(subscription_code="005930_AL", callback_code=code,
                         real_type="unexpected_type", fids=fids,
                         received_at_utc="2026-09-18T06:40:00Z", received_ns=1,
                         session_id="fixture", server="mock")
    assert result["callback_code_raw"] == code
    assert result["subscription_code_raw"] == "005930_AL"
    assert result["real_type_raw"] == "unexpected_type"
    assert result["fids"] == fids
    fids["15"] = "mutated"
    assert result["fids"]["15"] == " -30 "
    assert result["receipt_market_period"] == "aftermarket"
    assert result["venue"] == "unknown" and result["nxt_coverage"] == "unconfirmed"


@pytest.mark.parametrize("changes", [dict(base_code="005930_NX"), dict(server="guess"),
    dict(duration_seconds=301), dict(start_at="2026-09-18T15:40:00")])
def test_invalid_plan_is_rejected(changes):
    with pytest.raises(ValueError):
        prepare_plan(**(dict(base_code="005930", server="mock",
                            start_at="2026-09-18T15:40:00+09:00") | changes))
