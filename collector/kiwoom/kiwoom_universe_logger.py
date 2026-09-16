"""
키움증권 Open API+ 순수 보통주(~2,550개) 실시간 체결 및 10호가 LOB 수집 엔진 (32비트 전용)
- 코스피 / 코스닥 보통주 자동 선별 (우선주, ETF, ETN, 스팩 제외)
- 매수/매도 2중 판정 완비 (FID 14 + 최우선 매도호가 FID 27 비교로 매수 틱 100% 포착)
- 화면번호 26개 분할 등록 (SetRealReg)
- 멀티스레드 대용량 큐 버퍼링으로 틱 누락 0% 방어
- Ctrl+C 정상 종료 처리 완비
- 수신 침묵 감시: 장중 무이벤트 구간을 주기 루프에서 경고하고, 종료 시
  결손이 남아 있으면 '정상 종료' 라고 보고하지 않는다 (session_monitor).
  정상 종료에도 마지막 수신~종료 갭 수치와 임계값을 함께 남긴다.
- 대기큐 적체 감시: 절대값이 아니라 "바닥 이상을 일정 시간 유지하며
  줄지 않는가"로 판정한다. 장중 최대 적체값과 시각을 세션 요약에 남긴다.
- 회전 파일 로그: logs/kiwoom_universe_{YYYYMMDD}.log (콘솔 출력 동시 기록)
- 저장소: sampledata/raw_ticks/{YYYYMMDD}_raw.db
"""

import sys
import os
from pathlib import Path
from datetime import datetime
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

RAW_DIR = PROJECT_ROOT / "sampledata" / "raw_ticks"
RAW_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = PROJECT_ROOT / "logs"

from collector.kiwoom.session_monitor import (                         # noqa: E402
    DEFAULT_GAP_THRESHOLD_SEC,
    DEFAULT_QUEUE_BACKLOG_FLOOR,
    DEFAULT_QUEUE_STUCK_WINDOW_SEC,
    SessionLog,
    SessionMonitor,
)


from collector.kiwoom.tick_writer import TickWriter  # noqa: E402


class KiwoomUniverseLogger:
    def __init__(
        self,
        *,
        gap_threshold_sec: float = DEFAULT_GAP_THRESHOLD_SEC,
        queue_backlog_floor: int = DEFAULT_QUEUE_BACKLOG_FLOOR,
        queue_stuck_window_sec: float = DEFAULT_QUEUE_STUCK_WINDOW_SEC,
    ):
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

        # 수신 침묵 감시기 + 대기큐 적체 감시기(발견 #13). 판정은 _stats_worker 의
        # 기존 1초 루프에서만 돈다.
        self.monitor = SessionMonitor(
            gap_threshold_sec=gap_threshold_sec,
            queue_backlog_floor=queue_backlog_floor,
            queue_stuck_window_sec=queue_stuck_window_sec,
        )
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
        return self.writer.total_trades

    @property
    def total_quotes(self):
        return self.writer.total_quotes

    def _poll_control(self):
        # OCX calls and shutdown run on the Qt thread, between event callbacks.
        # In particular, SIGINT must not drain while a callback is about to put().
        if self._shutdown_done:
            return
        if self.writer.error:
            self._shutdown_requested = "DB 저장 오류"
        if self._shutdown_requested:
            self._shutdown(self._shutdown_requested)

    def start(self):
        self.monitor.start()
        self.log.emit("=" * 65)
        self.log.emit("🚀 [키움증권 보통주 전 종목 실시간 틱/호가 수집 데몬]")
        self.log.emit(f"📁 저장 파일: {self.db_path.name}")
        self.log.emit(f"📝 로그 파일: {self.log_path}")
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
        if err_code != 0:
            self.exit_code = 2
            self.log.emit(f"❌ 로그인 실패 (에러코드: {err_code})")
            self._shutdown(f"로그인 실패 (에러코드 {err_code})")
            return

        self.log.emit("🎉 [성공] 키움증권 서버 로그인 완료!")

        # 1. 백그라운드 DB 저장 워커 스레드 가동
        self.db_thread = threading.Thread(target=self.writer.run, daemon=False)
        self.db_thread.start()

        # 2. 통계 출력 스레드 가동
        self.stats_thread = threading.Thread(target=self._stats_worker, daemon=True)
        self.stats_thread.start()

        # 3. 보통주 선별 및 26개 화면 분할 등록
        self._register_all_universe()

    def _register_all_universe(self):
        """코스피/코스닥에서 순수 보통주만 선별하여 100개씩 화면번호 분할 등록"""
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

        self.log.emit(f"📊 최종 수집 대상 보통주: 총 {len(filtered_codes)}개 종목 (잡주/ETF 제외 완료)")

        # 2. FID 리스트 정의 (27:최우선매도호가, 28:최우선매수호가 추가로 정밀 매수 판정 지원)
        fids = "20;10;15;14;27;28;21;" + ";".join(str(f) for f in range(41, 81))

        # 3. 100개씩 청크 분할하여 화면번호(1000, 1001, ...) 부여
        chunk_size = 100
        screen_base = 1000

        self.log.emit("📡 보통주 실시간 구독 등록 시작 (화면번호 분할 매핑)...")
        for idx in range(0, len(filtered_codes), chunk_size):
            chunk = filtered_codes[idx : idx + chunk_size]
            screen_no = str(screen_base + (idx // chunk_size))
            code_str = ";".join(chunk)

            self.ocx.dynamicCall(
                "SetRealReg(QString, QString, QString, QString)",
                screen_no,
                code_str,
                fids,
                "0"
            )
            time.sleep(0.05)

        self.log.emit(f"✅ 코스피/코스닥 보통주 ({len(filtered_codes)}개) 실시간 수신 등록 완료!")
        self.log.emit("🔴 실시간 체결/호가 수집 가동 중... (종료하려면 터미널에서 Ctrl + C)")

    def _on_receive_real_data(self, code: str, real_type: str, real_data: str):
        """키움 실시간 이벤트 수신 핸들러 (2중 방어 매수/매도 판정 적용)"""
        if not self.accepting_events:
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
            now = datetime.now()
            now_int = int(now.strftime("%H%M%S"))
            now_str = now.strftime("%H:%M:%S")
            queue_depth = self.trade_queue.qsize() + self.quote_queue.qsize()

            # 침묵/적체 판정은 이 주기 루프 안에서만 한다. 이벤트 수신 경로에는
            # 판정도 I/O 도 붙이지 않는다.
            for notice in (self.monitor.tick(), self.monitor.sample_queue_depth(queue_depth)):
                if notice:
                    self.log.end_status_line()   # 상태줄(\r) 위에 겹쳐 찍히지 않도록
                    self.log.emit(notice)

            if now_int >= 153500:
                self.log.end_status_line()
                self.log.emit("🔔 [15:35 장 마감 감지] 전 종목 수집을 종료합니다.")
                self._shutdown_requested = "장 마감 (15:35)"
                break

            self.log.status(
                f"⏱️ [{now_str}] 실시간 보통주 적재 현황 ➔ "
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
        self.log.emit("💾 수신을 멈추고 남은 체결/호가의 DB 커밋을 기다립니다.")
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
    logger = KiwoomUniverseLogger()
    try:
        sys.exit(logger.start())
    except KeyboardInterrupt:
        # 시그널 핸들러가 먼저 처리했다면 _shutdown 은 한 번만 실행된다.
        logger._shutdown("사용자 중단 (Ctrl+C)")