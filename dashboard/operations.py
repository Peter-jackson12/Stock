"""Operations UI; distinct module name avoids shadowing the control_tower package."""
from pathlib import Path
from datetime import datetime
import sqlite3

import streamlit as st

from control_tower.jobs import JobStore
from control_tower.service import queue_inspection, plan_replay, start_inspection_worker
from control_tower.status import observe_collector, observe_raw_capture
from dashboard.session_assessment_view import render_session_assessment
from control_tower.managed_capture import ManagedCaptures, start_managed_capture, ACTIVE
from control_tower.offline_worker import start_replay_worker, retry_replay_worker
from control_tower.capture_health import CaptureHealth
from control_tower.operator_summary import summarize_operator_state
from control_tower.operator_environment import inspect_operator_environment
from control_tower.collector_preflight import inspect_collector_preflight
from control_tower.windows_shortcut import inspect_shortcuts, manage_shortcut, shortcut_contract
from control_tower.collector_run_plan import (
    CollectionRunPlanStore,
    build_collection_run_plan,
    inspect_run_target,
)
from control_tower.replay_schedule import schedule_replay, schedules, expire_missed

ROOT = Path(__file__).resolve().parents[1]
JOB_STATUS = {"planned": "장외 계획", "queued": "실행 대기", "running": "실행 중 · 상태 확인 필요 시 이력 보존",
              "succeeded": "작업 완료", "failed": "작업 실패", "cancelled": "취소됨"}


def render_control_tower(root=None):
    root = Path(root) if root is not None else ROOT
    st.title("Stock 운영 관리")
    st.caption("수집 관측 · 작업 이력 · 틱 연구 결과를 한곳에서 확인합니다.")
    if st.button("상태 새로고침", key="control_refresh"):
        st.rerun()
    observation = observe_collector(root)
    raw = observe_raw_capture(root)
    render_operator_overview(observation, raw)
    render_operator_environment(root)
    render_windows_shortcuts(root)
    preflight = render_collector_preflight(root)
    render_collection_run_plan(root, preflight)
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
    render_session_assessment(raw, observation)
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
                                st.error(f"취소 요청 실패: {exc}")
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


def render_operator_overview(observation, raw):
    """현재 저장 근거를 사람이 먼저 읽을 수 있는 안전한 요약으로 표시한다."""
    summary = summarize_operator_state(observation, raw)
    st.subheader("Operator 요약")
    columns = st.columns(3)
    columns[0].metric("저장된 raw 상태", summary["raw_evidence"])
    columns[1].metric("로그 heartbeat", summary["heartbeat_evidence"])
    columns[2].metric("현재 수집기", summary["collector_now"])
    if summary["producer_state"]:
        st.caption(f"상태 파일의 과거 생산자 주장: {summary['producer_state']}")
    renderer = {"error": st.error, "warning": st.warning, "info": st.info}[summary["tone"]]
    renderer(f"{summary['headline']} — {summary['guidance']}")
    with st.expander("처음이면 무엇부터 보면 되나요?"):
        st.markdown(
            """
1. **상태 새로고침**으로 저장된 근거의 시각을 다시 읽습니다.
2. 아래 **raw v2 수집 세션 / 세션 근거 / 오늘 수집**에서 오류·중단·오래된 기록을 확인합니다.
3. 실제 수집을 시작하기 전에는 서버·시장 구간·기존 수집기 충돌 여부를 별도 확인합니다.
4. 기존 성과 런은 왼쪽 **백테스트 분석**에서 봅니다. 현재 selected-v2 연구 경로와 같은 결과 형식은 아닙니다.
            """
        )
        st.caption("이 요약은 원본 DB·PID·창·lease를 열지 않으며, 수집 시작 승인이나 데이터 품질 인증을 만들지 않습니다.")


def render_operator_environment(root):
    """CLI doctor와 같은 작은 읽기 전용 환경 목록을 표시한다."""
    with st.expander("읽기 전용 환경 점검"):
        st.caption("Windows · 64-bit · Python 버전 · 프로젝트 .venv · 필수 파일 · GUI 패키지 metadata · .venv32 파일 존재만 확인합니다.")
        try:
            report = inspect_operator_environment(Path(root))
        except (OSError, ValueError) as exc:
            st.warning(f"환경 목록을 읽지 못했습니다: {exc}. 자동 설치하거나 수정하지 않았습니다.")
            return
        for check in report["checks"]:
            st.text(f"[{check['status']}] {check['name']}: {check['detail']}")
        if report["inventory_passed"]:
            st.info("환경 목록 점검: 통과")
        else:
            st.warning("환경 목록 점검: 미충족 — 자동 설치하거나 수정하지 않았습니다.")
        st.caption("PASS는 위 작은 목록 점검만 뜻합니다. GUI 정상 기동·수집 준비·OCX 준비 또는 실행 승인이 아닙니다.")


