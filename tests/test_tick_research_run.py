import json
from dataclasses import replace

import pytest

from engine.tick_ordering import OrderedTick
from engine.tick_research_run import run_research, ResearchRunFailed


def events():
    q = OrderedTick("test", "s", 1, 0, "005930", "unknown", "quote",
                    bid=10000, ask=10001, bid_size=10, ask_size=3,
                    market_second=32399, bid_sizes=(10, 10, 10), ask_sizes=(3, 3, 3))
    return [q, replace(q, seq=2, received_ns=1, kind="trade", market_second=32400,
                       price=10001, volume=30, is_buy=True)]


def run(tmp_path, stream=None, **changes):
    config = dict(source="test", session_id="s", code="005930", venue="unknown", cash=100000,
                  max_quote_age_ns=100, buy_latency_ns=5, sell_latency_ns=5,
                  cancel_latency_ns=2, fee_rate="0.001")
    args = dict(output_root=tmp_path, dataset_label="synthetic", simulator_config=config,
                close_ns=20, quantity=2)
    return run_research(events() if stream is None else stream, **(args | changes))


def test_save_open_position_fills_and_explicit_cost_settings(tmp_path):
    path = run(tmp_path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["status"] == "completed_with_open_position"
    assert saved["open_quantity"] == 2 and saved["input_complete"]
    assert saved["fills"][0]["price"] == "10001"
    assert saved["fills"][0]["fee"] == "20.002"
    assert saved["settings"]["simulator"]["fee_rate"] == "0.001"
    assert saved["raw_identity_verified"] is False
    assert len(saved["code_sha256"]) == 6


def test_reruns_preserve_previous_files_and_have_same_reproducibility_key(tmp_path):
    first = run(tmp_path)
    original = first.read_bytes()
    second = run(tmp_path)
    assert first != second and first.read_bytes() == original
    a, b = json.loads(original), json.loads(second.read_bytes())
    assert a["reproducibility_key"] == b["reproducibility_key"]
    assert a["fills"] == b["fills"]


def test_changed_inputs_change_identity(tmp_path):
    first = json.loads(run(tmp_path).read_bytes())
    changed = events()
    changed[-1] = replace(changed[-1], volume=31)
    second = json.loads(run(tmp_path, changed).read_bytes())
    assert first["event_sha256"] != second["event_sha256"]
    assert first["reproducibility_key"] != second["reproducibility_key"]


def test_partial_failure_is_saved_as_diagnostics_and_raised(tmp_path):
    source = events() + [replace(events()[-1], seq=3, received_ns=7, session_id="wrong")]
    with pytest.raises(ResearchRunFailed) as caught:
        run(tmp_path, source)
    saved = json.loads(caught.value.path.read_bytes())
    assert saved["status"] == "failed" and saved["diagnostics_only"]
    assert not saved["input_complete"]
    assert "TickOrderError" in saved["error"]


@pytest.mark.parametrize("stream,status", [([], "completed_empty_input"),
                                          (events()[:1], "completed_no_fills")])
def test_empty_and_no_fill_outcomes_are_distinct(tmp_path, stream, status):
    saved = json.loads(run(tmp_path, stream).read_bytes())
    assert saved["status"] == status


def test_close_boundary_violation_is_failed_not_silently_truncated(tmp_path):
    with pytest.raises(ResearchRunFailed):
        run(tmp_path, close_ns=1)


def test_missing_fee_configuration_rejected_before_creating_output(tmp_path):
    with pytest.raises(ValueError, match="fee_rate"):
        run(tmp_path, simulator_config={})
    assert list(tmp_path.iterdir()) == []
