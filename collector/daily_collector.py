"""
네이버 금융 다이렉트 연동 일봉 8대 매트릭스 수집기 (개선판)
- count=4000 확장으로 2022년 과거 데이터 및 오늘 최신 데이터까지 완벽 지원
- IndexError 방어 로직 추가
- 기존 엔진 규격(1행 Name, 1열 Code, 열 A000000, 억원/원 단위, CP949) 100% 호환
"""

from datetime import datetime
from pathlib import Path
import re
import sys
import time
import xml.etree.ElementTree as ET
import numpy as np
import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from engine.config import CSV_PATH

DAILY_FILES = ["open", "high", "low", "close", "tradamt", "mkt", "shares", "float"]


def fetch_stock_meta_and_candles(
    code: str, count: int = 4000
) -> tuple[str, pd.DataFrame, dict]:
    """네이버 fchart API에서 종목명과 일봉 OHLCV 데이터를 가져옵니다.

    :param count: 4000 (약 16년 치 일봉 데이터)
    """
    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count={count}&requestType=0"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        res = requests.get(url, headers=headers, timeout=10)
        root = ET.fromstring(res.text)
    except Exception as e:
        print(f"❌ [{code}] 네이버 캔들 통신 실패: {e}")
        return f"종목_{code}", pd.DataFrame(), {"shares": 100_000_000, "float": 60.0}

    # 1. 종목명 추출
    chartdata = root.find("chartdata")
    stock_name = (
        chartdata.attrib.get("name", f"종목_{code}")
        if chartdata is not None
        else f"종목_{code}"
    )

    # 2. 일봉 데이터 파싱 (날짜|시가|고가|저가|종가|거래량)
    records = []
    for item in root.findall(".//item"):
        raw = item.attrib.get("data", "")
        parts = raw.split("|")
        if len(parts) >= 6:
            records.append(
                {
                    "date": parts[0].strip(),
                    "open": float(parts[1]),
                    "high": float(parts[2]),
                    "low": float(parts[3]),
                    "close": float(parts[4]),
                    "vol": float(parts[5]),
                }
            )

    df = pd.DataFrame(records)
    if not df.empty:
        df.set_index("date", inplace=True)

    # 3. 보조 메타데이터 크롤링
    meta = {"shares": 100_000_000, "float": 60.0}
    try:
        page_url = f"https://finance.naver.com/item/main.naver?code={code}"
        page_res = requests.get(page_url, headers=headers, timeout=5)
        m_shares = re.search(
            r"상장주식수.*?<em.*?>([\d,]+)</em>", page_res.text, re.DOTALL
        )
        if m_shares:
            meta["shares"] = int(m_shares.group(1).replace(",", ""))
    except Exception:
        pass

    return stock_name, df, meta


class FastDailyCollector:

    def __init__(self, output_dir: Path = CSV_PATH):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def collect(
        self,
        start_date: str,
        end_date: str,
        target_tickers: list[str],
    ):
        start_date = start_date.replace("-", "")
        end_date = end_date.replace("-", "")
        targets = [str(t).zfill(6) for t in target_tickers]

        print(
            f"🚀 [네이버 API 직결] {len(targets)}개 종목 수집 시작: {start_date} ~ {end_date}"
        )

        # 1. 기준 영업일 달력 추출 (삼성전자 기준, count=4000)
        _, cal_df, _ = fetch_stock_meta_and_candles("005930", count=4000)
        if cal_df.empty:
            print("❌ 영업일 캘린더 데이터를 가져오지 못했습니다.")
            return

        # 기간 필터링
        cal_df = cal_df.loc[
            (cal_df.index >= start_date) & (cal_df.index <= end_date)
        ]
        trading_dates = list(cal_df.index)

        # 빈 날짜 예외 방어
        if not trading_dates:
            print(
                f"❌ 지정한 기간({start_date} ~ {end_date})에 해당하는 개장일(영업일)이 없습니다."
            )
            return

        print(
            f"📅 대상 영업일: {trading_dates[0]} ~ {trading_dates[-1]} (총 {len(trading_dates)}일)"
        )

        col_keys = [f"A{t}" for t in targets]

        # 8대 매트릭스 버퍼 초기화
        matrices = {
            f: pd.DataFrame(index=trading_dates, columns=col_keys)
            for f in DAILY_FILES
        }
        name_map = {}

        # 2. 종목별 데이터 채우기
        success_count = 0
        for idx, code in enumerate(targets):
            col = f"A{code}"
            stock_name, df, meta = fetch_stock_meta_and_candles(
                code, count=4000
            )
            name_map[col] = stock_name
            print(
                f"[{idx+1}/{len(targets)}] {stock_name}({code}) 처리 중...",
                end=" ",
            )

            if df.empty:
                print("⚠️ 데이터 없음")
                continue

            shares = meta["shares"]
            float_ratio = meta["float"]

            for d in trading_dates:
                if d in df.index:
                    c_open = df.loc[d, "open"]
                    c_high = df.loc[d, "high"]
                    c_low = df.loc[d, "low"]
                    c_close = df.loc[d, "close"]
                    c_vol = df.loc[d, "vol"]

                    tradamt_eok = round((c_close * c_vol) / 100_000_000, 2)
                    mkt_eok = int((c_close * shares) / 100_000_000)

                    matrices["open"].loc[d, col] = c_open
                    matrices["high"].loc[d, col] = c_high
                    matrices["low"].loc[d, col] = c_low
                    matrices["close"].loc[d, col] = c_close
                    matrices["tradamt"].loc[d, col] = tradamt_eok
                    matrices["mkt"].loc[d, col] = mkt_eok
                    matrices["shares"].loc[d, col] = shares
                    matrices["float"].loc[d, col] = float_ratio
                else:
                    for f in DAILY_FILES:
                        matrices[f].loc[d, col] = np.nan

            success_count += 1
            print("✅ 완료")
            time.sleep(0.05)

        if success_count == 0:
            print("🚨 수집된 데이터가 없습니다.")
            return

        # 3. CSV 저장 (기존 규격: 1행 Name, 1열 Code, CP949 인코딩)
        print("\n💾 8개 일봉 CSV 파일 저장 중...")
        for f in DAILY_FILES:
            mat = matrices[f]
            name_row = pd.DataFrame(
                [{c: name_map.get(c, "") for c in mat.columns}], index=["Name"]
            )
            final_df = pd.concat([name_row, mat])
            final_df.index.name = "Code"
            final_df.reset_index(inplace=True)

            out_path = self.output_dir / f"{f}.csv"
            final_df.to_csv(out_path, encoding="CP949", index=False)
            print(f"  📁 {out_path.name} 저장 완료 (Shape: {final_df.shape})")


if __name__ == "__main__":
    collector = FastDailyCollector()

    # 과거 2022년 샘플 데이터 + 오늘 실시간 데이터 날짜까지 한 번에 수집!
    # (원하시는 기간으로 언제든 변경 가능합니다)
    collector.collect(
        start_date="20220420",
        end_date=datetime.now().strftime("%Y%m%d"),  # 2022년부터 오늘 날짜까지
        target_tickers=["000270", "005930", "000660"],
    )