import json

import pytest

from core.runstore import RunStore
from engine.nxt_session import NxtUnverifiedError
from engine.nxt_tick_engine import NextradeTickEngine


def engine(tmp_path):
    e = NextradeTickEngine.__new__(NextradeTickEngine)
    e.code, e.date_str = "005930", "20260917"
    e.venue_resolution, e.nxt_coverage = "per_event_venue", "unconfirmed"
    e.allow_unverified_nxt = False
    e.run_store = RunStore(tmp_path)
    e.load_and_preprocess = lambda: [dict(sec=32400, price=10000, vol=1,
        venue="KRX", bid_p1=9900, ask_p1=10100)]
    return e


def test_default_blocks_and_explicit_bypass_persists_even_without_trades(tmp_path, capsys):
    e = engine(tmp_path)
    with pytest.raises(NxtUnverifiedError):
        e.run_strategy()
    assert not list(tmp_path.rglob("manifest.json"))
    e.allow_unverified_nxt = True
    key = e.run_strategy()
    manifest = json.loads((e.run_store.run_dir(key) / "manifest.json").read_text(encoding="utf-8"))
    guard = manifest["params"]["nxt_guard"]
    assert guard["allow_unverified"] is True
    assert guard["status"] == "unverified" and guard["exhausted"] is None
    assert guard["nxt_coverage"] == "unconfirmed" and guard["reason"]
    assert "NXT 미검증" in capsys.readouterr().out