def _render_shortcut_state(label, result):
    state = result.get("state")
    if state == "ready":
        st.success(f"{label}: 준비됨 · 아이콘 더블클릭으로 Stock UI를 열 수 있습니다.")
        st.caption(f"대상: {result.get('target')} {result.get('arguments') or ''}")
    elif state == "absent":
        st.info(f"{label}: 아직 바로가기가 없습니다.")
    elif state == "stale":
        st.warning(f"{label}: 이전 프로젝트 경로를 가리킵니다. 현재 경로로 갱신할 수 있습니다.")
        st.caption(f"현재 바로가기 대상: {result.get('target')}")
    elif state == "conflict":
        st.error(f"{label}: 같은 이름의 다른 바로가기가 있습니다. 자동 덮어쓰기·삭제하지 않습니다.")
    elif state == "unsupported":
        st.info(f"{label}: Windows에서만 바로가기를 관리합니다.")
    else:
        st.warning(f"{label}: {result.get('reason') or '상태를 확인하지 못했습니다.'}")


def render_windows_shortcuts(root):
    """Create/remove only the current user's shortcuts after explicit clicks."""
    root = Path(root)
    with st.expander("Windows 실행 바로가기"):
        contract = shortcut_contract(root, "desktop")
        st.caption(
            "한 번 만든 뒤에는 Stock Operator 아이콘을 더블클릭하면 기존 stock.cmd ui가 실행됩니다. "
            "관리자 권한·레지스트리·영구 PowerShell 실행 정책 변경은 없습니다."
        )
        st.text(f"현재 프로젝트 대상: {contract['expected_target']} ui")

        status_key = "operator_windows_shortcut_status"
        if st.button("바로가기 상태 확인", key="shortcut_status_check"):
            st.session_state[status_key] = inspect_shortcuts(root)

        left, right = st.columns(2)
        with left:
            if st.button("바탕화면 바로가기 만들기/갱신", key="shortcut_desktop_install"):
                result = manage_shortcut(root, location="desktop", action="install")
                st.session_state.setdefault(status_key, {})["desktop"] = result
            if st.button("바탕화면 바로가기 제거", key="shortcut_desktop_remove"):
                result = manage_shortcut(root, location="desktop", action="remove")
                st.session_state.setdefault(status_key, {})["desktop"] = result
        with right:
            if st.button("시작 메뉴 바로가기 만들기/갱신", key="shortcut_start_install"):
                result = manage_shortcut(root, location="start_menu", action="install")
                st.session_state.setdefault(status_key, {})["start_menu"] = result
            if st.button("시작 메뉴 바로가기 제거", key="shortcut_start_remove"):
                result = manage_shortcut(root, location="start_menu", action="remove")
                st.session_state.setdefault(status_key, {})["start_menu"] = result

        statuses = st.session_state.get(status_key) or {}
        labels = {"desktop": "바탕화면", "start_menu": "시작 메뉴"}
        for location in ("desktop", "start_menu"):
            if location in statuses:
                _render_shortcut_state(labels[location], statuses[location])

        st.caption(
            "같은 이름의 다른 바로가기는 보호합니다. 프로젝트 폴더를 옮겼다면 새 위치에서 "
            "stock.cmd ui로 한 번 연 뒤 여기서 바로가기를 현재 경로로 갱신하세요."
        )
        st.caption(
            "브라우저 탭만 닫으면 Streamlit 서버는 계속 실행될 수 있습니다. "
            "바로가기가 연 콘솔 창을 닫거나 그 창에서 Ctrl+C를 누르면 UI 서버가 종료됩니다."
        )


def render_collector_preflight(root):
    """Show local start-decision facts without authorizing or starting collection."""
    with st.expander("수집 전 읽기 전용 preflight"):
        st.caption(
            "로컬 Windows에서 collector runtime · 32-bit/OCX 공식 preflight · 저장 여유 · "
            "process/window · lease를 읽습니다. 시장 조회나 수집 동작은 하지 않습니다."
        )
        try:
            report = inspect_collector_preflight(Path(root))
        except (OSError, RuntimeError, ValueError) as exc:
            st.warning(f"수집 전 점검을 읽지 못했습니다: {exc}. 자동 수정하거나 실행하지 않았습니다.")
            return {
                "schema": "operator_collector_preflight_unavailable",
                "status": "UNVERIFIED",
                "local_status": "UNVERIFIED",
                "storage_free_bytes": None,
                "checks": [{
                    "id": "preflight_probe",
                    "status": "UNVERIFIED",
                    "label": "수집 전 preflight",
                    "detail": str(exc)[:512],
                    "next_check": "오류를 자동 수정하지 말고 실행 직전에 다시 점검하세요.",
                }],
            }
        renderer = {
            "PASS": st.info,
            "WARN": st.warning,
            "BLOCKED": st.error,
            "UNVERIFIED": st.warning,
        }.get(report["status"], st.warning)
        renderer(
            f"현재 표시: {report['status']} · 로컬 관측 {report['local_status']} — "
            "시장 일정과 실행 승인은 별도 확인이 필요합니다."
        )
        for check in report["checks"]:
            st.text(f"[{check['status']}] {check['label']}: {check['detail']}")
            st.caption(f"다음 확인: {check['next_check']}")
        st.caption(
            "이 결과는 GitHub/원격 상태로 운영 PC를 추정하지 않으며, 기존 수집 시작 버튼을 "
            "활성화하거나 collector admission/실행 승인으로 승격하지 않습니다."
        )
        return report


