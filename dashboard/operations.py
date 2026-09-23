"""Operations UI; distinct module name avoids shadowing the control_tower package."""
from pathlib import Path
from datetime import datetime
import sqlite3

import streamlit as st

from control_tower.jobs import JobStore
from control_tower.service import queue_inspection, plan_replay, start_inspection_worker
from control_tower.status import observe_collector, observe_raw_capture
from control_tower.session_assessment import assess_session
from control_tower.managed_capture import ManagedCaptures, start_managed_capture, ACTIVE
from control_tower.offline_worker import start_replay_worker, retry_replay_worker
from control_tower.capture_health import CaptureHealth
from control_tower.replay_schedule import schedule_replay, schedules, expire_missed

ROOT = Path(__file__).resolve().parents[1]
JOB_STATUS = {"planned": "장외 계획", "queued": "실행 대기", "running": "실행 중 · 상태 확인 필요 시 이력 보존",
              "succeeded": "작업 완료", "failed": "작업 실패", "cancelled": "취소됨"}
ASSESSMENT_LABEL = {
    "unverified": "미확인", "active": "활성", "draining": "저장 마무리", "closed": "저장 닫힘",
    "interrupted": "중단", "failed": "실패", "alive": "프로세스 존재", "absent": "프로세스 부재",
    "access_denied": "접근 거부", "mismatch": "식별 불일치", "clear": "관련 창 없음",
    "runtime_error": "Runtime 오류 창", "ocx_window_present": "OCX 창 존재", "held": "lease 보유",
    "free": "lease 해제", "recent": "최근 수신", "stale": "오래된 수신", "clock_ahead": "시각 이상",
    "lagging": "지연 증가", "stopped": "수신 정지", "diagnostic_only": "진단 전용",
    "ineligible": "연구 부적격", "eligible": "연구 적격", "verified_exited": "종료 확인",
    "residual_native": "native 잔류", "process_alive": "프로세스 존재",
    "process_absent_native_unverified": "프로세스 부재·native 미확인", "contradictory": "근거 충돌",
}


