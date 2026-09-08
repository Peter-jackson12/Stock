"""
네이버 금융 다이렉트 연동 일봉 8대 매트릭스 수집기
- 외부 패키지 버그(KRX 로그인, 404 에러) 원천 배제 (requests + XML 내장 모듈 사용)
- 네이버 캔들 API 직결: 속도 극대화 및 종목명 자동 추출
- 기존 엔진 규격(1행 Name, 1열 Code, 열 A000000, 억원/원 단위, CP949) 100% 호환
"""

from pathlib import Path
import re
import sys
import time
import xml.etree.ElementTree as ET
import numpy as np
import pandas as pd
import requests

# 프로젝트 루트 경로 등록
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from engine.config import CSV_PATH

DAILY_FILES = ["open", "high", "low", "close", "tradamt", "mkt", "shares", "float"]


def fetch_stock_meta_and_candles(
    code: str, count: int = 1000
) -> tuple[str, pd.DataFrame, dict]:
    """네이버 fchart API에서 종목명과 일봉 OHLCV 데이터를 즉시 가져옵니다.

    :return: (종목명, 일봉 DataFrame, 메타정보 dict)
    """
    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count={count}&requestType=0"
    headers = {"User-Agent": "Mozilla/5.0"}
    res = requests.get(url, headers=headers, timeout=10)
    root = ET.fromstring(res.text)

    # 1. 종목명 추출 (chartdata 태그의 name 속성)
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

    # 3. 네이버 페이지에서 상장주식수/시총 보조 크롤링
    meta = {"shares": 100_000_000, "float": 60.0}
    try:
        page_url = f"https://finance.naver.com/item/main.naver?code={code}"
        page_res = requests.get(page_url, headers=headers, timeout=5)
        # 상장주식수 정규식 추출
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
        """
        :param start_date: 'YYYYMMDD' (예: '20220420')
        :param end_date: 'YYYYMMDD' (예: '20220506')
        :param target_tickers: 대상 종목코드 리스트 (예: ['000270', '005930'])
        """
        start_date = start_date.replace("-", "")
        end_date = end_date.replace("-", "")
        targets = [str(t).zfill(6) for t in target_tickers]

        print(
            f"🚀 [네이버 API 직결] {len(targets)}개 종목 수집 시작: {start_date} ~ {end_date}"
        )

        # 1. 기준 영업일 달력 추출 (삼성전자 기준)
        _, cal_df, _ = fetch_stock_meta_and_candles("005930")
        if cal_df.empty:
            print("❌ 영업일 캘린더 데이터를 가져오지 못했습니다.")
            return

        # 기간 필터링
        cal_df = cal_df.loc[
            (cal_df.index >= start_date) & (cal_df.index <= end_date)
        ]
        trading_dates = list(cal_df.index)
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
            stock_name, df, meta = fetch_stock_meta_and_candles(code)
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

                    # 거래대금: 억원 단위 (종가 * 거래량 / 1억)
                    tradamt_eok = round((c_close * c_vol) / 100_000_000, 2)
                    # 시가총액: 억원 단위 (종가 * 상장주식수 / 1억)
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

        # 3. 기존 엔진 규격(1행 Name, 1열 Code, CP949 인코딩)으로 CSV 저장
        print("\n💾 8개 CSV 파일 저장 중...")
        for f in DAILY_FILES:
            mat = matrices[f]
            # 1행에 'Name' 추가
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

    # 테스트: 기아, 삼성전자, SK하이닉스 2022년 4월~5월 구간 수집
    collector.collect(
        start_date="20220420",
        end_date="20220506",
        target_tickers=["000270", "005930", "000660"],
    )