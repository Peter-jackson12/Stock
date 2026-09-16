import sys

from scripts import verify_phase_a as verify


def test_default_verifies_baseline_without_requiring_historical_ticks(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["verify_phase_a.py"])
    monkeypatch.setattr(verify, "RESULTS", [])
    monkeypatch.setattr(verify, "verify_bar_engine", lambda *args: verify.check("bar", True))
    def unexpected(*args):
        raise AssertionError("default must not require a tick date")
    monkeypatch.setattr(verify, "verify_tick_engine", unexpected)
    assert verify.main() == 0
    assert "틱 검증 미실행" in capsys.readouterr().out


def test_explicit_tick_date_is_checked_and_failure_stays_failure(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["verify_phase_a.py", "--date", "20260916", "--code", "005930"])
    monkeypatch.setattr(verify, "RESULTS", [])
    monkeypatch.setattr(verify, "verify_bar_engine", lambda *args: verify.check("bar", True))
    def tick(root, date, code):
        assert (date, code) == ("20260916", "005930")
        verify.check("missing requested input", False)
    monkeypatch.setattr(verify, "verify_tick_engine", tick)
    assert verify.main() == 1
