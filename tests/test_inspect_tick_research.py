import json

import pytest

from scripts import inspect_tick_research as reader


def save(tmp_path, **changes):
    result = dict(schema="tick_research_result_v1", status="completed_with_open_position",
                  event_count=2, open_quantity=1, final_cash="100", orders=[{}], fills=[{}], signals=[{}],
                  input_complete=True, diagnostics_only=False, error=None)
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result | changes), encoding="utf-8")
    return path


def test_summary_does_not_infer_profit_or_closed_trades(tmp_path):
    summary = reader.inspect(save(tmp_path))
    assert summary["fills_count"] == 1 and summary["open_quantity"] == 1
    assert "pnl" not in summary and "trades_count" not in summary


@pytest.mark.parametrize("status", ["running", "failed"])
def test_failed_and_running_exit_nonzero(tmp_path, status):
    assert reader.main([str(save(tmp_path, status=status))]) == 2


@pytest.mark.parametrize("changes", [{"event_count": True}, {"final_cash": "NaN"},
                                     {"schema": "other"}, {"status": "OK"}, {"fills": {}}])
def test_invalid_result_rejected(tmp_path, changes):
    assert reader.main([str(save(tmp_path, **changes))]) == 3


def test_size_limit_uses_bounded_read(tmp_path, monkeypatch):
    path = save(tmp_path)
    monkeypatch.setattr(reader, "MAX_BYTES", 10)
    assert reader.main([str(path)]) == 3


def test_missing_file_is_not_created(tmp_path):
    path = tmp_path / "missing.json"
    assert reader.main([str(path)]) == 3
    assert not path.exists()


@pytest.mark.parametrize("status", ["completed_flat", "completed_empty_input", "completed_no_fills"])
def test_contradictory_completion_status_rejected(tmp_path, status):
    assert reader.main([str(save(tmp_path, status=status))]) == 3


@pytest.mark.parametrize("field,value", [
    ("input_complete", False), ("input_complete", None), ("input_complete", 1),
    ("diagnostics_only", True), ("diagnostics_only", None), ("diagnostics_only", 0),
    ("error", "checksum mismatch"), ("error", ""), ("error", False),
])
def test_completion_requires_explicit_success_evidence(tmp_path, field, value):
    assert reader.main([str(save(tmp_path, **{field: value}))]) == 3


@pytest.mark.parametrize("field", ["input_complete", "diagnostics_only", "error"])
def test_completion_missing_evidence_is_invalid(tmp_path, field):
    path = save(tmp_path)
    data = json.loads(path.read_bytes())
    del data[field]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert reader.main([str(path)]) == 3


@pytest.mark.parametrize("status,counts", [
    ("completed_empty_input", dict(event_count=0, open_quantity=0, orders=[], fills=[], signals=[])),
    ("completed_no_selected_events", dict(event_count=0, open_quantity=0, orders=[], fills=[], signals=[])),
    ("completed_no_fills", dict(open_quantity=0, fills=[])),
    ("completed_with_open_position", {}),
    ("completed_flat", dict(open_quantity=0)),
])
def test_all_completion_outcomes_require_success_evidence(tmp_path, status, counts):
    assert reader.main([str(save(tmp_path, status=status, **counts))]) == 0
    assert reader.main([str(save(tmp_path, status=status, input_complete=False, **counts))]) == 3


@pytest.mark.parametrize("status,changes", [
    ("completed_no_fills", dict(open_quantity=1, fills=[])),
    ("completed_empty_input", dict(event_count=0, open_quantity=0, orders=[], fills=[{}], signals=[])),
    ("completed_empty_input", dict(event_count=0, open_quantity=0, orders=[{}], fills=[], signals=[])),
    ("completed_no_selected_events", dict(event_count=0, open_quantity=0, orders=[], fills=[], signals=[{}])),
    ("completed_with_open_position", dict(open_quantity=1, fills=[])),
])
def test_cross_field_completion_contradictions_are_rejected(tmp_path, status, changes):
    assert reader.main([str(save(tmp_path, status=status, **changes))]) == 3
