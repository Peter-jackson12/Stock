"""
네이버 금융 다이렉트 연동 일봉 8대 매트릭스 수집기 (개선판)
- count=4000 확장으로 2022년 과거 데이터 및 오늘 최신 데이터까지 완벽 지원
- IndexError 방어 로직 추가
- 기존 엔진 규격(1행 Name, 1열 Code, 열 A000000, 억원/원 단위, CP949) 100% 호환

[2026-09-15 rev.2 §2' 스텁 제거]
과거에는 shares/float 를 못 구하면 각각 1억주/60.0% 상수로 채워 넣었다.
이 상수는 룩어헤드 이전에 **거짓 데이터**다 — 결측이면 하류(D-3 보수적 null
정책)가 안전하게 걸러내지만, 그럴듯한 상수는 아무도 못 잡는다.

또한 shares 는 매일 finance.naver.com 에서 스크랩한 "지금 이 순간의" 상장주식수
하나를 **전 거래일(2022년~오늘)에 그대로 broadcast** 하고 있었다(발견 #0-3).
액면분할 등으로 과거와 현재 상장주식수가 다른 종목에서는 이게 곧 시점 위반
(D-4 point-in-time)이자 룩어헤드다. 지금은 날짜별 상장주식수 소스가 없으므로
(§3-a/§3-b 대기 중), 이 스크랩값은 **수집 시점(가장 최근 거래일)에만** 쓰고
과거 날짜는 전부 NaN 으로 둔다. mkt(시가총액)도 shares 가 없으면 NaN — 있는
값을 억지로 계산하지 않는다. float_ratio 크롤러는 아직 없으므로 항상 NaN.

[2026-09-15 rev.2 §5 tidy 스냅샷]
mkt/shares/float 는 이제 wide CSV 에 직접 쓰지 않는다. 수집 시점(last_date)의
관측값을 collector/daily_snapshot.py 로 하루치 tidy 스냅샷(sampledata/Daily/
snapshots/YYYYMMDD.csv)에 저장하고, 그 스냅샷 전체를 피벗해 mkt.csv/
shares.csv/float.csv 를 **파생**시킨다. 스냅샷이 없는 날짜는 구조적으로
NaN 이 된다 — broadcast 버그가 다시 생길 경로 자체가 없다.
open/high/low/close/tradamt 는 그대로 이 파일이 직접 wide CSV 에 쓴다(매번
다시 받아도 그날그날의 실제 시세라 시점 문제가 없다).
"""

from datetime import datetime
from pathlib import Path
import json
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
from collector.daily_snapshot import write_snapshot, rebuild_wide_csvs

