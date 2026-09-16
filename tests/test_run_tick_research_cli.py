from contextlib import contextmanager
from decimal import Decimal

import pytest

from scripts import run_tick_research as cli


def args():
    return ["--db", "fixture.db", "--output-root", "research-results", "--code", "005930",
            "--venue", "unknown", "--quantity", "2", "--cash", "100000", "--fee-rate", "0.001",
            "--buy-latency-sec", "0.5", "--sell-latency-sec", "1", "--cancel-latency-sec", "0.1",
            "--max-quote-age-sec", "2"]


def test_cli_passes_explicit_costs_and_converted_times(monkeypatch, capsys):
    captured = {}
    @contextmanager
    def reader(path):
        yield {"source": "test", "session_id": "s"}, iter(())
    def run(path, **kwargs):
        captured.update(kwargs)
        return "fixture-result.json"
    monkeypatch.setattr(cli, "read_raw_v2", reader)
    monkeypatch.setattr(cli, "run_raw_v2", run)
    assert cli.main(args()) == 0
    config = captured["simulator_config"]
    assert config["fee_rate"] == Decimal("0.001")
    assert config["buy_latency_ns"] == 500_000_000
    assert config["sell_latency_ns"] == 1_000_000_000
    assert config["cancel_latency_ns"] == 100_000_000
    assert config["max_quote_age_ns"] == 2_000_000_000
    assert "fixture-result.json" in capsys.readouterr().out


@pytest.mark.parametrize("flag,value", [("--fee-rate", "NaN"), ("--fee-rate", "1"),
    ("--cash", "-1"), ("--quantity", "0"), ("--buy-latency-sec", "0.0000000001"),
    ("--sell-latency-sec", "Infinity"), ("--venue", " ")])
def test_invalid_config_rejected_before_open(monkeypatch, flag, value):
    def unexpected(*a, **k):
        pytest.fail("invalid CLI inputs must not open any DB")
    monkeypatch.setattr(cli, "read_raw_v2", unexpected)
    values = args()
    values[values.index(flag) + 1] = value
    with pytest.raises(SystemExit) as exc:
        cli.main(values)
    assert exc.value.code == 2


def test_reader_failure_returns_nonzero(monkeypatch, capsys):
    def failing(path):
        raise ValueError("incomplete raw dataset")
    monkeypatch.setattr(cli, "read_raw_v2", failing)
    assert cli.main(args()) == 2
    assert "incomplete" in capsys.readouterr().err


def test_missing_required_cost_has_no_zero_default():
    values = args()
    offset = values.index("--fee-rate")
    del values[offset:offset + 2]
    with pytest.raises(SystemExit) as exc:
        cli.main(values)
    assert exc.value.code == 2
