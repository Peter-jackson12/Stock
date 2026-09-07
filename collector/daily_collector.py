"""
일봉 8대 매트릭스 CSV 생성 및 증분 업데이트 수집기
- open, high, low, close, tradamt(억원), mkt(억원), shares, float(%)
- 기존 백테스트 엔진과 100% 호환되는 형식(1열 Code/날짜, 1행 종목명, 열이름 A000000)으로 저장
"""

from datetime import datetime
from pathlib import Path
import re
import sys
import time
import bs4
import numpy as np
import pandas as pd
from pykrx import stock
import requests

# 프로젝트 루트 경로 등록
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from engine.config import CSV_PATH

# 8대 파일명 목록
DAILY_FILES = ["open", "high", "low", "close", "tradamt", "mkt", "shares", "float"]


def get_krx_market_dates(start_date: str, end_date: str) -> list[str]:
    """해당 기간 중 실제 주식시장 개장일 목록을 YYYYMMDD 형태로 가져옵니다."""
    df = stock.get_market_ohlcv_by_date(start_date, end_date, "005930")
    return [d.strftime("%Y%m%d") for d in df.index]


def fetch_float_ratio_fnguide(ticker: str) -> float:
    """네이버 금융 / FnGuide에서 최신 유통비율(%)을 크롤링합니다."""
    url = f"https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={ticker}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        soup = bs4.BeautifulSoup(res.text, "html.parser")
        # '유통주식수/유통비율' 텍스트가 있는 셀 탐색
        target_td = soup.find(lambda tag: tag.name == "td" and "유통주식수" in tag.text)
        if target_td:
            next_td = target_td.find_next_sibling("td")
            if next_td:
                # '12,345,678주 / 65.43%' 형태에서 뒤의 % 추출
                match = re.search(r"/\s*([\d\.]+)%", next_td.text)
                if match:
                    return float(match.group(1))
    except Exception:
        pass
    return 60.0  # 파싱 실패 시 기본값


class DailyCollector:

    def __init__(self, output_dir: Path = CSV_PATH):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def build_history(
        self,
        start_date: str,
        end_date: str,
        target_tickers: list[str] | None = None,
    ):
        """과거 기간 데이터를 수집하여 8개 CSV 파일을 최초 생성 또는 갱신합니다.

        :param start_date: 'YYYYMMDD'
        :param end_date: 'YYYYMMDD'
        :param target_tickers: 특정 종목 리스트만 뽑을 경우 (None이면 코스피/코스닥
        전체)
        """
        trading_dates = get_krx_market_dates(start_date, end_date)
        if not trading_dates:
            print("⚠️ 수집할 영업일이 없습니다.")
            return

        print(f"📅 총 {len(trading_dates)}영업일 데이터 수집 시작: {trading_dates[0]} ~ {trading_dates[-1]}")

        # 종목 마스터 가져오기 (마지막 영업일 기준)
        last_date = trading_dates[-1]
        tickers_kospi = stock.get_market_ticker_list(last_date, market="KOSPI")
        tickers_kosdaq = stock.get_market_ticker_list(
            last_date, market="KOSDAQ"
        )
        all_tickers = sorted(list(set(tickers_kospi + tickers_kosdaq)))

        if target_tickers:
            all_tickers = [t for t in all_tickers if t in target_tickers]

        # 종목코드에 'A' 접두사 부여
        col_names = [f"A{t}" for t in all_tickers]
        name_dict = {f"A{t}": stock.get_market_ticker_name(t) for t in all_tickers}

        # 8개 메트릭 매트릭스 초기화 (행: 날짜들, 열: A종목코드들)
        matrices = {
            f: pd.DataFrame(index=trading_dates, columns=col_names)
            for f in DAILY_FILES
        }

        # 유통비율 크롤링 (종목별 1회 조회)
        print("🔍 종목별 유통비율 수집 중...")
        float_map = {}
        for idx, t in enumerate(all_tickers):
            float_map[f"A{t}"] = fetch_float_ratio_fnguide(t)
            if (idx + 1) % 100 == 0:
                print(f"  - 유통비율 수집 진행률: {idx+1}/{len(all_tickers)}")
                time.sleep(0.5)

        # 날짜별 루프 (PyKRX로 시장 전체 일괄 조회하여 속도 극대화)
        for d_idx, cur_date in enumerate(trading_dates):
            print(f"[{d_idx+1}/{len(trading_dates)}] {cur_date} 데이터 처리 중...")
            try:
                # 1. OHLCV 및 거래대금 (원 -> 억원 변환)
                ohlcv = stock.get_market_ohlcv_by_ticker(cur_date, market="ALL")
                # 2. 시가총액 및 상장주식수 (시총: 원 -> 억원 변환)
                cap = stock.get_market_cap_by_ticker(cur_date, market="ALL")

                for t in all_tickers:
                    col = f"A{t}"
                    if t in ohlcv.index:
                        matrices["open"].loc[cur_date, col] = ohlcv.loc[
                            t, "시가"
                        ]
                        matrices["high"].loc[cur_date, col] = ohlcv.loc[
                            t, "고가"
                        ]
                        matrices["low"].loc[cur_date, col] = ohlcv.loc[
                            t, "저가"
                        ]
                        matrices["close"].loc[cur_date, col] = ohlcv.loc[
                            t, "종가"
                        ]
                        # 거래대금: 억원 단위 (소수점 2자리)
                        matrices["tradamt"].loc[cur_date, col] = round(
                            ohlcv.loc[t, "거래대금"] / 100_000_000, 2
                        )
                    else:
                        for f in ["open", "high", "low", "close", "tradamt"]:
                            matrices[f].loc[cur_date, col] = np.nan

                    if t in cap.index:
                        # 시가총액: 억원 단위
                        matrices["mkt"].loc[cur_date, col] = int(
                            cap.loc[t, "시가총액"] / 100_000_000
                        )
                        matrices["shares"].loc[cur_date, col] = int(
                            cap.loc[t, "상장주식수"]
                        )
                    else:
                        matrices["mkt"].loc[cur_date, col] = np.nan
                        matrices["shares"].loc[cur_date, col] = np.nan

                    # 유통비율 (%)
                    matrices["float"].loc[cur_date, col] = float_map.get(
                        col, 60.0
                    )

            except Exception as e:
                print(f"⚠️ {cur_date} 수집 중 오류: {e}")

        # 기존 레거시 포맷으로 변환 후 CSV 저장
        print("💾 원본 포맷(CP949, 1행 종목명, 1열 Code)으로 저장 중...")
        for f in DAILY_FILES:
            df = matrices[f]
            # 1행에 종목명 Row 삽입
            name_row = pd.DataFrame([name_dict], index=["Name"])
            final_df = pd.concat([name_row, df])
            final_df.index.name = "Code"
            final_df.reset_index(inplace=True)

            out_file = self.output_dir / f"{f}.csv"
            final_df.to_csv(out_file, encoding="CP949", index=False)
            print(f"  ✅ 저장 완료: {out_file.name}")


if __name__ == "__main__":
    collector = DailyCollector()

    # 테스트 실행 예시: 2022년 4월~5월 기아(000270), 삼성전자(005930) 등 주요 종목 테스트
    # (전체 종목을 하시려면 target_tickers=None 으로 주시면 됩니다)
    collector.build_history(
        start_date="20220420",
        end_date="20220506",
        target_tickers=["000270", "005930", "000660", "370090"],
    )