from types import SimpleNamespace

import pytest

from control_tower.storage_guard import require_disk_space, MIN_FREE_BYTES
from control_tower import managed_capture, offline_worker


def test_reserve_boundary_and_no_files_written(tmp_path):
    assert require_disk_space(tmp_path, disk_usage=lambda p: SimpleNamespace(free=MIN_FREE_BYTES)) == MIN_FREE_BYTES
    with pytest.raises(OSError):
        require_disk_space(tmp_path, disk_usage=lambda p: SimpleNamespace(free=MIN_FREE_BYTES - 1))
    assert list(tmp_path.iterdir()) == []
    observed = []
    require_disk_space(tmp_path / "new" / "session", disk_usage=lambda p: observed.append(p) or SimpleNamespace(free=MIN_FREE_BYTES))
    assert observed == [tmp_path] and list(tmp_path.iterdir()) == []


def test_low_space_prevents_launch_intent_and_spawn(tmp_path, monkeypatch):
    def low(root):
        raise OSError("no space")
    monkeypatch.setattr(managed_capture, "require_disk_space", low)
    calls = []
    with pytest.raises(OSError):
        managed_capture.start_managed_capture(tmp_path, ["005930"], 60, "mock", popen=lambda *a, **k: calls.append(True))
    assert not calls and not managed_capture.ManagedCaptures(tmp_path).path.exists()
    monkeypatch.setattr(offline_worker, "require_disk_space", low)
    with pytest.raises(OSError):
        offline_worker.require_offline(tmp_path)
