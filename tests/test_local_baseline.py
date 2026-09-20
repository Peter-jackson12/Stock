"""장외에 명시적으로 선택하는 기존 Phase A 실데이터 검증."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.local_data
def test_phase_a_frozen_lob_baseline():
    root = Path(__file__).resolve().parents[1]
    dates = ("20220425", "20220426", "20220427", "20220428", "20220429",
             "20220502", "20220503", "20220504")
    daily = ("close", "float", "high", "key", "low", "mkt", "open", "shares", "tradamt")
    required = [root / "sampledata/old_data/temp" / f"{date}_LOB.db" for date in dates]
    required += [root / "sampledata/Daily_baseline" / f"{name}.csv" for name in daily]
    required.append(root / "results/cross_Today_1.csv")
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    assert not missing, "local_data 필수 입력 누락 (백테스트 미실행):\n" + "\n".join(missing)
    result = subprocess.run(
        [sys.executable, "scripts/verify_phase_a.py"], cwd=root,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr
