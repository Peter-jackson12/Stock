import json
import subprocess

from scripts import probe_replay_scheduler as probe


def test_cleanup_timeout_preserves_probe_evidence(tmp_path, monkeypatch):
    (tmp_path / ".venv/Scripts").mkdir(parents=True)
    (tmp_path / ".venv/Scripts/pythonw.exe").write_bytes(b"fixture")
    (tmp_path / ".venv/pyvenv.cfg").write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    monkeypatch.setattr(probe.sys, "argv", ["probe", "--execute"])
    monkeypatch.setattr(probe, "schedule_replay", lambda *args: None)
    calls = []

    def timeout(args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, 10)

    monkeypatch.setattr(probe.subprocess, "run", timeout)
    assert probe.main() == 2
    saved = list((tmp_path / "operations_state/scheduler_probes").glob("*/result.json"))
    assert len(saved) == 1
    result = json.loads(saved[0].read_text(encoding="utf-8"))
    assert result["registered"] and not result["task_removed"]
    assert "TimeoutExpired" in result["error"]
    assert "TimeoutExpired" in result["cleanup_error"]
    assert calls[0][1] == "/Run" and calls[1][1] == "/Delete"
    assert result["finished_at_utc"]
