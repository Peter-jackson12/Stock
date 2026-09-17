import subprocess
import sys

from collector.kiwoom.capture_diagnostics import CaptureDiagnostics


class Handler:
    def __init__(self):
        self.enabled = False
        self.dumps = 0
    def is_enabled(self):
        return self.enabled
    def enable(self, **kwargs):
        assert kwargs["all_threads"]
        self.enabled = True
    def disable(self):
        self.enabled = False
    def dump_traceback(self, **kwargs):
        self.dumps += 1
        kwargs["file"].write("fixture stack\n")


def test_stall_dumps_are_bounded_and_same_gap_is_not_repeated(tmp_path):
    handler = Handler()
    with CaptureDiagnostics(tmp_path, handler=handler) as diagnostics:
        for key in (None, None, 1, 1, 2, 3, 4):
            diagnostics.record_stall(key, {"queue_depth": 0})
        assert handler.dumps == 3
        path = diagnostics.path
    assert not handler.enabled
    assert path.read_text(encoding="utf-8").count('"reception_stall"') == 3


def test_real_fault_handler_in_separate_process_without_ocx(tmp_path):
    script = """
from collector.kiwoom.capture_diagnostics import CaptureDiagnostics
import faulthandler,sys
with CaptureDiagnostics(sys.argv[1]) as diagnostics:
    assert faulthandler.is_enabled()
    diagnostics.record_stall(1, {'session_id': 'fixture'})
assert not faulthandler.is_enabled()
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    files = list(tmp_path.glob("collector_fault_*.log"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "fixture" in text and "record_stall" in text


def test_existing_fault_handler_is_preserved(tmp_path):
    handler = Handler()
    handler.enabled = True
    with CaptureDiagnostics(tmp_path, handler=handler) as diagnostics:
        assert not diagnostics.owns_handler
        assert diagnostics.record_stall(1, {})
    assert handler.enabled