def render_control_tower(root=None):
    root = Path(root) if root is not None else ROOT
    st.title("Stock 운영 관리")
    st.caption("수집 관측 · 작업 이력 · 틱 연구 결과를 한곳에서 확인합니다.")
    if st.button("상태 새로고침", key="control_refresh"):
        st.rerun()
    observation = observe_collector(root)
    raw = observe_raw_capture(root)
    if "payload" in raw:
        payload = raw["payload"]
        snapshot = payload["snapshot"]
        st.subheader("raw v2 수집 세션")
        columns = st.columns(3)
        columns[0].metric("접수 콜백", f"{snapshot['accepted_callbacks']:,}")
        columns[1].metric("커밋 raw 레코드", f"{snapshot['committed_seq']:,}")
        columns[2].metric("미커밋 콜백", f"{snapshot['pending_callbacks']:,}")
        st.caption(f"보고 상태 {snapshot['state']} · 갱신 경과 {raw['age_seconds']}초 · 세션 {payload['identity']['session_id']}")
        st.text(payload["identity"]["dataset_path"])
        if payload["error"] or snapshot["state"] in ("failed", "interrupted"):
            st.error(f"수집 오류/중단: {payload['error'] or snapshot['error']}")
        elif raw["status"] != "recent":
            st.warning("상태 파일이 오래됐거나 시각 확인이 필요합니다. 현재 수집기 생존은 미확인입니다.")
        st.caption("콜백 수와 raw 수는 다릅니다. 상태 파일은 관측 자료이며 프로세스 생존·데이터 품질 인증이 아닙니다.")
    assessment = assess_session(raw, observation)
    st.subheader("수집 상태 축")
    axes = [
        ("저장", assessment.storage), ("프로세스", assessment.process), ("native UI", assessment.native_ui),
        ("lease", assessment.lease), ("feed freshness", assessment.feed), ("research", assessment.research),
    ]
    for offset in (0, 3):
        columns = st.columns(3)
        for column, (name, value) in zip(columns, axes[offset:offset + 3]):
            column.metric(name, ASSESSMENT_LABEL.get(value, value))
    st.caption(
        "종료 근거: " + ASSESSMENT_LABEL.get(assessment.termination, assessment.termination)
        + " · 저장 closed, lease free, queue 0은 각각 다른 근거이며 서로를 대신하지 않습니다."
    )
    st.subheader("오늘 수집")
    heartbeat = observation["heartbeat"]
    if heartbeat:
        columns = st.columns(3)
        label = "수신" if heartbeat.get("counts_kind") == "callbacks" else "적재"
        columns[0].metric(f"로그상 체결 {label}", f"{heartbeat['trades']:,}")
        columns[1].metric(f"로그상 호가 {label}", f"{heartbeat['quotes']:,}")
        columns[2].metric("대기 큐", f"{heartbeat['queue']:,}")
        st.caption(f"마지막 하트비트 {heartbeat['time']} · 경과 {observation['age_seconds']}초")
    if observation["status"] == "recent":
        st.info("최근 수집 하트비트가 있습니다. 프로세스 생존·무누락·데이터 정확성은 별도 확인이 필요합니다.")
    elif observation["status"] == "stale":
        st.warning("하트비트가 오래됐습니다. 수집 종료 또는 이상 여부를 확인하세요. 자동 재시작하지 않습니다.")
    elif observation["status"] == "clock_ahead":
        st.warning("로그 시각이 현재보다 앞섭니다. PC 시각을 확인하세요.")
    else:
        st.info("오늘의 읽을 수 있는 하트비트가 없습니다. 수집 중/중지 여부는 미확인입니다.")
    render_capture_controls(root)
    with st.expander("최근 로그 메시지와 관측 근거"):
        st.text(observation["log_path"])
        st.text("\n".join(observation["recent_messages"]) or "읽은 로그 끝부분에 해당 메시지가 없습니다.")
        st.caption("최대 64 KiB 로그 끝부분만 읽습니다. 전체 로그 오류 집계가 아닙니다.")

    inspect_tab, plan_tab, history_tab, connections_tab = st.tabs(["결과 조회", "장외 재생 계획", "작업 이력", "연결 범위"])
    store = JobStore(root)
    with inspect_tab:
        st.write("저장된 연구 결과 JSON을 별도 워커에서 읽습니다. 원본 틱 DB는 열지 않습니다.")
        with st.form("inspect_result_form"):
            result_path = st.text_input("연구 결과 경로", placeholder="research_runs/<run-id>/result.json", key="control_result_path")
            submitted = st.form_submit_button("결과 조회 요청")
        if submitted:
            try:
                job_id = queue_inspection(root, result_path)
            except (OSError, ValueError, sqlite3.Error) as exc:
                st.error(f"조회 요청을 저장하지 못했습니다: {exc}")
            else:
                st.success(f"조회 요청 저장: {job_id}")
                try:
                    start_inspection_worker()
                except OSError as exc:
                    st.warning(f"워커를 시작하지 못했습니다. 요청은 대기 상태로 보존됩니다: {exc}")
        if st.button("대기 중 결과 조회 처리", key="control_retry_worker"):
            try:
                start_inspection_worker()
                st.info("조회 워커 시작을 요청했습니다. 잠시 후 상태를 새로고침하세요.")
            except OSError as exc:
                st.error(f"워커 시작 실패: {exc}")
        st.caption("화면 연결이 끊겨도 실행된 워커와 기록은 유지됩니다. 워커가 중단된 실행은 자동 재실행하지 않습니다.")

    with plan_tab:
        st.write("종료된 raw v2의 재생 설정을 저장합니다. 작업 이력에서 장외 검사·재생을 별도로 실행할 수 있습니다.")
        with st.form("replay_plan_form"):
            db = st.text_input("닫힌 raw v2 경로", placeholder="sampledata/raw_ticks_v2/<session>.db", key="control_db")
            code = st.text_input("종목 코드", value="005930")
            venue = st.text_input("기록된 venue", value="unknown")
            quantity = st.number_input("주문 수량", min_value=1, value=1, step=1)
            cash = st.text_input("초기 현금", value="100000")
            fee = st.text_input("편도 비용률", placeholder="검증한 비용 비율을 입력")
            buy = st.text_input("매수 지연(초)", value="1")
            sell = st.text_input("매도 지연(초)", value="1")
            cancel = st.text_input("취소 지연(초)", value="1")
            age = st.text_input("최대 호가 나이(초)", value="2")
            cooldown = st.text_input("재진입 대기(초)", value="10")
            rule = st.selectbox("청산 규칙", ["fixed", "tick_trail", "step_trail"])
            save_plan = st.form_submit_button("장외 계획 저장 · 실행 안 함")
        if save_plan:
            try:
                job_id = plan_replay(root, db=db, code=code, venue=venue, quantity=quantity, cash=cash,
                    fee_rate=fee, buy_latency_sec=buy, sell_latency_sec=sell, cancel_latency_sec=cancel,
                    max_quote_age_sec=age, cooldown_sec=cooldown, exit_rule=rule)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                st.error(f"계획을 저장하지 못했습니다: {exc}")
            else:
                st.success(f"장외 계획 저장: {job_id}")
        st.caption("계획 저장은 헤더만 확인합니다. 별도 실행 시 전체 무결성을 검사하며, 화면 실행은 32 MiB·10만 raw 이내로 제한합니다.")

    with history_tab:
        try:
            jobs = store.recent()
            reservations = {r["job_id"]: r for r in schedules(root)}
        except sqlite3.Error as exc:
            st.error(f"작업 이력 조회 실패: {exc}")
            jobs = []
            reservations = {}
        if reservations and st.button("지난 예약 대조 · 재실행 안 함", key="schedule_reconcile"):
            try:
                st.info(f"기한이 지난 미실행 예약 {expire_missed(root)}건을 만료 처리했습니다.")
            except sqlite3.Error as exc:
                st.error(f"예약 대조 실패: {exc}")
        if not jobs:
            st.info("저장된 작업이 없습니다. 결과 조회나 장외 계획을 등록하면 여기에 표시됩니다.")
        else:
            for job in jobs:
                with st.expander(f"{JOB_STATUS.get(job['status'], job['status'])} · {job['kind']} · {job['id'][:8]}"):
                    st.caption(f"생성 {job['created_at']} · 갱신 {job['updated_at']} · worker {job['owner'] or '미배정'}")
                    if job["status"] == "running":
                        st.warning("실행 기록이 남아 있습니다. 기록만으로 워커 생존을 확정할 수 없습니다.")
                    if job["result"] is not None:
                        if job["result"].get("diagnostics_only"):
                            st.warning("조회 자체는 완료됐지만 연구 결과는 실행 중이거나 실패한 진단 자료입니다.")
                        st.json(job["result"])
                    if job["error"]:
                        st.error(job["error"])
                    st.json(job["payload"])
                    reservation = reservations.get(job["id"])
                    if reservation:
                        st.caption(f"예약 {reservation['due']} · {reservation['state']} · OS 등록 {reservation['registration']}")
                        if reservation["error"]:
                            st.warning(reservation["error"])
                    elif job["kind"] == "replay_raw_v2" and job["status"] == "planned":
                        with st.form(f"schedule_form_{job['id']}"):
                            due = st.text_input("예약 시각 (시간대 포함)", placeholder="2026-09-17T18:00:00+09:00", key=f"due_{job['id']}")
                            reserve = st.form_submit_button("장외 재생 1회 예약")
                        if reserve:
                            try:
                                task_name = schedule_replay(root, job["id"], datetime.fromisoformat(due))
                                st.success(f"예약 등록: {task_name}")
                            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                                st.error(f"예약 등록 미완료: {exc}")
                        st.caption("1분~7일 이내 장외 시각만 허용합니다. PC가 켜져 있고 현재 Windows 사용자가 로그인되어 있어야 합니다. 5분 이상 놓친 예약은 재실행하지 않습니다.")
                    if job["kind"] == "replay_raw_v2" and job["status"] in ("planned", "queued"):
                        if st.button("장외 검사·재생 실행", key=f"replay_{job['id']}"):
                            try:
                                start = start_replay_worker if job["status"] == "planned" else retry_replay_worker
                                start(root, job["id"])
                                st.success("워커 시작을 요청했습니다. 결과는 새로고침으로 확인하세요.")
                            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                                st.error(f"재생 시작 미완료: {exc}")
                    if job["status"] in ("queued", "planned"):
                        if st.button("이 대기 작업 취소", key=f"cancel_{job['id']}"):
                            try:
                                cancelled = store.cancel(job["id"])
                            except sqlite3.Error as exc:
                                st.error(f"취소 요청 저장 실패: {exc}")
                            else:
                                if cancelled:
                                    st.rerun()
                                else:
                                    st.warning("이미 실행되었거나 상태가 바뀌어 취소하지 않았습니다.")

    with connections_tab:
        st.markdown("""- **키움 수집:** 로그 관측 및 이 화면에서 만든 소규모 세션의 시작·종료 요청 연결.
- **raw v2:** 운영 수집기 기본 저장 경로와 상태 관측 연결. 실피드 부하·품질 검증은 별도.
- **연구 결과:** 별도 경량 워커로 조회 가능. 수익률·총자산을 새로 계산하지 않음.
- **장외 재생:** 닫힌 소규모 raw v2 전체 무결성 검사 후 별도 워커에서 재생. 품질 오류는 실패로 보존.
- **기존 백테스트 분석:** 왼쪽 화면 선택에서 분석으로 이동.
- **일봉·메타데이터:** 개별 파일럿 유지. 자동 작업 연결 미완료.
- **모의·실전 주문:** 미구현. 주문 전송 버튼 없음.""")
        st.caption("다음 단계: 수집기 제어 계약 → 장외 작업 워커 → 종료/데이터 검증 후 단계별 실행. 로그인과 원격 접근 설정은 별도입니다.")


