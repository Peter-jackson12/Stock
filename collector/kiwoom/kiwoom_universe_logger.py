"""
키움증권 Open API+ 코스피/코스닥 후보군 실시간 체결 및 10호가 수집 엔진 (32비트 전용)
- 기존 코드/이름 필터 사용. 종목 분류 정확성은 별도 대조 필요.
- 기본 raw v2: 원문 FID·수신 순서 보존, 방향/venue 미확인은 unknown
- 화면번호 26개 분할 등록 (SetRealReg)
- 제한 큐와 저장 워커; 초과/오류 시 중단하며 무누락을 보장하지 않음
- Ctrl+C 정상 종료 처리 완비
- 수신 침묵 감시: 장중 무이벤트 구간을 주기 루프에서 경고하고, 종료 시
  결손이 남아 있으면 '정상 종료' 라고 보고하지 않는다 (session_monitor).
  정상 종료에도 마지막 수신~종료 갭 수치와 임계값을 함께 남긴다.
- 대기큐 적체 감시: 절대값이 아니라 "바닥 이상을 일정 시간 유지하며
  줄지 않는가"로 판정한다. 장중 최대 적체값과 시각을 세션 요약에 남긴다.
- 회전 파일 로그: logs/kiwoom_universe_{YYYYMMDD}.log (콘솔 출력 동시 기록)
- 저장소: sampledata/raw_ticks_v2/{YYYYMMDD}/{session_id}.db (실행별 새 파일)
- --storage raw-v1: 기존 호환 경로를 명시적으로 선택
"""

import sys
import os
from pathlib import Path
from datetime import datetime, timezone
import argparse
import json
import subprocess
import time
import queue
import threading
import signal

from PyQt5.QtWidgets import QApplication
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QTimer

# 기동 중(로거 초기화 전)에도 Ctrl+C 가 먹도록 OS 기본 동작을 깔아둔다.
# 로거가 뜨면 _install_sigint_handler() 가 '요약 남기고 종료' 핸들러로 교체한다.
signal.signal(signal.SIGINT, signal.SIG_DFL)

# 프로젝트 루트 경로 등록
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

#: 실시간 등록 FID. 전 종목 경로와 구독 계획 경로가 같은 목록을 쓴다.
REAL_FIDS = "20;10;15;14;27;28;21;" + ";".join(str(f) for f in range(41, 81))

RAW_DIR = PROJECT_ROOT / "sampledata" / "raw_ticks"
LOG_DIR = PROJECT_ROOT / "logs"

from collector.kiwoom.session_monitor import (                         # noqa: E402
    DEFAULT_GAP_THRESHOLD_SEC,
    DEFAULT_QUEUE_BACKLOG_FLOOR,
    DEFAULT_QUEUE_STUCK_WINDOW_SEC,
    SessionLog,
    SessionMonitor,
)


from collector.kiwoom.market_sessions import resolve_profile  # noqa: E402
from collector.kiwoom.resource_log import ResourceHistory  # noqa: E402
from collector.kiwoom.session_transition import SessionTransition, write_transition_record  # noqa: E402
from collector.kiwoom.subscription_plan import MODE_NXT  # noqa: E402
from collector.kiwoom.tick_writer import TickWriter  # noqa: E402
from collector.kiwoom.live_capture import LiveRawCapture, TRADE_FIDS, QUOTE_FIDS  # noqa: E402


