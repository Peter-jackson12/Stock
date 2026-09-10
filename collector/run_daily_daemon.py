"""
데이터 수집 완전 자동화 데몬 (Full-Auto Daily Daemon)
- 09:00 ~ 15:35: KIS WebSocket 실시간 틱/호가 수집 (네트워크 끊김 자동 재접속 완비)
- 15:35 장 마감: 수집 자동 종료
- 15:36: 당일 1초봉 LOB.db 자동 빌드 (build_lob_db.py 연동)
- 15:37: 당일 8대 일봉 CSV 자동 업데이트 (daily_collector.py 연동)
"""

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from dotenv import load_dotenv
import requests
import websockets

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine.config import DATA_DIR
from collector.universe import TARGET_CODES
from collector.build_lob_db import resample_raw_to_lob
from collector.daily_collector import FastDailyCollector

# 환경변수 로드
ENV_CANDIDATES = [
    PROJECT_ROOT / "KIS_APP.env",
    PROJECT_ROOT / ".env",
    PROJECT_ROOT / "collector" / "KIS_APP.env",
]
for p in ENV_CANDIDATES:
    if p.exists():
        load_dotenv(p)
        break

KIS_APP_KEY = os.getenv("KIS_APP_KEY", "").strip().strip('"').strip("'")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "").strip().strip('"').strip("'")
IS_MOCK = os.getenv("IS_MOCK", "True").lower() in ("true", "1", "yes")

RAW_DIR = DATA_DIR / "raw_ticks"
RAW_DIR.mkdir(parents=True, exist_ok=True)