#: 이 파일이 직접 wide CSV 로 쓰는 매트릭스. mkt/shares/float 는 daily_snapshot
#: 이 tidy 스냅샷에서 파생시키므로 여기 없다.
DAILY_FILES = ["open", "high", "low", "close", "tradamt"]


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
        return (
            f"종목_{code}",
            pd.DataFrame(),
            {
                "shares": None,
                "shares_reason": f"candle_fetch_failed: {e}",
                "float": None,
                "float_reason": "no_crawler_implemented",
            },
        )

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

    # 3. 보조 메타데이터 크롤링 — 성공해도 "지금 이 순간" 값 하나뿐이다.
    # 과거 날짜에 broadcast 하지 않는다(호출자가 최신 거래일에만 반영).
    meta = {"shares": None, "shares_reason": "no_source"}
    try:
        page_url = f"https://finance.naver.com/item/main.naver?code={code}"
        page_res = requests.get(page_url, headers=headers, timeout=5)
        m_shares = re.search(
            r"상장주식수.*?<em.*?>([\d,]+)</em>", page_res.text, re.DOTALL
        )
        if m_shares:
            meta["shares"] = int(m_shares.group(1).replace(",", ""))
            meta["shares_reason"] = None
        else:
            meta["shares_reason"] = "page_pattern_not_found"
    except Exception as e:
        meta["shares_reason"] = f"page_fetch_failed: {e}"

    # float_ratio 크롤러 부재 확정(발견 #0-3) — 항상 NaN, 이유를 남긴다.
    meta["float"] = None
    meta["float_reason"] = "no_crawler_implemented"

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

        # OHLCV 매트릭스 버퍼 초기화 (mkt/shares/float 는 tidy 스냅샷에서 파생 — §5)
        matrices = {
            f: pd.DataFrame(index=trading_dates, columns=col_keys)
            for f in DAILY_FILES
        }
        name_map = {}
        meta_reasons: dict[str, dict] = {}
        snapshot_rows: list[dict] = []     # last_date 시점 관측값만 (§5 tidy)
        last_date = trading_dates[-1]

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

            # 스크랩값은 "수집 시점(가장 최근 거래일)"에만 유효한 단일 스냅샷이다.
            # 과거 거래일에 그대로 broadcast 하면 시점 위반(D-4)이므로 쓰지 않는다.
            shares = meta["shares"]
            float_ratio = meta["float"]
            meta_reasons[code] = {
                "shares_reason": meta.get("shares_reason"),
                "float_reason": meta.get("float_reason"),
                "shares_applied_to": last_date if shares is not None else None,
            }

            for d in trading_dates:
                if d in df.index:
                    c_open = df.loc[d, "open"]
                    c_high = df.loc[d, "high"]
                    c_low = df.loc[d, "low"]
                    c_close = df.loc[d, "close"]
                    c_vol = df.loc[d, "vol"]

                    tradamt_eok = round((c_close * c_vol) / 100_000_000, 2)

                    matrices["open"].loc[d, col] = c_open
                    matrices["high"].loc[d, col] = c_high
                    matrices["low"].loc[d, col] = c_low
                    matrices["close"].loc[d, col] = c_close
                    matrices["tradamt"].loc[d, col] = tradamt_eok

                    if d == last_date:
                        mkt_eok = (
                            int((c_close * shares) / 100_000_000)
                            if shares is not None
                            else np.nan
                        )
                        snapshot_rows.append({
                            "code": code,
                            "name": stock_name,
                            "mkt": mkt_eok,
                            "shares": shares if shares is not None else np.nan,
                            "float": float_ratio if float_ratio is not None else np.nan,
                        })
                else:
                    for f in DAILY_FILES:
                        matrices[f].loc[d, col] = np.nan

            success_count += 1
            print("✅ 완료")
            time.sleep(0.05)

        if success_count == 0:
            print("🚨 수집된 데이터가 없습니다.")
            return

        # 3. OHLCV CSV 저장 (기존 규격: 1행 Name, 1열 Code, CP949 인코딩)
        print(f"\n💾 {len(DAILY_FILES)}개 일봉 CSV 파일 저장 중...")
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

        # 3.5 mkt/shares/float — tidy 스냅샷에 오늘 관측값을 기록하고, 그 전체를
        # 피벗해 wide CSV 를 파생시킨다 (rev.2 §5). 스냅샷이 없는 과거 날짜는
        # 구조적으로 NaN — broadcast 가 다시 생길 경로가 없다.
        if snapshot_rows:
            snapshot_df = pd.DataFrame(snapshot_rows)
            snap_path = write_snapshot(self.output_dir, last_date, snapshot_df)
            print(f"  📁 snapshots/{snap_path.name} 저장 완료 ({len(snapshot_df)}종목)")
            rebuild_wide_csvs(self.output_dir, name_map=name_map)
            for f in ("mkt", "shares", "float"):
                print(f"  📁 {f}.csv 파생 완료 (스냅샷 누적분 기준)")
        else:
            print("  ⚠️ 오늘자 스냅샷 행이 없어 mkt/shares/float.csv 를 다시 만들지 않았습니다.")

        # 4. shares/float 결측 사유 매니페스트 — "왜 비었는가"가 조용히 사라지지 않게.
        manifest_path = self.output_dir / "_meta_manifest.json"
        manifest = {
            "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_date": last_date,
            "note": (
                "shares/float/mkt 는 sampledata/Daily/snapshots/ 의 tidy 스냅샷에서 "
                "파생된다 — 스냅샷 없는 날짜는 NaN. float 는 크롤러 부재로 항상 "
                "NaN — rev.2 §2'/§5"
            ),
            "codes": meta_reasons,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  📁 {manifest_path.name} 저장 완료 (결측 사유 {len(meta_reasons)}종목)")


if __name__ == "__main__":
    collector = FastDailyCollector()

    # 과거 2022년 샘플 데이터 + 오늘 실시간 데이터 날짜까지 한 번에 수집!
    # (원하시는 기간으로 언제든 변경 가능합니다)
    collector.collect(
        start_date="20220420",
        end_date=datetime.now().strftime("%Y%m%d"),  # 2022년부터 오늘 날짜까지
        target_tickers=["000270", "005930", "000660"],
    )