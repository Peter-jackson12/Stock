"""Streamlit interaction smoke tests using temporary synthetic data only."""
import json
from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

from collector.raw_v2 import RawV2Writer
from control_tower.jobs import JobStore
from control_tower.service import run_one_inspection
from dashboard import operations as ui


def screen(tmp_path):
    script = "from dashboard.operations import render_control_tower\nrender_control_tower(" + repr(str(tmp_path)) + ")"
    return AppTest.from_string(script, default_timeout=10).run()


def button(app, label):
    return next(item for item in app.button if item.label == label)


def enter(app, label, value):
    next(item for item in app.text_input if item.label == label).input(value)


def test_initial_screen_never_starts_worker_or_creates_jobs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ui, "start_inspection_worker", lambda: calls.append(True))
    monkeypatch.setattr(ui, "start_managed_capture", lambda *args: calls.append(True))
    app = screen(tmp_path)
    assert not app.exception
    assert app.title[0].value == "Stock 운영 관리"
    assert len(app.tabs) == 4
    assert not calls and not JobStore(tmp_path).path.exists()
    assert not ui.ManagedCaptures(tmp_path).path.exists()


def test_capture_start_requires_click_and_stop_is_durable(tmp_path, monkeypatch):
    calls = []
    def start(root, codes, duration, server):
        calls.append((codes, duration, server))
        return ui.ManagedCaptures(root).create(codes, duration, server, ["C:/fixture/python.exe"])[0]
    monkeypatch.setattr(ui, "start_managed_capture", start)
    app = screen(tmp_path)
    button(app, "소규모 수집 시작 · 로그인").click().run()
    assert not app.exception and calls == [(["005930"], 60, "mock")]
    button(app, "상태 새로고침").click().run()
    assert button(app, "소규모 수집 시작 · 로그인").disabled
    button(app, "이 관리 세션 종료 요청").click().run()
    assert ui.ManagedCaptures(tmp_path).get()["state"] == "cancelled"


def test_raw_session_panel_separates_callback_and_raw_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "observe_raw_capture", lambda root: dict(status="stale", age_seconds=100,
        payload=dict(identity=dict(session_id="fixture", dataset_path="C:/fixture/raw.db"), error=None,
        snapshot=dict(accepted_callbacks=1, committed_seq=3, pending_callbacks=0, state="closed", error=None))))
    app = screen(tmp_path)
    assert not app.exception
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["접수 콜백"] == "1" and metrics["커밋 raw 레코드"] == "3"
    assert any("미확인" in item.value for item in app.warning)
    assert not JobStore(tmp_path).path.exists()


def test_result_request_and_failed_research_warning(tmp_path, monkeypatch):
    path = tmp_path / "research_runs/fixture/result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(schema="tick_research_result_v1", status="running")), encoding="utf-8")
    calls = []
    monkeypatch.setattr(ui, "start_inspection_worker", lambda: calls.append(True) or 123)
    app = screen(tmp_path)
    enter(app, "연구 결과 경로", str(path))
    button(app, "결과 조회 요청").click().run()
    assert not app.exception and len(calls) == 1
    assert JobStore(tmp_path).recent()[0]["status"] == "queued"
    run_one_inspection(tmp_path)
    button(app, "상태 새로고침").click().run()
    assert not app.exception
    assert any("진단 자료" in item.value for item in app.warning)
    assert JobStore(tmp_path).recent()[0]["result"]["status"] == "running"


def test_invalid_request_shows_error_without_spawning(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ui, "start_inspection_worker", lambda: calls.append(True))
    app = screen(tmp_path)
    enter(app, "연구 결과 경로", "../missing.json")
    button(app, "결과 조회 요청").click().run()
    assert not app.exception and app.error
    assert not calls and not JobStore(tmp_path).path.exists()


def test_plan_can_be_saved_and_cancelled_without_any_worker(tmp_path, monkeypatch):
    path = tmp_path / "sampledata/raw_ticks_v2/fixture.db"
    with RawV2Writer(path, source="test", session_id="s", market_date="2026-09-16", feed_scope="test") as writer:
        writer.finish(close_ns=1)
    calls = []
    monkeypatch.setattr(ui, "start_inspection_worker", lambda: calls.append(True))
    app = screen(tmp_path)
    enter(app, "닫힌 raw v2 경로", str(path))
    enter(app, "편도 비용률", "0.001")
    button(app, "장외 계획 저장 · 실행 안 함").click().run()
    assert not app.exception
    assert JobStore(tmp_path).recent()[0]["status"] == "planned"
    button(app, "이 대기 작업 취소").click().run()
    assert not app.exception and not calls
    assert JobStore(tmp_path).recent()[0]["status"] == "cancelled"
    assert not (tmp_path / "research_runs").exists()


def test_app_entry_and_existing_analysis_navigation(tmp_path, monkeypatch):
    from dashboard import run_selector
    monkeypatch.setattr(ui, "ROOT", tmp_path)
    calls = []

    def stop_without_reading_production_runs():
        calls.append(True)
        st.stop()

    monkeypatch.setattr(run_selector, "get_selected_trades", stop_without_reading_production_runs)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard/app.py"), default_timeout=15).run()
    assert not app.exception and app.title[0].value == "Stock 운영 관리" and not calls
    app.sidebar.radio[0].set_value("백테스트 분석").run()
    assert not app.exception and "Analytics" in app.title[0].value and calls