class KiwoomUniverseLogger:
    def __init__(
        self,
        *,
        gap_threshold_sec: float = DEFAULT_GAP_THRESHOLD_SEC,
        queue_backlog_floor: int = DEFAULT_QUEUE_BACKLOG_FLOOR,
        queue_stuck_window_sec: float = DEFAULT_QUEUE_STUCK_WINDOW_SEC,
        storage="raw-v2",
        code_revision=None,
        codes=None,
        duration_seconds=None,
        managed=None,
        diagnostics=None,
        plan=None,
        aftermarket_plan=None,
        aftermarket_duration_seconds=None,
        aftermarket_transition_at=None,
        aftermarket_transition_after_seconds=None,
    ):
        if storage not in ("raw-v1", "raw-v2"):
            raise ValueError("unsupported storage backend")
        self.storage = storage
        self.managed = managed
        self.diagnostics = diagnostics
        self.code_revision = code_revision or subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True, timeout=3).strip()
        self.codes, self.duration_seconds = codes, duration_seconds
        # 구독 계획. None 이면 기존 정규장 경로 그대로다 — 접미사 허용은 계획이 있을 때만이다.
        self.plan = plan
        # 애프터마켓 전환은 **명시적으로 계획을 넘길 때만** 준비된다. 기본은 전환 없음이다.
        self.aftermarket_plan = aftermarket_plan
        # 전환 후 애프터마켓 구간 자체의 종료 조건. 정규장의 15:35 종료는 계획 모드에
        # 적용하지 않으므로, 전환 뒤에도 무제한으로 돌지 않도록 별도 상한을 강제한다.
        # 최초 구현은 실측 전이라 1~300초로만 검증한다 — 20시까지의 장시간 운영은
        # 이 상한을 없애는 별도 결정 없이는 기본 활성화하지 않는다.
        self.aftermarket_duration_seconds = aftermarket_duration_seconds
        self._aftermarket_subscribed_at = None
        self._regular_subscription_context = None
        self._pending_aftermarket_codes = None
        self._transition_record_path = None
        self._transition_active = False
        self._monotonic = time.monotonic
        self._last_received_monotonic = None
        self._last_received_utc = None
        self.transition = None
        # 전환을 자동으로 시켜 볼 트리거 — 둘 다 선택 항목이다. CLI 는 정확히 하나를
        # 요구하지만(사람이 직접 run_aftermarket_transition() 을 부를 수 없으므로),
        # 이 생성자를 직접 쓰는 기존 경로(수동 호출)는 트리거 없이도 그대로 돈다.
        self._aftermarket_transition_at = aftermarket_transition_at
        self._aftermarket_transition_after_seconds = aftermarket_transition_after_seconds
        self._aftermarket_transition_due = False
        if aftermarket_plan is not None:
            if aftermarket_plan.mode != MODE_NXT:
                raise ValueError("애프터마켓 전환 계획은 NXT 모드여야 한다")
            if plan is not None:
                raise ValueError("정규장 계획과 애프터마켓 전환 계획을 함께 줄 수 없다")
            if storage != "raw-v2" or managed is not None:
                raise ValueError("애프터마켓 전환은 독립 raw-v2 실행만 지원한다")
            if type(aftermarket_duration_seconds) is not int or not 1 <= aftermarket_duration_seconds <= 300:
                raise ValueError("애프터마켓 전환 후 구간은 1~300초 제한 시간이 필요하다")
            if aftermarket_transition_at is not None and aftermarket_transition_after_seconds is not None:
                raise ValueError("전환 시각과 경과 시간 트리거를 함께 줄 수 없다")
            if aftermarket_transition_at is not None and (
                    type(aftermarket_transition_at) is not int or not 0 <= aftermarket_transition_at <= 235959):
                raise ValueError("전환 시각은 HHMMSS 형식의 정수(0~235959)여야 한다")
            if aftermarket_transition_after_seconds is not None and (
                    type(aftermarket_transition_after_seconds) is not int
                    or aftermarket_transition_after_seconds <= 0):
                raise ValueError("경과 시간 트리거는 양의 정수(초)여야 한다")
            # 시장 구간 프로필이 잘못됐으면 여기서 바로 거부한다 — Qt/OCX 를 만들고
            # 로그인까지 간 뒤 전환 단계(open_aftermarket)에서야 실패하지 않기 위해서다.
            resolve_profile(aftermarket_plan.market_profile)
        elif aftermarket_transition_at is not None or aftermarket_transition_after_seconds is not None:
            raise ValueError("애프터마켓 전환 계획 없이 트리거를 줄 수 없다")
        if plan is not None:
            if codes is not None:
                raise ValueError("구독 계획과 --codes 를 함께 줄 수 없다")
            if plan.mode != MODE_NXT:
                raise ValueError("구독 계획은 명시적 NXT 모드에서만 받는다")
            if storage != "raw-v2" or managed is not None:
                raise ValueError("NXT 계획은 독립 raw-v2 검증 실행만 지원한다")
            if type(duration_seconds) is not int or not 1 <= duration_seconds <= 300:
                raise ValueError("NXT 계획은 1~300초 제한 시간이 필요하다")
        self._subscribed_at = None
        self.raw_capture = None
        self._login_handled = False
        # OCX(ActiveX) 는 이 스레드에 묶인다 — run_aftermarket_transition() 이 다른
        # 스레드에서 불리면 OCX 호출 전에 거부한다(_stats_worker 는 플래그만 세운다).
        self._main_thread_ident = threading.get_ident()
        self.app = QApplication(sys.argv)

        # 파이썬 인터프리터가 Ctrl+C를 감지할 수 있도록 0.2초 주기 타이머 가동
        self.timer = QTimer()
        self.timer.timeout.connect(self._poll_control)
        self.timer.start(200)

        self.today_str = datetime.now().strftime("%Y%m%d")
        self.db_path = RAW_DIR / f"{self.today_str}_raw.db"
        self.log_path = LOG_DIR / f"kiwoom_universe_{self.today_str}.log"

        # 회전 파일 로그. 콘솔에 나가는 메시지는 전부 여기에도 남는다.
        # (주기 상태줄만 60초에 한 번 샘플로 남긴다 — SessionLog 참고)
        self.log = SessionLog(self.log_path)

        # 감시기 구성값을 남겨 둔다 — 애프터마켓 전환 때 같은 설정으로 새 감시기를 만들기
        # 위해서다. 정규장의 수신 시각·침묵 권고·카운터를 새 세션이 잘못 이어받지 않게
        # 인스턴스 자체를 교체한다(재사용하지 않는다).
        self._gap_threshold_sec = gap_threshold_sec
        self._queue_backlog_floor = queue_backlog_floor
        self._queue_stuck_window_sec = queue_stuck_window_sec

        # 수신 침묵 감시기 + 대기큐 적체 감시기(발견 #13). 판정은 _stats_worker 의
        # 기존 1초 루프에서만 돈다.
        self.monitor = SessionMonitor(
            sessions=None if plan is None else resolve_profile(plan.market_profile),
            gap_threshold_sec=gap_threshold_sec,
            queue_backlog_floor=queue_backlog_floor,
            queue_stuck_window_sec=queue_stuck_window_sec,
        )
        # 메모리 추이 기록. 상태 파일은 덮어쓰이므로 별도 이력을 남긴다.
        # raw v2 세션 폴더가 로그인 뒤에야 정해지므로 여기서는 자리만 잡는다.
        self.resources: ResourceHistory | None = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False
        self._shutdown_requested = None
        self.exit_code = 0
        self._install_sigint_handler()

        # 고속 메모리 큐 (UI 렉 방지)
        self.trade_queue = queue.Queue()
        self.quote_queue = queue.Queue()
        self.is_running = True
        self.accepting_events = True
        self.writer = None
        if storage == "raw-v1":
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            self.writer = TickWriter(self.db_path, self.trade_queue, self.quote_queue)

        # 키움 OCX 초기화
        try:
            self.ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        except Exception as e:
            self.log.emit(f"❌ 키움 OCX 로드 실패 (32비트 가상환경인지 확인하세요): {e}")
            sys.exit(1)

        # 이벤트 시그널 연결
        self.ocx.OnEventConnect.connect(self._on_login)
        self.ocx.OnReceiveRealData.connect(self._on_receive_real_data)

        self.app.aboutToQuit.connect(lambda: self._shutdown("Qt 종료"))

    @property
    def total_trades(self):
        return self.writer.total_trades if self.storage == "raw-v1" else self.raw_capture.received_trades

    @property
    def total_quotes(self):
        return self.writer.total_quotes if self.storage == "raw-v1" else self.raw_capture.received_quotes

    def _poll_control(self):
        # OCX calls and shutdown run on the Qt thread, between event callbacks.
        # In particular, SIGINT must not drain while a callback is about to put().
        if self._shutdown_done:
            return
        if self._transition_active:
            return  # 재진입한 Qt 타이머가 전환 중인 writer를 닫지 않는다.
        if self.managed is not None:
            try:
                if self.managed.poll():
                    self._shutdown_requested = "관리 화면 종료 요청"
            except Exception as exc:
                if self.raw_capture is not None:
                    self.raw_capture.queue.abort(f"managed control failed: {exc}")
                self.exit_code = 2
                self._shutdown_requested = "관리 채널 오류"
        if self.writer is not None and self.writer.error:
            self._shutdown_requested = "DB 저장 오류"
        if self.raw_capture is not None:
            try:
                self.raw_capture.write_status()
                if int(self.ocx.dynamicCall("GetConnectState()")) != 1:
                    self.raw_capture.queue.abort("OCX connection lost")
                    self._shutdown_requested = "OCX 연결 끊김"
            except Exception as exc:
                self.raw_capture.queue.abort(f"control polling failed: {exc}")
                self._shutdown_requested = "수집 상태 확인 오류"
        # 전환은 Qt 스레드에서 한 번만 부른다 — _stats_worker(별도 스레드)는 조건만
        # 확인해 이 플래그를 세우고, 실제 OCX 호출은 여기서만 한다. 이번 tick에
        # 종료 사유가 이미 잡혔으면 전환을 새로 시작하지 않는다.
        if (self._aftermarket_transition_due and self.transition is None
                and not self._shutdown_requested):
            self._aftermarket_transition_due = False
            self.run_aftermarket_transition()
            return
        if self._shutdown_requested:
            self._shutdown(self._shutdown_requested)

    def start(self):
        if self.managed is not None and self.managed.poll():
            self._shutdown("로그인 전 취소")
            return self.exit_code
        self.monitor.start()
        self.log.emit("=" * 65)
        self.log.emit("🚀 [키움증권 보통주 전 종목 실시간 틱/호가 수집 데몬]")
        self.log.emit(f"📁 저장 방식: {self.storage}; raw v2는 로그인 확인 후 새 파일 생성")
        self.log.emit(f"📝 로그 파일: {self.log_path}")
        if self.diagnostics is not None:
            self.log.emit(f"🧩 충돌/침묵 진단: {self.diagnostics.path}")
        self.log.emit("=" * 65)
        self.log.emit("🔑 키움 OpenAPI+ 서버 접속 시도 중...")
        self.ocx.dynamicCall("CommConnect()")
        try:
            self.app.exec_()
        finally:
            self._shutdown("이벤트 루프 종료")
        return self.exit_code

    def _on_login(self, err_code: int):
        if self._shutdown_done:
            return
        if self.managed is not None:
            try:
                if self.managed.poll():
                    self._shutdown("로그인 중 취소")
                    return
            except Exception as exc:
                self.exit_code = 2
                self._shutdown(f"관리 채널 오류: {exc}")
                return
        if self._login_handled:
            self.exit_code = 2
            if self.raw_capture is not None:
                self.raw_capture.queue.abort("unexpected login/reconnect event")
            self._shutdown("중복 로그인/재접속 이벤트")
            return
        self._login_handled = True
        if err_code != 0:
            self.exit_code = 2
            self.log.emit(f"❌ 로그인 실패 (에러코드: {err_code})")
            self._shutdown(f"로그인 실패 (에러코드 {err_code})")
            return

        self.log.emit("🎉 [성공] 키움증권 서버 로그인 완료!")

        # 1. 백그라운드 DB 저장 워커 스레드 가동
        if self.storage == "raw-v2":
            try:
                flag = str(self.ocx.dynamicCall("KOA_Functions(QString, QString)", "GetServerGubun", "")).strip()
                server = {"1": "mock", "0": "live"}.get(flag)
                if server is None:
                    raise ValueError(f"unknown server flag: {flag!r}")
                if self.managed is not None and server != self.managed.plan["server"]:
                    raise ValueError("observed server does not match requested server")
                self.raw_capture = LiveRawCapture(PROJECT_ROOT, server=server, code_revision=self.code_revision)
                if self.managed is not None:
                    self.managed.bind(self.raw_capture.report)
                self.writer = self.raw_capture
                self.db_path = self.raw_capture.path
                self.log.emit(f"📁 raw v2 저장 파일: {self.db_path}")
                self.log.emit(f"📝 세션 상태: {self.raw_capture.directory / 'status.json'}")
                self.resources = ResourceHistory(
                    self.raw_capture.directory / "resource_history.jsonl", clock=time.monotonic)
                self.resources.sample(force=True)
                if self.plan is not None:
                    # 목록 근거·시장 프로필·구독 원문을 세션에 남긴다. 상태 파일과 달리 덮어쓰지 않는다.
                    record = self.raw_capture.directory / "subscription_plan.json"
                    record.write_text(json.dumps(self.plan.describe(), ensure_ascii=False, indent=2),
                                      encoding="utf-8")
                    self.log.emit(f"📝 구독 계획 근거: {record}")
            except Exception as exc:
                self.exit_code = 2
                self.log.emit(f"❌ raw v2 시작 실패: {exc}")
                if self.raw_capture is not None:
                    self.raw_capture.queue.abort(f"startup failed: {exc}")
                self._shutdown("raw v2 시작 실패")
                return
        else:
            self.db_thread = threading.Thread(target=self.writer.run, daemon=False)
            self.db_thread.start()

        # 2. 통계 출력 스레드 가동
        self.stats_thread = threading.Thread(target=self._stats_worker, daemon=True)
        self.stats_thread.start()

        # 3. 보통주 선별 및 26개 화면 분할 등록
        try:
            self._register_all_universe()
            self._subscribed_at = time.monotonic()
            self.monitor.mark_reception_expected()
        except Exception as exc:
            self.log.emit(f"❌ 실시간 등록 실패: {exc}")
            if self.raw_capture is not None:
                self.raw_capture.queue.abort(f"registration failed: {exc}")
            self.exit_code = 2
            self._shutdown("실시간 등록 실패")

    def _register_all_universe(self):
        """코스피/코스닥에서 순수 보통주만 선별하여 100개씩 화면번호 분할 등록"""
        if self.plan is not None:
            return self._register_plan()
        self.log.emit("🔍 코스피 및 코스닥 전 종목 코드 추출 중...")
        kospi_raw = self.ocx.dynamicCall("GetCodeListByMarket(QString)", "0")
        kosdaq_raw = self.ocx.dynamicCall("GetCodeListByMarket(QString)", "10")

        kospi_codes = [c.strip() for c in kospi_raw.split(";") if c.strip()]
        kosdaq_codes = [c.strip() for c in kosdaq_raw.split(";") if c.strip()]
        all_codes = kospi_codes + kosdaq_codes

        self.log.emit(f"📊 시장 전체 종목 수: 코스피 {len(kospi_codes)}개 + 코스닥 {len(kosdaq_codes)}개 = 총 {len(all_codes)}개")

        # 1. 보통주만 필터링 (우선주, ETF, ETN, 스팩 제외)
        self.log.emit("🧹 보통주 선별 필터링 진행 중 (ETF, ETN, 스팩, 우선주 제외)...")
        filtered_codes = []
        for c in all_codes:
            if not c.endswith("0"):
                continue
            name_raw = self.ocx.dynamicCall("GetMasterCodeName(QString)", c)
            name = str(name_raw).strip() if name_raw else ""
            if any(keyword in name for keyword in ["스팩", "ETF", "ETN", "KoTop"]):
                continue
            filtered_codes.append(c)

        self.log.emit(f"📊 코드/이름 필터 적용: {len(filtered_codes)}개 후보 (종목 분류 정확성은 별도 확인)")
        if self.codes is not None:
            if not set(self.codes) <= set(filtered_codes):
                raise ValueError("requested codes absent from observed common-stock universe")
            filtered_codes = list(self.codes)
        if not filtered_codes:
            raise ValueError("empty collection universe")

        # 2. FID 리스트 (27:최우선매도호가, 28:최우선매수호가 추가로 정밀 매수 판정 지원)
        fids = REAL_FIDS

        # 3. 100개씩 청크 분할하여 화면번호(1000, 1001, ...) 부여
        chunk_size = 100
        screen_base = 1000

        self.log.emit("📡 보통주 실시간 구독 등록 시작 (화면번호 분할 매핑)...")
        for idx in range(0, len(filtered_codes), chunk_size):
            chunk = filtered_codes[idx : idx + chunk_size]
            screen_no = str(screen_base + (idx // chunk_size))
            code_str = ";".join(chunk)

            result = self.ocx.dynamicCall(
                "SetRealReg(QString, QString, QString, QString)",
                screen_no,
                code_str,
                fids,
                "0"
            )
            if self.storage == "raw-v2" and str(result).strip() != "0":
                raise RuntimeError(f"SetRealReg rejected screen {screen_no}: {result!r}")
            time.sleep(0.05)

        self.log.emit(f"✅ 코스피/코스닥 보통주 ({len(filtered_codes)}개) 실시간 수신 등록 완료!")
        self.log.emit("🔴 실시간 체결/호가 수집 가동 중... (종료하려면 터미널에서 Ctrl + C)")

    def run_aftermarket_transition(self):
        """정규장 저장을 마치고 애프터마켓 저장으로 넘긴다. 명시적 모드에서만 부른다.

        정규장 저장이 닫혔다고 확인된 뒤에만 다음 세션을 연다. 어느 단계든 실패하면
        다음으로 넘어가지 않고 실패 위치를 기록에 남긴다. 닫힌 정규장 파일은 다시 열지 않는다.
        """
        if threading.get_ident() != self._main_thread_ident:
            # OCX(ActiveX) 는 그것을 만든 스레드에서만 안전하다. _stats_worker(백그라운드
            # 스레드)는 트리거 플래그만 세우고, 실제 호출은 Qt 스레드의 _poll_control 이 한다.
            raise RuntimeError("애프터마켓 전환은 OCX 를 만든 스레드에서만 부를 수 있다")
        if self.aftermarket_plan is None:
            raise ValueError("애프터마켓 전환 계획이 없다")
        if self.transition is not None:
            raise ValueError("전환은 한 번만 실행한다")
        regular = self.raw_capture
        if regular is None or self._shutdown_done or self._shutdown_requested:
            raise ValueError("실행 중인 정규장 세션이 필요하다")
        plan = self.aftermarket_plan
        # 전환 중 도착하는 콜백의 활성 구독 문맥. 참고 정보일 뿐 실제 출처를 확정하지 않는다.
        self._regular_subscription_context = list(self.codes) if self.codes else "전체 관측 보통주"
        self._pending_aftermarket_codes = list(plan.codes)

        def unsubscribe():
            self.accepting_events = False          # 신규 입력 차단이 먼저다
            result = self.ocx.dynamicCall("SetRealRemove(QString, QString)", "ALL", "ALL")
            # void 반환(None/빈 문자열)도 허용하되 명시적 오류 코드는 무시하지 않는다.
            if result is not None and str(result).strip() not in ("", "0"):
                raise RuntimeError(f"SetRealRemove rejected: {result!r}")
            return result

        def finalize():
            # 닫힘과 마무리 정보를 확인해 돌려준다. 여기서 거짓이면 다음 단계로 가지 않는다.
            if regular is None:
                return False
            clean = regular.finish("애프터마켓 전환")
            snapshot = regular.queue.snapshot()
            return bool(clean and snapshot["state"] == "closed" and snapshot["writer_closed"]
                        and snapshot["finalization"] and not snapshot["pending_callbacks"]
                        and not snapshot["dropped_callbacks"] and not snapshot["error"]
                        and snapshot["accepted_callbacks"] == snapshot["committed_callbacks"])

        def open_aftermarket():
            # 모듈 수준 참조를 그대로 쓴다. 함수 안에서 다시 import 하면 로그인 경로와
            # 생성 지점이 갈리고, 대역을 끼워 넣을 수도 없다.
            self.raw_capture = LiveRawCapture(
                PROJECT_ROOT, server=regular.identity.server, code_revision=self.code_revision)
            self.transition.record.next_session_id = self.raw_capture.identity.session_id
            snapshot = self.raw_capture.queue.snapshot()
            if (self.raw_capture.identity.session_id == regular.identity.session_id
                    or self.raw_capture.path == regular.path
                    or snapshot["state"] != "running" or not snapshot["accepting"]
                    or snapshot["error"] or snapshot["writer_closed"]):
                raise RuntimeError("별도 애프터마켓 세션 준비가 확인되지 않았다")
            self.writer = self.raw_capture
            self.db_path = self.raw_capture.path
            self.plan = plan                       # 이후 기록은 애프터마켓 계획을 따른다
            # 새 세션에는 새 감시 상태를 쓴다 — 정규장의 수신 시각·침묵 권고·카운터를
            # 이어받지 않도록 인스턴스 자체를 교체한다.
            self.monitor = SessionMonitor(
                sessions=resolve_profile(plan.market_profile),
                gap_threshold_sec=self._gap_threshold_sec,
                queue_backlog_floor=self._queue_backlog_floor,
                queue_stuck_window_sec=self._queue_stuck_window_sec,
            )
            self.monitor.start()
            self.resources = ResourceHistory(
                self.raw_capture.directory / "resource_history.jsonl", clock=time.monotonic)
            record = self.raw_capture.directory / "subscription_plan.json"
            record.write_text(json.dumps(plan.describe(), ensure_ascii=False, indent=2),
                              encoding="utf-8")
            return self.raw_capture.identity.session_id

        def subscribe():
            result = self._register_plan()
            # 애프터마켓 구간 자체의 상한을 여기서부터 잰다 — 정규장 로그인 시각을
            # 이어받으면 이미 지난 시간으로 곧장 종료될 수 있다.
            self._aftermarket_subscribed_at = self._monotonic()
            return result

        self._transition_active = True
        self.accepting_events = False
        self.transition = SessionTransition(
            unsubscribe_regular=unsubscribe, finalize_regular=finalize,
            open_aftermarket=open_aftermarket, subscribe_nxt=subscribe,
            clock=lambda: self._monotonic(), persist=self._write_transition_record,
            cancelled=lambda: bool(self._shutdown_requested or not self.is_running))
        record = self.transition.run(
            last_event_before=self._last_received_monotonic,
            last_event_before_utc=self._last_received_utc,
            prev_session_id=regular.identity.session_id if regular is not None else None,
            code_revision=self.code_revision,
            plan_revision=plan.describe())
        if self.raw_capture is not regular and self.raw_capture is not None:
            record.next_session_id = self.raw_capture.identity.session_id
        self.log.end_status_line()
        if record.succeeded:
            self.monitor.mark_reception_expected()
            self.accepting_events = True  # 최종 필수 기록까지 성공한 뒤에만 입력을 연다.
            self.log.emit(f"🔄 애프터마켓 전환 완료 ({record.elapsed_sec:.1f}초) — {self.db_path}")
        else:
            self.exit_code = 2
            self.accepting_events = False
            cleanup = []
            try:
                result = self.ocx.dynamicCall("SetRealRemove(QString, QString)", "ALL", "ALL")
                cleanup.append(dict(resource="subscriptions", result=repr(result),
                    confirmed=result is None or str(result).strip() in ("", "0")))
            except Exception as exc:
                cleanup.append(dict(resource="subscriptions", error=str(exc)))
            for capture in (regular,) if self.raw_capture is regular else (regular, self.raw_capture):
                snapshot = capture.queue.snapshot()
                if snapshot["writer_closed"]:
                    continue  # 닫힌 정규장 파일·보고를 다시 쓰지 않는다.
                capture.queue.abort(f"transition failed: {record.error}")
                stopped = capture.queue.wait(5)
                cleanup.append(dict(resource=capture.identity.session_id, worker_stopped=stopped,
                                    writer_closed=capture.queue.snapshot()["writer_closed"]))
            record.cleanup = tuple(cleanup)
            try:
                self.transition.persist()
            except Exception:
                pass  # 이미 기록 실패를 record에 보존했다. 다음 단계나 재시도는 없다.
            self.log.emit(f"❌ 애프터마켓 전환 실패: {record.failed_step} — {record.error}")
            self._shutdown_requested = f"애프터마켓 전환 실패 ({record.failed_step})"
        self._transition_active = False
        if self._transition_record_path is not None:
            self.log.emit(f"📝 전환 진단 경로: {self._transition_record_path}")
        if record.recording_error:
            self.log.emit(f"❌ 전환 진단 저장 미확인: {record.recording_error}")
        if record.callbacks_during or record.callbacks_dropped:
            self.log.emit(f"⚠️ 전환 중 콜백 {len(record.callbacks_during)}건 보존"
                          f"{f', {record.callbacks_dropped}건 한도 초과로 누락' if record.callbacks_dropped else ''}"
                          " — 진단 저장 성공 여부는 recording_error와 함께 확인한다")
        return record

    def _write_transition_record(self):
        """새 세션 생성 전부터 같은 별도 경로에 단계별 진단을 저장한다."""
        record = self.transition.record
        if self._transition_record_path is None:
            self._transition_record_path = (self.raw_capture.directory.parent.parent
                / "session_transitions" / f"{record.transition_id}.json")
        write_transition_record(self._transition_record_path, record.describe())

    def _register_plan(self):
        """구독 계획의 코드만 등록한다. 조회로 받는 여섯 자리 목록을 NXT 대상으로 쓰지 않는다.

        접미사 코드는 조회 목록에 없으므로 기존 '관측된 보통주에 있는가' 검사를 적용하지 않는다.
        대신 목록의 출처·확인 시각을 기록에 남겨 근거를 추적 가능하게 둔다.
        """
        plan = self.plan
        self.log.emit(f"📡 구독 계획 등록 — {plan.mode} / {plan.market_profile} / {len(plan.codes)}종목")
        self.log.emit(f"   목록 출처: {plan.source.origin} (확인 {plan.source.verified_at})")
        self.log.emit("   NXT 거래 대상 확인: "
                      + ("확인됨" if plan.source.nxt_eligibility_confirmed else "미확인 — 사용자 입력일 뿐이다"))
        result = self.ocx.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            "1000", ";".join(plan.codes), REAL_FIDS, "0")
        if str(result).strip() != "0":
            raise RuntimeError(f"SetRealReg rejected plan codes: {result!r}")
        self.log.emit(f"✅ 실시간 수신 등록 완료: {', '.join(plan.codes)}")
        self.log.emit("접미사는 라우팅 근거이며 개별 체결 venue 와 NXT coverage 는 미확인이다.")
        return result

    def _on_receive_real_data(self, code: str, real_type: str, real_data: str):
        """Capture entry clocks before extracting raw FIDs on the Qt thread."""
        if not self.accepting_events:
            # 전환 중이면 저장 파일 대신 전환 기록에 보존한다. 조용히 버리지 않는다.
            if self.transition is not None and self.transition.in_progress:
                fids = {}
                if real_type in ("주식체결", "주식호가잔량"):
                    try:
                        for fid in (TRADE_FIDS if real_type == "주식체결" else QUOTE_FIDS):
                            fids[str(fid)] = self.ocx.dynamicCall("GetCommRealData(QString, int)", code, fid)
                    except Exception as exc:
                        fids["_read_error"] = f"{type(exc).__name__}: {exc}"
                self.transition.accept_callback(
                    code=code, real_type=real_type, fids=fids,
                    subscription_context=dict(
                        regular=self._regular_subscription_context,
                        aftermarket_plan_codes=self._pending_aftermarket_codes))
            return
        if self.storage == "raw-v2":
            received_monotonic = self._monotonic()
            received_ns = time.perf_counter_ns()
            received_at_utc = datetime.now(timezone.utc).isoformat()
            if self.raw_capture is None:
                return
            try:
                accepted = self.raw_capture.on_tick(code, real_type,
                    lambda fid: self.ocx.dynamicCall("GetCommRealData(QString, int)", code, fid),
                    received_ns=received_ns, received_at_utc=received_at_utc)
                if accepted:
                    self._last_received_monotonic = received_monotonic
                    self._last_received_utc = received_at_utc
                    if real_type == "주식체결":
                        self.monitor.on_trade()
                    elif real_type == "주식호가잔량":
                        self.monitor.on_quote()
                    if self.transition is not None:
                        self.transition.note_first_event_after(
                            at=received_monotonic, at_utc=received_at_utc)
                elif real_type in ("주식체결", "주식호가잔량"):
                    self._shutdown_requested = "raw v2 큐 입력 거부"
            except Exception as exc:
                self.accepting_events = False
                self.exit_code = 2
                self.raw_capture.queue.abort(f"callback failed: {exc}")
                self._shutdown_requested = "raw v2 콜백 오류"
            return
        now_str = datetime.now().strftime("%H%M%S")

        # 1. 주식체결 수신
        if real_type == "주식체결":
            p_raw = self.ocx.dynamicCall("GetCommRealData(QString, int)", code, 10).strip()
            v_raw = self.ocx.dynamicCall("GetCommRealData(QString, int)", code, 15).strip()
            che_gb = self.ocx.dynamicCall("GetCommRealData(QString, int)", code, 14).strip()
            ask_raw = self.ocx.dynamicCall("GetCommRealData(QString, int)", code, 27).strip()

            if p_raw and v_raw:
                try:
                    price = float(abs(int(p_raw)))
                    vol = abs(int(v_raw))
                    ask_p = float(abs(int(ask_raw))) if ask_raw else 0.0

                    # ⭐️ 2중 방어 매수 판정:
                    # 1) che_gb 문자열에 매수 표시가 있거나
                    # 2) 체결가가 당시 매도1호가(ask_p) 이상이면 무조건 시장가 매수(1)!
                    is_buy = 0
                    if che_gb.startswith("+") or che_gb == "1" or "매수" in che_gb:
                        is_buy = 1
                    elif ask_p > 0 and price >= ask_p:
                        is_buy = 1

                    self.trade_queue.put((now_str, code, price, vol, is_buy))
                    # 실제로 큐에 들어간 시점만 수신으로 친다. 카운터 증가와
                    # 타임스탬프 대입뿐이라 I/O 도 락도 붙지 않는다.
                    self.monitor.on_trade()
                except ValueError:
                    pass

        # 2. 주식호가잔량 수신 (10단계 호가)
        elif real_type == "주식호가잔량":
            try:
                offer_p = ",".join([str(abs(int(self.ocx.dynamicCall("GetCommRealData(QString, int)", code, f).strip() or 0))) for f in range(41, 51)])
                bid_p = ",".join([str(abs(int(self.ocx.dynamicCall("GetCommRealData(QString, int)", code, f).strip() or 0))) for f in range(51, 61)])
                offer_v = ",".join([str(abs(int(self.ocx.dynamicCall("GetCommRealData(QString, int)", code, f).strip() or 0))) for f in range(61, 71)])
                bid_v = ",".join([str(abs(int(self.ocx.dynamicCall("GetCommRealData(QString, int)", code, f).strip() or 0))) for f in range(71, 81)])

                self.quote_queue.put((now_str, code, offer_p, offer_v, bid_p, bid_v))
                self.monitor.on_quote()
            except Exception:
                pass

    def _stats_worker(self):
        """수집 현황 주기 출력 + 장중 침묵/대기큐 적체 감시 + 장 마감 종료 (기존 1초 루프)"""
        while self.is_running:
            if self._shutdown_requested:
                break
            if self._transition_active:
                time.sleep(1.0)
                continue
            now = datetime.now()
            now_int = int(now.strftime("%H%M%S"))
            now_str = now.strftime("%H:%M:%S")
            queue_depth = self.writer.pending if self.storage == "raw-v2" else self.trade_queue.qsize() + self.quote_queue.qsize()

            # 침묵/적체 판정은 이 주기 루프 안에서만 한다. 이벤트 수신 경로에는
            # 판정도 I/O 도 붙이지 않는다.
            silence_notice = self.monitor.tick()
            if silence_notice and self.diagnostics is not None:
                # 정체 시점의 Qt/FID 조회/저장 스택을 구간당 한 번, 실행당 최대 세 번 보존한다.
                # 회복 알림은 진단하지 않고, 진단 실패가 운영 수집을 중단시키지 않게 한다.
                if "⚠️" in silence_notice:
                    try:
                        self.diagnostics.record_stall(self.monitor.last_event_ts, dict(
                            last_event_ts=self.monitor.last_event_ts, queue_depth=queue_depth,
                            trades=self.total_trades, quotes=self.total_quotes,
                            session_id=(self.raw_capture.identity.session_id if self.raw_capture else None)))
                    except Exception as exc:
                        self.log.emit(f"⚠️ 침묵 진단 기록 실패: {type(exc).__name__}: {exc}")
            for notice in (silence_notice, self.monitor.sample_queue_depth(queue_depth)):
                if notice:
                    self.log.end_status_line()   # 상태줄(\r) 위에 겹쳐 찍히지 않도록
                    self.log.emit(notice)

            if self.resources is not None:
                self.resources.sample()

            # 장중 침묵이 한도를 넘으면 종료를 '시도' 한다. 파일 닫힘을 보장하지는 않는다 —
            # Qt/OCX 가 멈춘 경우 기존 종료 경로 자체가 돌지 않을 수 있다.
            stop_reason = self.monitor.silence_stop_reason()
            if stop_reason is not None:
                self.log.end_status_line()
                self.log.emit(f"🛑 {stop_reason}")
                if self.diagnostics is not None:
                    try:
                        self.diagnostics.record_stop(dict(reason=stop_reason,
                            last_event_ts=self.monitor.last_event_ts, queue_depth=queue_depth,
                            trades=self.total_trades, quotes=self.total_quotes))
                    except Exception as exc:
                        self.log.emit(f"⚠️ 종료 직전 진단 실패: {type(exc).__name__}: {exc}")
                if self.resources is not None:
                    sample = self.resources.sample(force=True)
                    if sample is not None:
                        self.log.emit(
                            f"   종료 직전 메모리: 커밋 {sample.commit/1048576:,.0f} MiB "
                            f"(최대 {sample.peak_commit/1048576:,.0f} MiB) — 단서일 뿐 원인 판정이 아님")
                self.exit_code = 2          # 수신 결손 상태의 종료는 정상 종료가 아니다
                self._shutdown_requested = "장중 침묵 한도 초과 (수신 결손)"
                break

            # 명시적 전환 트리거 감지. 실제 OCX 호출(run_aftermarket_transition)은
            # Qt 스레드의 _poll_control 에서만 한다 — 여기서는 조건만 세운다.
            # self.transition is None 가드가 한 번만 세우는 것을 보장한다: 전환이
            # 시작되면(성공이든 실패든) 바로 채워지고, 트리거는 다시 세워지지 않는다.
            if (self.aftermarket_plan is not None and self.transition is None
                    and not self._aftermarket_transition_due):
                if self._aftermarket_transition_at is not None and now_int >= self._aftermarket_transition_at:
                    self._aftermarket_transition_due = True
                elif (self._aftermarket_transition_after_seconds is not None
                      and self._subscribed_at is not None
                      and time.monotonic() - self._subscribed_at >= self._aftermarket_transition_after_seconds):
                    self._aftermarket_transition_due = True

            if self._aftermarket_subscribed_at is not None:
                # 전환 뒤 애프터마켓 구간 자체의 상한이다. 정규장 15:35 종료는 계획 모드에
                # 적용하지 않으므로, 이 상한이 없으면 전환 뒤에는 종료 조건이 없어진다.
                if self._monotonic() - self._aftermarket_subscribed_at >= self.aftermarket_duration_seconds:
                    self._shutdown_requested = "애프터마켓 제한 시간 종료"
                    break
            elif self.duration_seconds is not None:
                if self._subscribed_at is not None and time.monotonic() - self._subscribed_at >= self.duration_seconds:
                    self._shutdown_requested = "제한 수집 시간 종료"
                    break
            elif self.plan is None and self.aftermarket_plan is None and now_int >= 153500:
                # 정규장 전용 종료다. 구독 계획 모드(애프터마켓 포함)에는 적용하지 않는다 —
                # 15:35 를 20:00 으로 미루는 것으로는 부족하다는 런북 지적에 따른다.
                # 명시적 전환 트리거가 있는 실행도 여기서 멈추지 않는다 — 트리거가 스스로
                # 정규장 저장을 마무리하는 절차를 가지고 있다.
                self.log.end_status_line()
                self.log.emit("🔔 [15:35 장 마감 감지] 전 종목 수집을 종료합니다.")
                self._shutdown_requested = "장 마감 (15:35)"
                break

            self.log.status(
                f"⏱️ [{now_str}] {'수신 콜백' if self.storage == 'raw-v2' else 'DB 반영'} ➔ "
                f"체결: {self.total_trades:,}건 | 호가: {self.total_quotes:,}건 "
                f"(대기큐: {queue_depth:,})"
            )
            time.sleep(1.0)

    def _shutdown(self, reason: str):
        """
        모든 종료 경로가 지나는 단 하나의 출구.

        여기서 세션 보고서를 만든다. 마지막 수신 이후 장중 무이벤트 구간이
        임계를 넘었으면 '정상 종료' 라고 말하지 않는다 — 2026-09-14 에
        54분치 결손을 정상 종료로 보고했던 일의 재발 방지다.
        """
        if self._transition_active:
            self._shutdown_requested = reason
            return
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        self.accepting_events = False
        self.is_running = False
        self.timer.stop()
        try:
            self.ocx.dynamicCall("SetRealRemove(QString, QString)", "ALL", "ALL")
        except Exception as e:
            self.log.emit(f"⚠️ 실시간 등록 해제 실패: {e}")

        stats = getattr(self, "stats_thread", None)
        if stats is not None:
            stats.join()
        # Freeze reception end before disk drain; drain time is not a feed gap.
        report = self.monitor.finish(reason)
        if self.resources is not None:
            summary = self.resources.snapshot()
            if summary is not None:
                report.extra.append((
                    "메모리(커밋)  ",
                    f"마지막 {summary['commit']/1048576:,.0f} MiB / 최대 {summary['peak_commit']/1048576:,.0f} MiB "
                    f"— 이력 {summary['history_path']}"))
        self.log.emit("💾 수신을 멈추고 남은 체결/호가의 DB 커밋을 기다립니다.")
        if self.storage == "raw-v2":
            command = self.managed.stop[1] if self.managed is not None and self.managed.stop else None
            clean = self.raw_capture is not None and self.raw_capture.finish(reason, command=command)
            if not clean:
                cancelled_before_capture = self.raw_capture is None and self.managed is not None and self.managed.stop
                if not cancelled_before_capture:
                    self.exit_code = 2
                self.log.emit(f"❌ raw v2 종료 미확인: {self.raw_capture.error if self.raw_capture else reason}")
            else:
                snapshot = self.raw_capture.queue.snapshot()
                self.log.emit(f"💾 raw v2 저장 완료: {snapshot['committed_seq']:,} 레코드 / 콜백 {snapshot['accepted_callbacks']:,}건")
                self.log.emit("데이터 방향·venue·무누락 품질은 미검증입니다.")
                self.log.emit(report.headline)
            self.log.emit_all(report.lines())
            self.log.close()
            self.app.quit()
            return
        self.writer.stop.set()
        worker = getattr(self, "db_thread", None)
        if worker is not None:
            while worker.is_alive():
                worker.join(timeout=5.0)
                if worker.is_alive():
                    self.log.emit(f"💾 종료 저장 중: 미커밋 약 {self.writer.pending:,}건")
        pending = self.writer.pending
        storage_failed = bool(self.writer.error or pending)
        report.extra.extend([
            ("DB 반영 건수  ", f"체결 {self.total_trades:,} 건 / 호가 {self.total_quotes:,} 건"),
            ("종료 후 미커밋", f"{pending:,} 건"),
            ("DB 저장 결과  ", self.writer.error or ("미저장 잔여 있음" if pending else "커밋 완료")),
        ])
        if storage_failed:
            self.exit_code = 2
            self.log.emit(f"❌ 비정상 종료 — DB 저장 실패 / 미커밋 {pending:,}건. 재시작으로 복구되지 않습니다.")
        else:
            self.log.emit(report.headline)
        self.log.emit_all(report.lines())
        self.log.close()
        self.app.quit()

    def _install_sigint_handler(self):
        """
        첫 Ctrl+C 는 세션 요약을 남기고 종료하고, 두 번째 Ctrl+C 는 OS 기본
        동작(강제 종료)으로 되돌린다. 강제 종료라는 탈출구는 그대로 두되,
        평범한 중단에서 요약이 통째로 사라지지 않게 하려는 것이다.
        (200ms QTimer 가 돌고 있어 파이썬 시그널 핸들러가 실제로 실행된다)
        """
        def _handler(signum, frame):
            signal.signal(signal.SIGINT, signal.SIG_DFL)   # 다음 번엔 강제 종료
            self.log.end_status_line()
            self.log.emit("🛑 사용자에 의해 수집이 중단되었습니다 (Ctrl+C).")
            self._shutdown_requested = "사용자 중단 (Ctrl+C)"

        signal.signal(signal.SIGINT, _handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kiwoom collector; default raw-v2, no orders")
    parser.add_argument("--storage", choices=("raw-v2", "raw-v1"), default="raw-v2")
    parser.add_argument("--codes", help="explicit comma-separated common-stock codes for a small run")
    parser.add_argument("--duration-seconds", type=int)
    parser.add_argument("--preflight", action="store_true", help="read-only environment check; no OCX instance/login")
    parser.add_argument("--managed-launch", help=argparse.SUPPRESS)
    nxt = parser.add_argument_group(
        "NXT 구독 계획", "명시적 NXT 모드. 독립 raw-v2 검증 실행이며 제한 시간이 필요하다")
    nxt.add_argument("--nxt-codes", help="쉼표로 구분한 _NX 종목코드 (예: 005930_NX)")
    nxt.add_argument("--list-origin", help="이 목록이 어디서 왔는가 (예: 사용자 입력)")
    nxt.add_argument("--list-verified-at", help="목록을 확인한 시각 (예: 2026-09-17T16:30:00+09:00)")
    nxt.add_argument("--nxt-eligibility-confirmed", action="store_true",
                     help="공식 수단으로 NXT 거래 대상임을 확인했을 때만 지정한다. "
                          "입력했다는 사실과 확인했다는 사실은 다르다")
    nxt.add_argument("--list-note", default="", help="목록 근거에 남길 메모")
    nxt.add_argument("--market-profile", default="nxt_aftermarket",
                     help="시장 구간 프로필 (기본: nxt_aftermarket)")
    after = parser.add_argument_group(
        "애프터마켓 전환 (단일 로그인)",
        "명시적 모드. 같은 로그인 안에서 정규장 저장을 마치고 애프터마켓 저장으로 넘긴다. "
        "전환 시각 또는 경과 시간 트리거 중 하나가 필요하며, 독립 raw-v2/비관리 실행에서만 쓴다")
    after.add_argument("--aftermarket-nxt-codes", help="전환 후 구독할 쉼표구분 _NX 종목코드 (예: 005930_NX)")
    after.add_argument("--aftermarket-list-origin", help="전환 후 목록이 어디서 왔는가 (예: 사용자 입력)")
    after.add_argument("--aftermarket-list-verified-at", help="전환 후 목록을 확인한 시각")
    after.add_argument("--aftermarket-nxt-eligibility-confirmed", action="store_true",
                       help="공식 수단으로 NXT 거래 대상임을 확인했을 때만 지정한다")
    after.add_argument("--aftermarket-list-note", default="", help="전환 후 목록 근거에 남길 메모")
    after.add_argument("--aftermarket-market-profile", default="nxt_aftermarket",
                       help="전환 후 시장 구간 프로필 (기본: nxt_aftermarket)")
    after.add_argument("--aftermarket-duration-seconds", type=int,
                       help="전환 후 애프터마켓 구간 자체의 종료 상한 (1~300초, 전환 모드 필수)")
    after.add_argument("--aftermarket-transition-at",
                       help="전환을 실행할 KST 시각 HH:MM:SS (경과 시간 트리거와 함께 줄 수 없다)")
    after.add_argument("--aftermarket-transition-after-seconds", type=int,
                       help="정규장 구독 등록 완료 후 몇 초 뒤 전환할지 (전환 시각 트리거와 함께 줄 수 없다)")
    args = parser.parse_args()
    if args.preflight:
        from collector.kiwoom.preflight import inspect_environment
        result = inspect_environment()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result["ready"] else 2)
    codes = None if not args.codes else list(dict.fromkeys(args.codes.split(",")))
    if codes is not None and (not 1 <= len(codes) <= 10 or any(len(c) != 6 or not c.isdigit() for c in codes)):
        parser.error("--codes requires 1..10 six-digit codes")
    plan = None
    if args.nxt_codes:
        if codes is not None:
            parser.error("--nxt-codes 와 --codes 는 함께 쓸 수 없다")
        from collector.kiwoom.subscription_plan import plan_from_cli
        try:
            plan = plan_from_cli(
                nxt_codes=args.nxt_codes, list_origin=args.list_origin,
                list_verified_at=args.list_verified_at, market_profile=args.market_profile,
                nxt_eligibility_confirmed=args.nxt_eligibility_confirmed,
                note=args.list_note, duration_seconds=args.duration_seconds)
        except ValueError as exc:
            parser.error(str(exc))
    elif any((args.list_origin, args.list_verified_at, args.nxt_eligibility_confirmed)):
        parser.error("목록 근거 인자는 --nxt-codes 와 함께 써야 한다")
    if args.duration_seconds is not None and plan is None and (
            codes is None or not 1 <= args.duration_seconds <= 300):
        parser.error("--duration-seconds requires --codes and 1..300 seconds")
    aftermarket_plan = None
    aftermarket_duration_seconds = None
    aftermarket_transition_at = None
    aftermarket_transition_after_seconds = None
    aftermarket_given = any((
        args.aftermarket_nxt_codes, args.aftermarket_list_origin, args.aftermarket_list_verified_at,
        args.aftermarket_duration_seconds is not None, args.aftermarket_transition_at,
        args.aftermarket_transition_after_seconds is not None,
        args.aftermarket_nxt_eligibility_confirmed, args.aftermarket_list_note))
    if aftermarket_given:
        # 상충 인자는 OCX(QApplication/OCX 컨트롤) 를 만들기 전에 거부한다.
        if plan is not None:
            parser.error("--nxt-codes 계획과 애프터마켓 전환을 함께 줄 수 없다")
        if args.storage != "raw-v2":
            parser.error("애프터마켓 전환은 독립 raw-v2 실행만 지원한다")
        if args.managed_launch:
            parser.error("애프터마켓 전환은 관리 실행과 함께 쓸 수 없다")
        from collector.kiwoom.subscription_plan import resolve_aftermarket_transition_cli
        try:
            (aftermarket_plan, aftermarket_duration_seconds, aftermarket_transition_at,
             aftermarket_transition_after_seconds) = resolve_aftermarket_transition_cli(
                nxt_codes=args.aftermarket_nxt_codes, list_origin=args.aftermarket_list_origin,
                list_verified_at=args.aftermarket_list_verified_at,
                market_profile=args.aftermarket_market_profile,
                duration_seconds=args.aftermarket_duration_seconds,
                transition_at=args.aftermarket_transition_at,
                transition_after_seconds=args.aftermarket_transition_after_seconds,
                nxt_eligibility_confirmed=args.aftermarket_nxt_eligibility_confirmed,
                note=args.aftermarket_list_note)
        except ValueError as exc:
            parser.error(str(exc))
    from collector.kiwoom.collector_lease import CollectorLease
    from collector.kiwoom.capture_diagnostics import CaptureDiagnostics
    with CollectorLease(PROJECT_ROOT), CaptureDiagnostics(LOG_DIR) as diagnostics:
        managed = None
        if args.managed_launch:
            if codes or args.duration_seconds is not None or args.storage != "raw-v2":
                parser.error("managed launch uses its stored plan only")
            from control_tower.managed_capture import ManagedCapturePeer, TOKEN_ENV
            managed = ManagedCapturePeer(PROJECT_ROOT, args.managed_launch, os.environ.pop(TOKEN_ENV, ""))
            codes, args.duration_seconds = managed.plan["codes"], managed.plan["duration"]
        logger = None
        failure = None
        try:
            if managed is not None and managed.poll():
                managed.finish()
                sys.exit(0)
            logger = KiwoomUniverseLogger(storage=args.storage, codes=codes, plan=plan,
                duration_seconds=args.duration_seconds, managed=managed, diagnostics=diagnostics,
                aftermarket_plan=aftermarket_plan, aftermarket_duration_seconds=aftermarket_duration_seconds,
                aftermarket_transition_at=aftermarket_transition_at,
                aftermarket_transition_after_seconds=aftermarket_transition_after_seconds)
            logger.start()
        except KeyboardInterrupt:
            if logger is not None:
                logger._shutdown("사용자 중단 (Ctrl+C)")
        except BaseException as exc:
            failure = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            # Keep the lease while any accepted data is still being drained.
            if logger is not None and logger.raw_capture is not None:
                if not logger._shutdown_done:
                    logger.raw_capture.queue.abort(failure or "unexpected collector exit")
                while not logger.raw_capture.queue.wait(5):
                    print("raw v2 저장 워커 종료 대기 중", flush=True)
            if managed is not None and not managed.finished:
                capture = logger.raw_capture if logger is not None else None
                report = capture.report if capture is not None and capture.report.state == "closed" else None
                error = failure or (capture.error if capture else None)
                if error is None and logger is not None and logger.exit_code:
                    error = f"collector exited with code {logger.exit_code}; see capture log"
                managed.finish(report, error)
        sys.exit(logger.exit_code)
