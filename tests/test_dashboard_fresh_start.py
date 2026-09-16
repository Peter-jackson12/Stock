"""Test the real Streamlit entry point without pre-imported project packages."""
from pathlib import Path
import subprocess
import sys


def test_fresh_interpreter_renders_operational_entrypoint():
    root = Path(__file__).resolve().parents[1]
    program = """
import sys
from streamlit.testing.v1 import AppTest
assert 'control_tower' not in sys.modules
app = AppTest.from_file('dashboard/app.py', default_timeout=20).run()
assert not app.exception, [e.message for e in app.exception]
assert app.title[0].value == 'Stock 운영 관리'
"""
    result = subprocess.run([sys.executable, "-c", program], cwd=root, timeout=30,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout + result.stderr
