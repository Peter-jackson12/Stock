"""Control tower UI. Observations and durable plans are separate from execution."""
from pathlib import Path
import sqlite3

import streamlit as st

from control_tower.jobs import JobStore
from control_tower.service import queue_inspection, plan_replay, start_inspection_worker
from control_tower.status import observe_collector

ROOT = Path(__file__).resolve().parents[1]
JOB_STATUS = {"planned": "장외 계획 · 실행 미연결", "queued": "조회 대기", "running": "조회 중 · 상태 확인 필요 시 이력 보존",
              "succeeded": "조회 완료", "failed": "조회 실패", "cancelled": "취소됨"}


def render_control_tower(root=None):
    root = Path(root) if root is not None else ROOT
    st.title("Stock 운영 관리")
    st.caption("수집 관측 · 작업 이력 · 틱 연구 결과를 한곳에서 확인합니다.")
    if st.button("상태 새로고침", key="control_refresh"):
        st.rerun()
    observation = observe_collector(root)
    st.subheader("오늘 수집")
    heartbeat = observation["heartbeat"]
    if heartbeat:
        columns = st.columns(3)
        columns[0].metric("로그상 체결 적재", f"{heartbeat['trades']:,}")
        columns[1].metric("로그상 호가 적재", f"{heartbeat['quotes']:,}")
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
    st.caption("현재 수집기는 이 화면 밖에서 실행됩니다. 시작·종료 제어는 아직 연결하지 않았습니다.")
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
        st.write("종료된 raw v2의 재생 설정을 저장합니다. 지금은 계획 저장만 가능하며 실제 재생은 시작하지 않습니다.")
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
        st.caption("헤더의 closed 표기만 확인합니다. 전체 체크섬·품질·운영 수집 종료 확인은 실행 전 별도로 필요합니다.")

    with history_tab:
        try:
            jobs = store.recent()
        except sqlite3.Error as exc:
            st.error(f"작업 이력 조회 실패: {exc}")
            jobs = []
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
        st.markdown("""- **키움 수집:** 로그 관측 연결. 프로세스 시작·정상 종료 제어 미연결.
- **raw v2:** 종료 파일의 재생 계획 저장. 운영 수집기 전환 미적용.
- **연구 결과:** 별도 경량 워커로 조회 가능. 수익률·총자산을 새로 계산하지 않음.
- **기존 백테스트 분석:** 왼쪽 화면 선택에서 분석으로 이동.
- **일봉·메타데이터:** 개별 파일럿 유지. 자동 작업 연결 미완료.
- **모의·실전 주문:** 미구현. 주문 전송 버튼 없음.""")
        st.caption("다음 단계: 수집기 제어 계약 → 장외 작업 워커 → 종료/데이터 검증 후 단계별 실행. 로그인과 원격 접근 설정은 별도입니다.")
