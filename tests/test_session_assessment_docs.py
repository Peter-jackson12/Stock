"""Structural navigation checks for the dedicated assessment contract."""
from pathlib import Path

from tests.test_documentation import LINK, outside_fences, link_errors

ROOT = Path(__file__).resolve().parents[1]


def test_assessment_contract_is_connected_from_both_entrypoints():
    for entry in ("README.md", "HANDOFF.md"):
        targets = LINK.findall(outside_fences((ROOT / entry).read_text(encoding="utf-8")))
        assert "docs/SESSION_ASSESSMENT.md" in targets
    assert not link_errors(ROOT, "docs/SESSION_ASSESSMENT.md")


def test_contract_points_to_implementation_and_regressions():
    targets = set(LINK.findall(outside_fences(
        (ROOT / "docs/SESSION_ASSESSMENT.md").read_text(encoding="utf-8"))))
    assert {"../control_tower/session_assessment.py", "../control_tower/status.py",
            "../dashboard/session_assessment_view.py", "../tests/test_session_assessment.py",
            "../tests/test_session_assessment_ui.py"} <= targets
