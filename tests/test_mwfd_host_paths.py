from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from scripts.materialize_mwfd_04_events import local_to_win, win_to_local

WIN = "C:\\Projects\\TotalStock\\StockSnapshots\\exploratory_profitability_20260921_01\\summary.jsonl"
LINUX = "/sessions/rcw-01fdfe2equdn5tenjzpcmaww/mnt/TotalStock/StockSnapshots/exploratory_profitability_20260921_01/summary.jsonl"
REL = ("StockSnapshots", "exploratory_profitability_20260921_01", "summary.jsonl")


@pytest.mark.parametrize("host_os", ["nt", "posix"])
def test_linux_vm_mount_path_resolves_under_root_on_any_host(tmp_path, host_os):
    assert win_to_local(LINUX, tmp_path, host_os=host_os) == tmp_path.joinpath(*REL)


def test_linux_vm_mount_path_ignores_session_id(tmp_path):
    other = LINUX.replace("rcw-01fdfe2equdn5tenjzpcmaww", "rcw-another-session")
    assert win_to_local(other, tmp_path, host_os="nt") == win_to_local(LINUX, tmp_path, host_os="nt")


def test_windows_path_behaviour_is_unchanged(tmp_path):
    assert win_to_local(WIN, tmp_path, host_os="nt") == Path(WIN)
    assert win_to_local(WIN, tmp_path, host_os="posix") == tmp_path.joinpath(*REL)


@pytest.mark.parametrize("path", ["relative/file.json", "/opt/other/file.json", "D:\\elsewhere\\file.json"])
def test_unrelated_paths_are_returned_as_is(tmp_path, path):
    assert win_to_local(path, tmp_path, host_os="nt") == Path(path)


@pytest.mark.parametrize("path", [
    "/sessions/x/mnt/TotalStock/../secret.json",
    "/sessions/x/mnt/TotalStock/a//b.json",
    "C:\\Projects\\TotalStock\\..\\secret.json",
])
def test_paths_escaping_root_are_rejected(tmp_path, path):
    with pytest.raises(ValueError, match="unsafe"):
        win_to_local(path, tmp_path, host_os="posix")


def test_local_to_win_records_windows_notation_from_posix_host(tmp_path):
    target = tmp_path.joinpath(*REL)
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    recorded = local_to_win(target, tmp_path, host_os="posix")
    assert recorded == WIN
    assert PureWindowsPath(recorded).parts[-3:] == REL
    assert win_to_local(recorded, tmp_path, host_os="posix") == target


def test_linux_manifest_value_of_mwfd04_run_is_resolvable(tmp_path):
    """MWFD-04 run_manifest.json에 남은 Linux 경로는 수정 없이 Windows에서도 해석된다."""
    resolved = win_to_local(LINUX, tmp_path, host_os="nt")
    assert PurePosixPath(*resolved.relative_to(tmp_path).parts) == PurePosixPath(*REL)
