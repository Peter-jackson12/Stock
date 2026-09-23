"""Render the new component against synthetic data only; no control actions."""
from streamlit.testing.v1 import AppTest

from tests.test_session_assessment import IDENTITY, NOW, raw
from dataclasses import replace
from control_tower.session_assessment import DIAGNOSTIC_SCOPE


def screen(value, log):
    script = ("from dashboard.session_assessment_view import render_session_assessment\n"
              "from datetime import datetime\n"
              f"render_session_assessment({value!r}, {log!r}, now=datetime.fromisoformat({NOW.isoformat()!r}))")
    return AppTest.from_string(script, default_timeout=10).run()


def test_recent_log_not_rendered_as_callback_or_source_health():
    app = screen(raw(), dict(status="recent", heartbeat=dict(trades=3, quotes=0)))
    assert not app.exception and not app.button
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["세션 콜백 진행"] == metrics["원천 시각 신선도"] == "미확인"
    assert metrics["저장 보고"] == "저장 닫힘 보고"
    assert any("누적 계수 재출력" in item.value for item in app.caption)


def test_diagnostic_raw_gets_visible_exclusion_warning():
    app = screen(raw(identity=replace(IDENTITY, feed_scope=DIAGNOSTIC_SCOPE)), {})
    assert not app.exception
    assert any("연구 입력으로 사용하지 않습니다" in item.value for item in app.warning)
    assert {item.label: item.value for item in app.metric}["연구 입력 적격성"] == "진단 전용 · 연구 입력 제외"


def test_stale_running_status_and_missing_input_stay_unknown():
    app = screen(raw("running", age=3600), {})
    assert not app.exception
    assert {item.label: item.value for item in app.metric}["저장 보고"] == "미확인"
    assert any("현재 상태로 승격하지" in item.value for item in app.warning)
    empty = screen({}, {})
    assert not empty.exception and all(item.value == "미확인" for item in empty.metric)
