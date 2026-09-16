import json

import pandas as pd
import pytest
import requests

from collector import daily_collector as collector
from collector.daily_snapshot import read_all_snapshots
from tests.test_daily_snapshot import mock_source


def response(text="", status=200):
    result = requests.Response()
    result.status_code = status
    result._content = text.encode("utf-8")
    result.encoding = "utf-8"
    return result


@pytest.mark.parametrize("payload", ["<broken", '<chart><item data="20260916|bad|1|1|1|1"/></chart>'])
def test_candle_parse_failure_does_not_block_current_meta(monkeypatch, payload):
    def get(url, **kwargs):
        return response(payload if "fchart" in url else "상장주식수<em>1,234</em>")
    monkeypatch.setattr(collector, "_get", get)
    monkeypatch.setattr(collector, "observation_date", lambda: "20260916")
    _, frame, meta = collector.fetch_stock_meta_and_candles("005930")
    assert frame.empty
    assert meta["candles_reason"].startswith("candle_fetch_failed")
    assert meta["shares"] == 1234
    assert meta["observed_date"] == "20260916"


def test_candle_timeout_does_not_block_meta_and_retries_are_bounded(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if "fchart" in url:
            raise requests.Timeout("offline")
        return response("상장주식수<em>123</em>")
    monkeypatch.setattr(collector.requests, "get", get)
    monkeypatch.setattr(collector.time, "sleep", lambda _: None)
    _, frame, meta = collector.fetch_stock_meta_and_candles("005930")
    assert frame.empty and meta["shares"] == 123
    assert sum("fchart" in url for url in calls) == 3
    assert len(calls) == 4


@pytest.mark.parametrize("status, expected", [(403, 1), (429, 3), (503, 3)])
def test_http_retry_policy(monkeypatch, status, expected):
    calls = []
    def get(*args, **kwargs):
        calls.append(1)
        return response(status=status)
    monkeypatch.setattr(collector.requests, "get", get)
    monkeypatch.setattr(collector.time, "sleep", lambda _: None)
    with pytest.raises(requests.HTTPError):
        collector._get("https://example.invalid", timeout=1)
    assert len(calls) == expected


def test_mid_batch_interrupt_preserves_completed_observation_and_journal(tmp_path, monkeypatch):
    mock_source(monkeypatch, ["20260916"])
    original = collector.fetch_stock_meta_and_candles
    def fetch(code, **kwargs):
        if code == "000660":
            raise KeyboardInterrupt()
        return original(code, **kwargs)
    monkeypatch.setattr(collector, "fetch_stock_meta_and_candles", fetch)
    with pytest.raises(KeyboardInterrupt):
        collector.FastDailyCollector(tmp_path).collect("20260916", "20260916", ["005930", "000660"])
    saved = read_all_snapshots(tmp_path)
    assert list(saved.code) == ["005930"]
    assert saved.iloc[0].shares == 100
    journal = next((tmp_path / "collection_runs").glob("*.jsonl"))
    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in events] == ["started", "observed"]
    assert events[-1]["reasons"]["float_reason"] == "no_crawler_implemented"

    # 재실행은 기존 파일을 유지하고 이후 종목을 추가한다. TR 요청 생략 기능은 별개다.
    monkeypatch.setattr(collector, "fetch_stock_meta_and_candles", original)
    collector.FastDailyCollector(tmp_path).collect("20260916", "20260916", ["005930", "000660"])
    assert set(read_all_snapshots(tmp_path).code) == {"005930", "000660"}
    assert len(list((tmp_path / "collection_runs").glob("*.jsonl"))) == 2
    assert journal.read_text(encoding="utf-8").splitlines() == [json.dumps(e, ensure_ascii=False) for e in events]


def test_price_export_failure_does_not_lose_collected_snapshot(tmp_path, monkeypatch):
    mock_source(monkeypatch, ["20260916"])
    original = pd.DataFrame.to_csv
    def to_csv(self, path, *args, **kwargs):
        if "unverified_fchart" in str(path):
            from core.price_policy import PRICE_MANIFEST
            marker = json.loads((tmp_path / "unverified_fchart" / PRICE_MANIFEST).read_text())
            assert marker["price_basis"] == "split_adjusted_observed"
            raise OSError("disk error during derived export")
        return original(self, path, *args, **kwargs)
    monkeypatch.setattr(pd.DataFrame, "to_csv", to_csv)
    with pytest.raises(OSError):
        collector.FastDailyCollector(tmp_path).collect("20260916", "20260916", ["005930"])
    assert read_all_snapshots(tmp_path).iloc[0].shares == 100
