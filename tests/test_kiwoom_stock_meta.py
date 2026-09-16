from datetime import datetime, timezone

import pandas as pd
import pytest

from collector.kiwoom.stock_meta import normalize_opt10001, save_observation
from collector.daily_snapshot import read_all_snapshots

# 사용자가 KOA Studio에서 제공한 응답 중 검증에 쓰는 원문 필드.
RAW = {"종목코드": "005930", "종목명": "삼성전자", "상장주식": "5846279",
       "시가총액": "14528002", "현재가": "248500", "유통주식": "4405778",
       "유통비율": "75.4", "시가": "0", "고가": "0", "저가": "0", "거래량": "0"}


def parse(raw=None, **kwargs):
    return normalize_opt10001(RAW if raw is None else raw,
                             received_at=datetime(2026, 9, 15, 22, tzinfo=timezone.utc),
                             shares_multiplier=kwargs.get("shares_multiplier", 1000), mkt_to_eok=1)


def test_user_response_normalizes_units_without_creating_daily_prices(tmp_path):
    result = parse()
    assert result["snapshot"] == {"date": "20260916", "code": "005930", "name": "삼성전자",
                                  "shares": 5846279000, "mkt": 14528002, "float": 75.4}
    assert result["float_shares"] == 4405778000
    assert result["reasons"] == {}
    assert result["diagnostics"]["computed_float_pct"] == pytest.approx(75.3603788)
    save_observation(tmp_path, result)
    assert read_all_snapshots(tmp_path).iloc[0]["float"] == 75.4
    assert not (tmp_path / "open.csv").exists()
    assert len(list((tmp_path / "kiwoom_observations").glob("*.json"))) == 1


def test_wrong_share_unit_is_not_silently_saved():
    result = parse(shares_multiplier=1)
    assert result["snapshot"]["shares"] is None
    assert result["snapshot"]["mkt"] is None
    assert result["reasons"]["시가총액"] == "market_cap_unit_or_value_mismatch"


@pytest.mark.parametrize("value", ["", "NaN", "-1", "101"])
def test_invalid_float_is_missing_not_a_default(value):
    assert parse({**RAW, "유통비율": value})["snapshot"]["float"] is None


def test_inconsistent_float_count_rejects_ratio():
    result = parse({**RAW, "유통주식": "100"})
    assert result["snapshot"]["float"] is None
    assert result["reasons"]["유통비율"] == "float_share_ratio_mismatch"


def test_signed_price_is_magnitude_for_consistency_check():
    assert parse({**RAW, "현재가": "-248500"})["reasons"] == {}


def test_missing_values_do_not_generate_zero_or_defaults(tmp_path):
    raw = {**RAW, "상장주식": "", "시가총액": "", "유통비율": "", "유통주식": ""}
    result = parse(raw)
    assert all(result["snapshot"][k] is None for k in ["shares", "mkt", "float"])
    save_observation(tmp_path, parse())
    save_observation(tmp_path, result)
    assert read_all_snapshots(tmp_path).iloc[0].shares == 5846279000


def test_backdated_mutation_rejected(tmp_path):
    result = parse()
    result["snapshot"]["date"] = "20220425"
    with pytest.raises(ValueError, match="date"):
        save_observation(tmp_path, result)


def test_naive_receipt_time_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        normalize_opt10001(RAW, received_at=datetime(2026, 9, 16), shares_multiplier=1000, mkt_to_eok=1)
