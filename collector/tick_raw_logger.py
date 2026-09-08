"""
1단계: 장중 실시간 틱/호가 Raw 데이터 수집기
- 파라미터 버그 수정: appsecret / secretkey 동시 지원으로 EGW00104 해결
- .env 파일 다중 위치(루트 .env, collector/KIS_APP.env 등) 자동 탐색
"""

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
from dotenv import load_dotenv
import requests
import websockets

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from engine.config import DATA_DIR

# 1. .env 다중 경로 자동 탐색 (어디에 저장하셨든 다 읽어옵니다)
ENV_CANDIDATES = [
    PROJECT_ROOT / ".env",
    PROJECT_ROOT / ".env.txt",
    PROJECT_ROOT / "KIS_APP.env",
    PROJECT_ROOT / "collector" / "KIS_APP.env",
    PROJECT_ROOT / "collector" / ".env",
]
loaded = False
for env_path in ENV_CANDIDATES:
    if env_path.exists():
        load_dotenv(env_path)
        print(f"📄 환경변수 파일 로드 성공: {env_path}")
        loaded = True
        break

if not loaded:
    print("⚠️ .env 파일을 찾지 못했습니다. 환경변수 기본값을 사용합니다.")

KIS_APP_KEY = os.getenv("KIS_APP_KEY", "").strip().strip('"').strip("'")
KIS_APP_SECRET = os.getenv("KIS_APP_SECRET", "").strip().strip('"').strip("'")
IS_MOCK = os.getenv("IS_MOCK", "True").lower() in ("true", "1", "yes")

RAW_DIR = DATA_DIR / "raw_ticks"
RAW_DIR.mkdir(parents=True, exist_ok=True)


class RawTickLogger:

    def __init__(self, target_codes: list[str]):
        self.target_codes = [str(c).zfill(6) for c in target_codes]
        self.today_str = datetime.now().strftime("%Y%m%d")
        self.db_path = RAW_DIR / f"{self.today_str}_raw.db"

        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=OFF;")
        self._init_db()

    def _init_db(self):
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_trades (
                t_time TEXT,
                code TEXT,
                price REAL,
                vol INTEGER,
                is_buy INTEGER
            );
        """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_quotes (
                q_time TEXT,
                code TEXT,
                offer_p TEXT,
                offer_v TEXT,
                bid_p TEXT,
                bid_v TEXT
            );
        """
        )
        self.conn.commit()

    def get_approval_key(self) -> str:
        base_url = (
            "https://openapivts.koreainvestment.com:29443"
            if IS_MOCK
            else "https://openapi.koreainvestment.com:9443"
        )
        url = f"{base_url}/oauth2/Approval"

        # ⭐️ 핵심 수정: appsecret과 secretkey를 둘 다 넣어 호환성 완벽 보장!
        body = {
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
            "secretkey": KIS_APP_SECRET,
        }

        print(
            f"🔍 [점검] 키 길이: AppKey({len(KIS_APP_KEY)}자), Secret({len(KIS_APP_SECRET)}자), 모의투자({IS_MOCK})"
        )

        try:
            res = requests.post(
                url,
                json=body,
                headers={"content-type": "application/json; utf-8"},
                timeout=10,
            )
            data = res.json()
            if "approval_key" in data:
                print(f"🔑 Approval Key 발급 성공!")
                return data["approval_key"]
            else:
                print(f"❌ KIS 거절 응답 ({res.status_code}): {data}")
        except Exception as e:
            print(f"❌ 통신 에러: {e}")
        return ""

    async def run(self):
        approval_key = self.get_approval_key()
        if not approval_key:
            print("❌ Approval Key 발급 실패! 설정을 다시 확인하세요.")
            return

        ws_url = (
            "ws://ops.koreainvestment.com:31000"
            if IS_MOCK
            else "ws://ops.koreainvestment.com:21000"
        )
        print(
            f"🚀 [실시간 웹소켓 접속] {ws_url} (수집 대상: {self.target_codes})"
        )

        async with websockets.connect(ws_url, ping_interval=30) as ws:
            for code in self.target_codes:
                for tr_id in ["H0STCNT0", "H0STASP0"]:
                    msg = {
                        "header": {
                            "approval_key": approval_key,
                            "custtype": "P",
                            "tr_type": "1",
                            "content-type": "utf-8",
                        },
                        "body": {"input": {"tr_id": tr_id, "tr_key": code}},
                    }
                    await ws.send(json.dumps(msg))
                    await asyncio.sleep(0.05)

            print("🔴 실시간 시세/호가 수집 시작! (종료하려면 Ctrl + C)")
            cur = self.conn.cursor()
            count = 0

            while True:
                raw = await ws.recv()
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
                            print(
                                f"  ⚡ [체결] {code} | {d[2]}원 | {d[12]}주 | {'매수' if is_buy else '매도'}"
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

                        count += 1
                        if count % 10 == 0:
                            self.conn.commit()


if __name__ == "__main__":
    # 삼성전자(005930), 기아(000270) 실시간 테스트
    logger = RawTickLogger(target_codes=["005930", "000270"])
    asyncio.run(logger.run())