class FullAutoCollector:

    def __init__(self, target_codes: list[str]):
        self.target_codes = target_codes
        self.today_str = datetime.now().strftime("%Y%m%d")
        self.db_path = RAW_DIR / f"{self.today_str}_raw.db"

        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=OFF;")
        self._init_db()

    def _init_db(self):
        cur = self.conn.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS raw_trades (t_time TEXT, code TEXT, price REAL, vol INTEGER, is_buy INTEGER);"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS raw_quotes (q_time TEXT, code TEXT, offer_p TEXT, offer_v TEXT, bid_p TEXT, bid_v TEXT);"""
        )
        self.conn.commit()

    def get_approval_key(self) -> str:
        base_url = (
            "https://openapivts.koreainvestment.com:29443"
            if IS_MOCK
            else "https://openapi.koreainvestment.com:9443"
        )
        url = f"{base_url}/oauth2/Approval"
        body = {
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "secretkey": KIS_APP_SECRET,
        }
        try:
            res = requests.post(
                url,
                json=body,
                headers={"content-type": "application/json; utf-8"},
                timeout=10,
            )
            return res.json().get("approval_key", "")
        except Exception as e:
            print(f"❌ Approval 통신 에러: {e}")
            return ""

    async def run(self):
        print("=" * 60)
        print(f"🚀 [올인원 데이터 수집 데몬 가동 시작]")
        print(
            f"📅 오늘 날짜: {self.today_str} | 수집 대상: 총 {len(self.target_codes)}개 종목"
        )
        print(f"📁 저장 파일: {self.db_path.name}")
        print("=" * 60)

        approval_key = self.get_approval_key()
        if not approval_key:
            print("❌ Approval Key 발급 실패! KIS_APP.env를 확인하세요.")
            return

        ws_url = (
            "ws://ops.koreainvestment.com:31000"
            if IS_MOCK
            else "ws://ops.koreainvestment.com:21000"
        )

        cur = self.conn.cursor()
        tick_count = 0

        # 장마감 시간 (15:35:00)
        end_time_int = 153500

        # 네트워크 끊김 시 자동 재접속 무한 루프
        while True:
            now_int = int(datetime.now().strftime("%H%M%S"))
            if now_int >= end_time_int:
                print(f"\n🔔 [15:35 장 마감 도달] 실시간 수집을 안전하게 마감합니다.")
                break

            try:
                print(f"🔌 WebSocket 접속 연결 중: {ws_url}")
                async with websockets.connect(ws_url, ping_interval=30) as ws:
                    # 종목 구독 신청
                    for code in self.target_codes:
                        for tr_id in ["H0STCNT0", "H0STASP0"]:
                            msg = {
                                "header": {
                                    "approval_key": approval_key,
                                    "custtype": "P",
                                    "tr_type": "1",
                                    "content-type": "utf-8",
                                },
                                "body": {
                                    "input": {"tr_id": tr_id, "tr_key": code}
                                },
                            }
                            await ws.send(json.dumps(msg))
                            await asyncio.sleep(0.02)

                    print(f"✅ {len(self.target_codes)}개 종목 실시간 수신 등록 완료!")

                    # 실시간 메시지 수신 루프
                    while True:
                        now_int = int(datetime.now().strftime("%H%M%S"))
                        if now_int >= end_time_int:
                            break

                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)

                        if raw.startswith("0") or raw.startswith("1"):
                            parts = raw.split("|")
                            if len(parts) >= 4:
                                tr_id = parts[1]
                                d = parts[3].split("^")
                                code = d[0]
                                now_str = datetime.now().strftime("%H%M%S")

                                if tr_id == "H0STCNT0":
                                    is_buy = 1 if d[21] == "1" else 0
                                    cur.execute(
                                        "INSERT INTO raw_trades VALUES (?, ?, ?, ?, ?)",
                                        (
                                            now_str,
                                            code,
                                            float(d[2]),
                                            int(d[12]),
                                            is_buy,
                                        ),
                                    )
                                elif tr_id == "H0STASP0":
                                    offer_p = ",".join(d[3:13])
                                    bid_p = ",".join(d[13:23])
                                    offer_v = ",".join(d[23:33])
                                    bid_v = ",".join(d[33:43])
                                    cur.execute(
                                        "INSERT INTO raw_quotes VALUES (?, ?, ?, ?, ?, ?)",
                                        (
                                            now_str,
                                            code,
                                            offer_p,
                                            offer_v,
                                            bid_p,
                                            bid_v,
                                        ),
                                    )

                                tick_count += 1
                                if tick_count % 100 == 0:
                                    self.conn.commit()
                                    print(
                                        f"\r📊 [실시간 수집 중] 누적 수신 틱: {tick_count:,}건 ({now_str})",
                                        end="",
                                    )

            except asyncio.TimeoutError:
                # 틱이 잠시 안 들어와도 장마감 시간 체크하며 유지
                continue
            except Exception as e:
                print(f"\n⚠️ 일시적 연결 끊김 발생 ({e}). 5초 후 자동 재접속합니다...")
                await asyncio.sleep(5)

        self.conn.commit()
        self.conn.close()
        print(f"\n💾 원본 틱 저장 완료 (총 {tick_count:,}건 적재)")

        # =======================================================
        # 2단계: 장 마감 후 자동 후처리 (LOB 빌드 + 일봉 업데이트)
        # =======================================================
        print("\n" + "=" * 60)
        print(f"⚙️ [후처리 파이프라인 자동 가동]")
        print("=" * 60)

        # 1) 1초봉 51개 컬럼 LOB.db 자동 빌드
        print("1️⃣ [LOB 생성] 원본 틱 ➔ 1초봉 LOB 변환 시작...")
        resample_raw_to_lob(self.today_str)

        # 2) 오늘 일봉 8개 CSV 자동 업데이트
        print("2️⃣ [일봉 갱신] 오늘 날짜 일봉 8대 매트릭스 CSV 갱신 시작...")
        collector = FastDailyCollector()
        collector.collect(
            start_date="20220420",
            end_date=self.today_str,
            target_tickers=self.target_codes,
        )

        print("\n🎉🎉 [수집 완료] 오늘의 모든 데이터 수집 및 전처리가 완벽히 끝났습니다!")


if __name__ == "__main__":
    daemon = FullAutoCollector(target_codes=TARGET_CODES)
    asyncio.run(daemon.run())