@st.fragment(run_every="5s")
def render_capture_controls(root):
    # Fragment reruns bypass app.py; repeat authorization before any action.
    from dashboard.access import require_access
    require_access(show_logout=False)
    store = ManagedCaptures(root)
    with st.expander("소규모 수집 시작·종료"):
        st.caption("시작하면 별도 32비트 수집기에서 키움 로그인이 열립니다. 최대 10종목·300초이며 기존 수집기를 인수하지 않습니다.")
        try:
            current = store.get()
        except (OSError, ValueError, sqlite3.Error) as exc:
            st.error(f"수집 제어 기록 확인 실패: {exc}")
            return
        active = current is not None and current["state"] in ACTIVE
        if current:
            st.write(f"관리 요청 {current['id']} · 기록 상태 {current['state']}")
            st.caption(f"기록 갱신 {current['updated']} · 현재 생존/무누락 인증이 아닙니다.")
            if active and current["owner"]:
                key = "capture_health_" + str(root)
                if key not in st.session_state:
                    st.session_state[key] = CaptureHealth()
                try:
                    health = st.session_state[key].observe(store, current)
                    if health["state"] == "responsive":
                        st.info(f"제어 응답 확인 · heartbeat {health['heartbeat_seq']} · 마지막 변화 {health['age_seconds']}초 전")
                    else:
                        st.warning(f"제어 응답 미확인: {health['reason']}")
                except (OSError, ValueError, sqlite3.Error) as exc:
                    st.warning(f"제어 응답 확인 실패: {exc}")
            if current["error"]:
                st.error(current["error"])
            if active and st.button("이 관리 세션 종료 요청", key="capture_stop"):
                try:
                    store.request_stop(current["id"])
                    st.success("종료 요청을 저장했습니다. 저장 완료 보고는 새로고침하여 확인하세요.")
                except (OSError, ValueError, sqlite3.Error) as exc:
                    st.error(f"종료 요청 실패: {exc}")
            if active and current["owner"] and st.button("종료된 관리 프로세스 이력 대조", key="capture_reconcile"):
                try:
                    state = store.reconcile(current["id"])
                    st.success(f"저장 보고 대조 결과: {state}. 원본 무결성·품질 검사는 별도입니다.")
                except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                    st.error(f"대조 미완료: {exc}")
        with st.form("capture_start_form"):
            codes = st.text_input("수집 종목 (쉼표 구분)", value="005930", key="capture_codes")
            duration = st.number_input("수집 시간 (초)", min_value=1, max_value=300, value=60, step=1)
            server = st.selectbox("로그인할 서버", ["mock", "live"])
            start = st.form_submit_button("소규모 수집 시작 · 로그인", disabled=active)
        if start:
            try:
                launch = start_managed_capture(root, [c.strip() for c in codes.split(",")], int(duration), server)
                st.success(f"시작 요청 저장: {launch}. 실제 로그인 서버가 다르면 수집하지 않습니다.")
            except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
                st.error(f"시작 요청 실패: {exc}")
