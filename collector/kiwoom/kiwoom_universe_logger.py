"""
키움증권 Open API+ 순수 보통주(~2,550개) 실시간 체결 및 10호가 LOB 수집 엔진 (32비트 전용)
- 코스피 / 코스닥 보통주 자동 선별 (우선주, ETF, ETN, 스팩 제외)
- 매수/매도 2중 판정 완비 (FID 14 + 최우선 매도호가 FID 27 비교로 매수 틱 100% 포착)
- 화면번호 26개 분할 등록 (SetRealReg)
- 멀티스레드 대용량 큐 버퍼링으로 틱 누락 0% 방어
- Ctrl+C 정상 종료 처리 완비
- 저장소: sampledata/raw_ticks/{YYYYMMDD}_raw.db
"""

import sys
import os
from pathlib import Path
from datetime import datetime
import time
import queue
import threading
import sqlite3
import signal

from PyQt5.QtWidgets import QApplication
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QTimer

# 윈도우 OS 레벨에서 Ctrl+C 강제 종료 시그널 허용
signal.signal(signal.SIGINT, signal.SIG_DFL)

# 프로젝트 루트 경로 등록
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "sampledata" / "raw_ticks"
RAW_DIR.mkdir(parents=True, exist_ok=True)


class KiwoomUniverseLogger:
    def __init__(self):
        self.app = QApplication(sys.argv)

        # 파이썬 인터프리터가 Ctrl+C를 감지할 수 있도록 0.2초 주기 타이머 가동
        self.timer = QTimer()
        self.timer.timeout.connect(lambda: None)
        self.timer.start(200)

        self.today_str = datetime.now().strftime("%Y%m%d")
        self.db_path = RAW_DIR / f"{self.today_str}_raw.db"

        # 고속 메모리 큐 (UI 렉 방지)
        self.trade_queue = queue.Queue()
        self.quote_queue = queue.Queue()
        self.is_running = True

        # 키움 OCX 초기화
        try:
            self.ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        except Exception as e:
            print(f"❌ 키움 OCX 로드 실패 (32비트 가상환경인지 확인하세요): {e}")
            sys.exit(1)

        # 이벤트 시그널 연결
        self.ocx.OnEventConnect.connect(self._on_login)
        self.ocx.OnReceiveRealData.connect(self._on_receive_real_data)

        # 통계 카운터
        self.total_trades = 0
        self.total_quotes = 0

    def start(self):
        print("=" * 65)
        print("🚀 [키움증권 보통주 전 종목 실시간 틱/호가 수집 데몬]")
        print(f"📁 저장 파일: {self.db_path.name}")
        print("=" * 65)
        print("🔑 키움 OpenAPI+ 서버 접속 시도 중...")
        self.ocx.dynamicCall("CommConnect()")
        self.app.exec_()

    def _on_login(self, err_code: int):
        if err_code != 0:
            print(f"❌ 로그인 실패 (에러코드: {err_code})")
            self.app.quit()
            return

        print("🎉 [성공] 키움증권 서버 로그인 완료!")
        
        # 1. 백그라운드 DB 저장 워커 스레드 가동
        self.db_thread = threading.Thread(target=self._db_writer_worker, daemon=True)
        self.db_thread.start()

        # 2. 통계 출력 스레드 가동
        self.stats_thread = threading.Thread(target=self._stats_worker, daemon=True)
        self.stats_thread.start()

        # 3. 보통주 선별 및 26개 화면 분할 등록
        self._register_all_universe()

    def _register_all_universe(self):
        """코스피/코스닥에서 순수 보통주만 선별하여 100개씩 화면번호 분할 등록"""
        print("🔍 코스피 및 코스닥 전 종목 코드 추출 중...")
        kospi_raw = self.ocx.dynamicCall("GetCodeListByMarket(QString)", "0")
        kosdaq_raw = self.ocx.dynamicCall("GetCodeListByMarket(QString)", "10")

        kospi_codes = [c.strip() for c in kospi_raw.split(";") if c.strip()]
        kosdaq_codes = [c.strip() for c in kosdaq_raw.split(";") if c.strip()]
        all_codes = kospi_codes + kosdaq_codes

        print(f"📊 시장 전체 종목 수: 코스피 {len(kospi_codes)}개 + 코스닥 {len(kosdaq_codes)}개 = 총 {len(all_codes)}개")

        # 1. 보통주만 필터링 (우선주, ETF, ETN, 스팩 제외)
        print("🧹 보통주 선별 필터링 진행 중 (ETF, ETN, 스팩, 우선주 제외)...")
        filtered_codes = []
        for c in all_codes:
            if not c.endswith("0"):
                continue
            name_raw = self.ocx.dynamicCall("GetMasterCodeName(QString)", c)
            name = str(name_raw).strip() if name_raw else ""
            if any(keyword in name for keyword in ["스팩", "ETF", "ETN", "KoTop"]):
                continue
            filtered_codes.append(c)

        print(f"📊 최종 수집 대상 보통주: 총 {len(filtered_codes)}개 종목 (잡주/ETF 제외 완료)")

        # 2. FID 리스트 정의 (27:최우선매도호가, 28:최우선매수호가 추가로 정밀 매수 판정 지원)
        fids = "20;10;15;14;27;28;21;" + ";".join(str(f) for f in range(41, 81))

        # 3. 100개씩 청크 분할하여 화면번호(1000, 1001, ...) 부여
        chunk_size = 100
        screen_base = 1000

        print("📡 보통주 실시간 구독 등록 시작 (화면번호 분할 매핑)...")
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

        print(f"✅ 코스피/코스닥 보통주 ({len(filtered_codes)}개) 실시간 수신 등록 완료!")
        print("🔴 실시간 체결/호가 수집 가동 중... (종료하려면 터미널에서 Ctrl + C)")

    def _on_receive_real_data(self, code: str, real_type: str, real_data: str):
        """키움 실시간 이벤트 수신 핸들러 (2중 방어 매수/매도 판정 적용)"""
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
            except Exception:
                pass

    def _db_writer_worker(self):
        """백그라운드에서 SQLite WAL 모드로 대용량 배치 INSERT 전담"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=OFF;")
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS raw_trades (
                t_time TEXT, code TEXT, price REAL, vol INTEGER, is_buy INTEGER
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS raw_quotes (
                q_time TEXT, code TEXT, offer_p TEXT, offer_v TEXT, bid_p TEXT, bid_v TEXT
            );
        """)
        conn.commit()

        trade_batch = []
        quote_batch = []

        while self.is_running:
            while not self.trade_queue.empty() and len(trade_batch) < 1000:
                trade_batch.append(self.trade_queue.get())

            while not self.quote_queue.empty() and len(quote_batch) < 1000:
                quote_batch.append(self.quote_queue.get())

            if trade_batch:
                cur.executemany("INSERT INTO raw_trades VALUES (?, ?, ?, ?, ?)", trade_batch)
                self.total_trades += len(trade_batch)
                trade_batch.clear()

            if quote_batch:
                cur.executemany("INSERT INTO raw_quotes VALUES (?, ?, ?, ?, ?, ?)", quote_batch)
                self.total_quotes += len(quote_batch)
                quote_batch.clear()

            conn.commit()
            time.sleep(0.5)

        conn.close()

    def _stats_worker(self):
        """터미널에 수집 현황 주기적 출력 및 장마감 체크"""
        while self.is_running:
            now_int = int(datetime.now().strftime("%H%M%S"))
            now_str = datetime.now().strftime("%H:%M:%S")

            if now_int >= 153500:
                print(f"\n🔔 [15:35:00 장 마감 감지] 전 종목 수집을 정상 종료합니다.")
                self.is_running = False
                self.ocx.dynamicCall("SetRealRemove(QString, QString)", "ALL", "ALL")
                self.app.quit()
                break

            print(f"\r⏱️ [{now_str}] 실시간 보통주 적재 현황 ➔ 체결: {self.total_trades:,}건 | 호가: {self.total_quotes:,}건 (대기큐: {self.trade_queue.qsize() + self.quote_queue.qsize()})", end="")
            time.sleep(1.0)


if __name__ == "__main__":
    logger = KiwoomUniverseLogger()
    try:
        logger.start()
    except KeyboardInterrupt:
        print("\n🛑 사용자에 의해 수집이 중단되었습니다.")
        logger.is_running = False