def render_collection_run_plan(root, preflight):
    """Render a disconnected review/save contract immediately after preflight."""
    with st.expander("수집 Run Plan · 실행 직전 체크리스트", expanded=True):
        st.caption(
            "managed capture와 같은 입력 규칙으로 계획값을 검토하지만, 저장해도 로그인·수집 시작·"
            "시장 조회·실행 승인을 만들지 않습니다. 기존 시작 화면의 값이나 버튼 상태와도 연결되지 않습니다."
        )
        try:
            run_target = inspect_run_target(Path(root))
        except (OSError, RuntimeError, ValueError) as exc:
            run_target = {"probe_ok": False, "error": f"run target probe failed: {exc}"}

        with st.form("collection_run_plan_form"):
            server = st.selectbox("계획 서버", ["mock", "live"], key="run_plan_server")
            codes_text = st.text_input(
                "계획 종목 (쉼표 구분)", value="005930", key="run_plan_codes",
            )
            duration = st.number_input(
                "계획 수집 시간 (초)", min_value=1, max_value=300, value=60, step=1,
                key="run_plan_duration",
            )
            required_storage = st.number_input(
                "예상 필요 저장공간 (MiB, 0은 아직 미확인)",
                min_value=0,
                max_value=1024 * 1024,
                value=0,
                step=1,
                key="run_plan_storage_mib",
            )
            market_date = st.text_input(
                "목표 시장 날짜 (계획값, 검증 아님)", placeholder="YYYY-MM-DD 또는 비워 두기",
                key="run_plan_market_date",
            )
            market_segment = st.text_input(
                "목표 장 구간 (계획값, 검증 아님)", placeholder="승인된 구간 계약 또는 비워 두기",
                key="run_plan_market_segment",
            )
            review = st.form_submit_button("계획 검토 · 실행 안 함")
            save = st.form_submit_button("계획 저장 · 실행 안 함")

        codes = [code.strip() for code in codes_text.split(",")]
        try:
            plan = build_collection_run_plan(
                codes=codes,
                duration_seconds=int(duration),
                server=server,
                required_storage_mib=int(required_storage) if required_storage else None,
                planned_market_date=market_date,
                planned_market_segment=market_segment,
                preflight=preflight,
                run_target=run_target,
            )
        except (TypeError, ValueError) as exc:
            st.error(f"Run Plan 입력을 검토하지 못했습니다: {exc}")
            return

        renderer = {
            "PASS": st.info,
            "WARN": st.warning,
            "BLOCKED": st.error,
            "UNVERIFIED": st.warning,
        }.get(plan["status"], st.warning)
        renderer(f"계획 상태: {plan['status']} · review_only · 실행 연결 없음")
        for check in plan["checks"]:
            st.text(f"[{check['status']}] {check['label']}: {check['detail']}")
            st.caption(f"다음 확인: {check['next_check']}")

        store = CollectionRunPlanStore(root)
        if review:
            st.info("현재 입력을 검토했습니다. operations_state에 저장하거나 실행하지 않았습니다.")
        if save:
            try:
                plan_id = store.save(plan)
            except (OSError, ValueError, sqlite3.Error) as exc:
                st.error(f"Run Plan을 저장하지 못했습니다: {exc}")
            else:
                st.success(f"검토 전용 Run Plan 저장: {plan_id[:8]} · 실행/승인 연결 없음")
        try:
            recent = store.recent(3)
        except (OSError, ValueError, sqlite3.Error) as exc:
            st.warning(f"저장된 Run Plan 목록을 읽지 못했습니다: {exc}")
        else:
            if recent:
                latest = recent[0]
                inputs = latest["payload"]["inputs"]
                st.caption(
                    f"최근 저장 {latest['id'][:8]} · {latest['created_at']} · "
                    f"{inputs['server']} / {', '.join(inputs['codes'])} / {inputs['duration_seconds']}초 · "
                    "review_only"
                )
        st.caption(
            "저장된 계획은 현재 preflight나 Git 상태가 아닙니다. 실제 실행 직전에 새 admission과 "
            "시장 일정·장 구간·명시적 실행 승인을 모두 다시 확인해야 합니다."
        )




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
