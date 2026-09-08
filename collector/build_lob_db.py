"""
2단계: Raw 틱데이터를 백테스트용 1초봉 LOB DB로 변환(Resampling)하는 배치 엔진
- 입력: sampledata/raw_ticks/{YYYYMMDD}_raw.db
- 출력: sampledata/temp/{YYYYMMDD}_LOB.db (테이블별 51개 컬럼)
"""

from datetime import datetime
from pathlib import Path
import sqlite3
import sys
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from engine.config import DATA_DIR, SEC_PATH

RAW_DIR = DATA_DIR / "raw_ticks"
LOB_COLUMNS = [
    "time",
    "open",
    "high",
    "low",
    "close",
    "vol",
    "buy_vol",
    "sell_vol",
    "tick",
    "buy_tick",
    "sell_tick",
    "offer_p1",
    "offer_p2",
    "offer_p3",
    "offer_p4",
    "offer_p5",
    "offer_p6",
    "offer_p7",
    "offer_p8",
    "offer_p9",
    "offer_p10",
    "offer_v1",
    "offer_v2",
    "offer_v3",
    "offer_v4",
    "offer_v5",
    "offer_v6",
    "offer_v7",
    "offer_v8",
    "offer_v9",
    "offer_v10",
    "bid_p1",
    "bid_p2",
    "bid_p3",
    "bid_p4",
    "bid_p5",
    "bid_p6",
    "bid_p7",
    "bid_p8",
    "bid_p9",
    "bid_p10",
    "bid_v1",
    "bid_v2",
    "bid_v3",
    "bid_v4",
    "bid_v5",
    "bid_v6",
    "bid_v7",
    "bid_v8",
    "bid_v9",
    "bid_v10",
]


def resample_raw_to_lob(date_str: str):
    raw_db_path = RAW_DIR / f"{date_str}_raw.db"
    if not raw_db_path.exists():
        print(f"❌ 원본 틱 파일이 없습니다: {raw_db_path}")
        return

    SEC_PATH.mkdir(parents=True, exist_ok=True)
    lob_db_path = SEC_PATH / f"{date_str}_LOB.db"

    print(
        f"⚙️ [{date_str}] 틱 데이터 ➔ 1초봉 LOB 변환 시작: {raw_db_path.name} -> {lob_db_path.name}"
    )

    raw_conn = sqlite3.connect(raw_db_path)
    lob_conn = sqlite3.connect(lob_db_path)
    lob_conn.execute("PRAGMA journal_mode=WAL;")

    # 수집된 종목 리스트 확인
    codes = [
        r[0]
        for r in raw_conn.execute(
            "SELECT DISTINCT code FROM raw_trades"
        ).fetchall()
    ]

    col_defs = ", ".join([f"{col} REAL" for col in LOB_COLUMNS[1:]])

    for code in codes:
        print(f"  🔨 종목 [{code}] 1초봉 및 호가 매핑 중...", end=" ")

        # 1. 체결 데이터 로드
        trades_df = pd.read_sql_query(
            f"SELECT * FROM raw_trades WHERE code='{code}' ORDER BY t_time ASC",
            raw_conn,
        )
        if trades_df.empty:
            print("체결 없음")
            continue

        # 2. 호가 데이터 로드
        quotes_df = pd.read_sql_query(
            f"SELECT * FROM raw_quotes WHERE code='{code}' ORDER BY q_time ASC",
            raw_conn,
        )

        # 3. 1초 단위 그룹화 (OHLCV + 틱)
        lob_rows = []
        grouped = trades_df.groupby("t_time")

        for sec, g in grouped:
            c_open = g["price"].iloc[0]
            c_high = g["price"].max()
            c_low = g["price"].min()
            c_close = g["price"].iloc[-1]
            tot_vol = g["vol"].sum()
            buy_vol = g[g["is_buy"] == 1]["vol"].sum()
            sell_vol = g[g["is_buy"] == 0]["vol"].sum()
            tick_cnt = len(g)
            buy_tick = (g["is_buy"] == 1).sum()
            sell_tick = (g["is_buy"] == 0).sum()

            # 해당 초(sec) 이전 가장 최신의 10호가 매핑
            recent_q = quotes_df[quotes_df["q_time"] <= sec]
            if not recent_q.empty:
                last_q = recent_q.iloc[-1]
                op = [float(x) for x in last_q["offer_p"].split(",")]
                ov = [int(x) for x in last_q["offer_v"].split(",")]
                bp = [float(x) for x in last_q["bid_p"].split(",")]
                bv = [int(x) for x in last_q["bid_v"].split(",")]
            else:
                op = [0.0] * 10
                ov = [0] * 10
                bp = [0.0] * 10
                bv = [0] * 10

            row = (
                sec,
                c_open,
                c_high,
                c_low,
                c_close,
                tot_vol,
                buy_vol,
                sell_vol,
                tick_cnt,
                buy_tick,
                sell_tick,
                *op,
                *ov,
                *bp,
                *bv,
            )
            lob_rows.append(row)

        # 4. 대상 테이블 생성 및 INSERT
        lob_conn.execute(f"DROP TABLE IF EXISTS '{code}';")
        lob_conn.execute(
            f"CREATE TABLE IF NOT EXISTS '{code}' (time TEXT, {col_defs});"
        )
        placeholders = ", ".join(["?"] * 51)
        lob_conn.executemany(
            f"INSERT INTO '{code}' VALUES ({placeholders})", lob_rows
        )
        lob_conn.commit()
        print(f"✅ 완료 ({len(lob_rows)}초봉 생성)")

    raw_conn.close()
    lob_conn.close()
    print(f"🎉 [{date_str}_LOB.db] 변환 완료! 이제 백테스트 엔진을 돌릴 수 있습니다.\n")


if __name__ == "__main__":
    today = datetime.now().strftime("%Y%m%d")
    target_date = sys.argv[1] if len(sys.argv) > 1 else today
    resample_raw_to_lob(target_date)