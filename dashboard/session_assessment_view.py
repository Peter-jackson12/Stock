"""Read-only assessment component. No jobs, process queries or control actions."""
import streamlit as st

from control_tower.session_assessment import assess_session

LABELS = {
    "unverified": "미확인", "active": "활성 보고", "draining": "저장 마무리 보고",
    "closed": "저장 닫힘 보고", "interrupted": "중단 보고", "failed": "실패 보고",
    "alive": "존재 관측", "absent": "부재 관측", "access_denied": "조회 접근 거부",
    "mismatch": "식별 불일치", "clear": "관련 창 없음 관측", "runtime_error": "Runtime 오류 창 관측",
    "ocx_window_present": "OCX 창 존재 관측", "held": "잠금 보유 관측", "free": "잠금 확보 가능 관측",
    "recent": "최근 갱신", "stale": "오래된 갱신", "clock_ahead": "시각 이상",
    "fresh": "원천 시각 별도 확인", "lagging": "원천 시각 차이 증가", "stopped": "수신 정지 관측",
    "diagnostic_only": "진단 전용 · 연구 입력 제외", "ineligible": "연구 부적격",
    "exit_observed": "종료 관측 · 새 실행 승인은 아님", "residual_native": "저장 닫힘 뒤 native 잔류 관측",
    "native_error": "native 오류 관측", "process_alive": "프로세스 존재 관측",
    "process_absent_native_unverified": "프로세스 부재 · native 미확인", "contradictory": "근거 충돌",
}


def render_session_assessment(raw, log, *, now=None):
    assessment = assess_session(raw, log, now=now)
    st.subheader("수집 상태 근거")
    st.caption("세션: " + (assessment.session_id or "미확인") + " · 상태 보고 시각: "
               + (assessment.reported_at_utc or "미확인") + " · " + LABELS[assessment.status_recency])
    axes = [("저장 보고", assessment.storage), ("프로세스 관측", assessment.process),
            ("native 창 관측", assessment.native_ui), ("잠금 관측", assessment.lease),
            ("세션 콜백 진행", assessment.activity), ("원천 시각 신선도", assessment.source_freshness),
            ("연구 입력 적격성", assessment.research)]
    for offset in (0, 4):
        chunk = axes[offset:offset + 4]
        for column, (label, value) in zip(st.columns(len(chunk)), chunk):
            column.metric(label, LABELS[value])
    st.caption("종료 근거: " + LABELS[assessment.termination])
    st.caption("일일 공용 로그 갱신: " + LABELS[assessment.log_recency]
               + " · 이 로그는 세션을 식별하지 않으며 누적 계수 재출력도 포함합니다.")
    st.caption("상태 보고와 로그 갱신은 현재 콜백 진행·원천 시각·OS 종료를 인증하지 않습니다. "
               "이 표시는 시작/재시작/연구 실행 권한에 연결되지 않습니다.")
    if assessment.reported_storage and assessment.storage == "unverified":
        st.warning("마지막 보고 상태 " + assessment.reported_storage + "를 현재 상태로 승격하지 않았습니다.")
    if assessment.research == "diagnostic_only":
        st.warning("진단 전용 feed_scope입니다. 저장이 닫혔어도 연구 입력으로 사용하지 않습니다.")
    if assessment.issues:
        st.warning("근거 확인 필요: " + ", ".join(assessment.issues))
