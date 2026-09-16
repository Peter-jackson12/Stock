"""네이버 일봉 수집기.
D-6: fchart는 삼성전자 2018년 분할 전 가격을 조정한다.
OHLCV는 unverified_fchart/에 격리하며 실제가용 wide CSV를 덮어쓰지 않는다.
현재 메타데이터는 한국 관측일에만 기록하며 과거 요청일에 소급하지 않는다.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import os
import uuid
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
from core.price_policy import PRICE_MANIFEST, ADJUSTED

#: 이 파일이 직접 wide CSV 로 쓰는 매트릭스. mkt/shares/float 는 daily_snapshot
#: 이 tidy 스냅샷에서 파생시키므로 여기 없다.
KST = timezone(timedelta(hours=9))

def observation_date() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


DAILY_FILES = ["open", "high", "low", "close", "tradamt"]


def _get(url: str, *, timeout: float, attempts: int = 3):
    """일시적인 통신 오류만 제한된 횟수로 재시도한다."""
    for attempt in range(attempts):
        try:
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            status = exc.response.status_code if exc.response is not None else None
            retryable = status is None or status in (408, 429) or status >= 500
            if not retryable or attempt == attempts - 1:
                raise
            time.sleep(0.5 * (2 ** attempt))


def parse_candles(code: str, text: str) -> tuple[str, pd.DataFrame, str | None]:
    """수집기와 응답 실측 도구가 공유하는 fchart 검증/파싱 경로."""
    name = f"종목_{code}"
    root = ET.fromstring(text)
    chartdata = root.find("chartdata")
    if chartdata is not None:
        name = chartdata.attrib.get("name", name)
    records = []
    for item in root.findall(".//item"):
        parts = item.attrib.get("data", "").split("|")
        if len(parts) != 6:
            raise ValueError("unexpected candle field count")
        date = parts[0].strip()
        if datetime.strptime(date, "%Y%m%d").strftime("%Y%m%d") != date:
            raise ValueError("invalid candle date")
        values = [float(value) for value in parts[1:]]
        if not all(np.isfinite(value) and value >= 0 for value in values):
            raise ValueError("invalid candle number")
        records.append(dict(zip(["date", "open", "high", "low", "close", "vol"], [date, *values])))
    if not records:
        return name, pd.DataFrame(), "no_candles"
    frame = pd.DataFrame(records).set_index("date").sort_index()
    if frame.index.has_duplicates:
        raise ValueError("duplicate candle date")
    return name, frame, None


def fetch_candles(code: str, count: int = 4000) -> tuple[str, pd.DataFrame, str | None]:
    """캔들 요청/파싱 실패를 메타데이터 수집과 분리한다."""
    name = f"종목_{code}"
    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count={count}&requestType=0"
    try:
        return parse_candles(code, _get(url, timeout=10).text)
    except (requests.RequestException, ET.ParseError, ValueError) as exc:
        return name, pd.DataFrame(), f"candle_fetch_failed: {type(exc).__name__}: {exc}"


def candle_coverage(frame, trading_dates):
    """Requested-calendar coverage, not proof of listing status or price basis."""
    requested = set(trading_dates)
    available = set(frame.index)
    matched = requested & available
    state = ("unknown_calendar" if not requested else "no_candles" if not available else
             "no_requested_rows" if not matched else "partial" if matched != requested else "complete")
    return dict(status=state, requested_days=len(requested), matched_days=len(matched),
                missing_days=len(requested - available),
                latest_available=max(available) if available else None,
                latest_requested=max(requested) if requested else None)


def fetch_current_meta(code: str) -> dict:
    meta = {
        "shares": None, "shares_reason": "no_source", "observed_date": None,
        "float": None, "float_reason": "no_crawler_implemented",
    }
    try:
        page = _get(f"https://finance.naver.com/item/main.naver?code={code}", timeout=5)
        meta["observed_date"] = observation_date()
        match = re.search(r"상장주식수.*?<em.*?>([\d,]+)</em>", page.text, re.DOTALL)
        if match:
            shares = int(match.group(1).replace(",", ""))
            if shares <= 0:
                raise ValueError("shares must be positive")
            meta.update(shares=shares, shares_reason=None)
        else:
            meta["shares_reason"] = "page_pattern_not_found"
    except (requests.RequestException, ValueError) as exc:
        meta["shares_reason"] = f"page_fetch_failed: {type(exc).__name__}: {exc}"
    return meta


def fetch_stock_meta_and_candles(
    code: str, count: int = 4000, *, include_meta: bool = True,
) -> tuple[str, pd.DataFrame, dict]:
    name, frame, reason = fetch_candles(code, count)
    meta = fetch_current_meta(code) if include_meta else {}
    meta["candles_reason"] = reason
    return name, frame, meta


def _record_attempt(path: Path, event: dict) -> None:
    """실행별 추기 로그. 중단 이후에도 완료된 관측과 사유를 확인한다."""
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


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
        targets = list(dict.fromkeys(str(t).zfill(6) for t in target_tickers))
        collected_date = observation_date()
        run_dir = self.output_dir / "collection_runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        journal = run_dir / f"{collected_date}_{uuid.uuid4().hex}.jsonl"
        _record_attempt(journal, {
            "event": "started", "at": datetime.now(KST).isoformat(),
            "start_date": start_date, "end_date": end_date, "targets": targets,
        })

        print(
            f"🚀 [네이버 API 직결] {len(targets)}개 종목 수집 시작: {start_date} ~ {end_date}"
        )

        # 1. 기준 영업일 달력 추출 (삼성전자 기준, count=4000)
        _, cal_df, _ = fetch_stock_meta_and_candles("005930", count=4000, include_meta=False)
        if cal_df.empty:
            print("❌ 영업일 캘린더 데이터를 가져오지 못했습니다.")
            cal_df = pd.DataFrame(index=pd.Index([], dtype=str))

        # 기간 필터링
        cal_df = cal_df.loc[
            (cal_df.index >= start_date) & (cal_df.index <= end_date)
        ]
        trading_dates = sorted(set(cal_df.index))

        # 빈 날짜 예외 방어
        if not trading_dates:
            print(
                f"❌ 지정한 기간({start_date} ~ {end_date})에 해당하는 개장일(영업일)이 없습니다."
            )

        if trading_dates:
            print(f"📅 대상 영업일: {trading_dates[0]} ~ {trading_dates[-1]} (총 {len(trading_dates)}일)")

        col_keys = [f"A{t}" for t in targets]

        # OHLCV 매트릭스 버퍼 초기화 (mkt/shares/float 는 tidy 스냅샷에서 파생 — §5)
        matrices = {
            f: pd.DataFrame(index=trading_dates, columns=col_keys)
            for f in DAILY_FILES
        }
        name_map = {}
        meta_reasons: dict[str, dict] = {}
        snapshot_rows: list[dict] = []
        last_date = trading_dates[-1] if trading_dates else None

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

            observed = meta.get("observed_date")
            coverage = candle_coverage(df, trading_dates)
            applicable = observed == collected_date and start_date <= collected_date <= end_date
            shares = meta["shares"] if applicable else None
            float_ratio = meta["float"] if applicable else None
            meta_reasons[code] = {
                "shares_reason": meta.get("shares_reason"),
                "float_reason": meta.get("float_reason"),
                "observed_date": observed,
                "shares_applied_to": collected_date if shares is not None else None,
                "snapshot_reason": None if applicable else "observation_date_outside_request_or_unknown",
                "candles_reason": meta.get("candles_reason") or ("no_candles" if df.empty else None),
                "candle_coverage": coverage,
            }
            if start_date <= collected_date <= end_date:
                close = df.loc[collected_date, "close"] if collected_date in df.index else None
                snapshot_rows.append({
                    "date": collected_date, "code": code, "name": stock_name,
                    "shares": shares, "float": float_ratio,
                    "mkt": int(close * shares / 100_000_000)
                    if close is not None and pd.notna(close) and shares is not None else np.nan,
                })
                # 뒤 종목/가격 파일 실패가 이미 받은 당일 관측을 잃게 하지 않는다.
                write_snapshot(self.output_dir, collected_date, pd.DataFrame([snapshot_rows[-1]]))
            _record_attempt(journal, {
                "event": "observed", "at": datetime.now(KST).isoformat(),
                "code": code, "meta": meta, "reasons": meta_reasons[code],
                "snapshot_date": collected_date if start_date <= collected_date <= end_date else None,
            })
            if df.empty:
                print("⚠️ 데이터 없음")
                continue
            if not coverage["matched_days"]:
                print(f"⚠️ 요청 기간 가격 없음 (응답 마지막 날짜: {coverage['latest_available']})")
                continue

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

                else:
                    for f in DAILY_FILES:
                        matrices[f].loc[d, col] = np.nan

            success_count += 1
            print("✅ 요청 기간 가격 수신" if coverage["status"] == "complete" else
                  f"⚠️ 요청 기간 일부 수신 ({coverage['matched_days']}/{coverage['requested_days']}일)")
            time.sleep(0.05)

        if success_count == 0 and not snapshot_rows:
            print("🚨 수집된 데이터가 없습니다.")
            _record_attempt(journal, {"event": "no_data", "at": datetime.now(KST).isoformat()})
            return

        # 3. OHLCV CSV 저장 (기존 규격: 1행 Name, 1열 Code, CP949 인코딩)
        print(f"\n💾 {len(DAILY_FILES)}개 일봉 CSV 파일 저장 중...")
        candle_dir = self.output_dir / "unverified_fchart"
        if success_count and trading_dates:
            candle_dir.mkdir(parents=True, exist_ok=True)
            # Mark the local price files BEFORE exporting any CSV. An interrupted
            # export must not leave unlabelled adjusted prices for a consumer.
            price_manifest = {
                "schema_version": 1,
                "source": "naver_fchart",
                "price_basis": ADJUSTED,
                "files": [f"{name}.csv" for name in DAILY_FILES],
            }
            marker_tmp = candle_dir / f".{uuid.uuid4().hex}.tmp"
            try:
                with marker_tmp.open("w", encoding="utf-8") as marker:
                    json.dump(price_manifest, marker, ensure_ascii=False, indent=2)
                    marker.flush()
                    os.fsync(marker.fileno())
                os.replace(marker_tmp, candle_dir / PRICE_MANIFEST)
            finally:
                marker_tmp.unlink(missing_ok=True)
            print("  ⚠️ D-6: fchart 분할조정 가격은 unverified_fchart/에 격리합니다.")
        for f in DAILY_FILES if success_count and trading_dates else []:
            mat = matrices[f]
            name_row = pd.DataFrame(
                [{c: name_map.get(c, "") for c in mat.columns}], index=["Name"]
            )
            final_df = pd.concat([name_row, mat])
            final_df.index.name = "Code"
            final_df.reset_index(inplace=True)

            out_path = candle_dir / f"{f}.csv"
            final_df.to_csv(out_path, encoding="CP949", index=False)
            print(f"  📁 {out_path.name} 저장 완료 (Shape: {final_df.shape})")

        # 3.5 mkt/shares/float — tidy 스냅샷에 오늘 관측값을 기록하고, 그 전체를
        # 피벗해 wide CSV 를 파생시킨다 (rev.2 §5). 스냅샷이 없는 과거 날짜는
        # 구조적으로 NaN — broadcast 가 다시 생길 경로가 없다.
        if snapshot_rows:
            snapshot_df = pd.DataFrame(snapshot_rows)
            snap_path = write_snapshot(self.output_dir, collected_date, snapshot_df)
            print(f"  📁 snapshots/{snap_path.name} 저장 완료 ({len(snapshot_df)}종목)")
            rebuild_wide_csvs(self.output_dir, name_map=name_map)
            for f in ("mkt", "shares", "float"):
                print(f"  📁 {f}.csv 파생 완료 (스냅샷 누적분 기준)")
        else:
            print("  ⚠️ 오늘자 스냅샷 행이 없어 mkt/shares/float.csv 를 다시 만들지 않았습니다.")

        # 4. shares/float 결측 사유 매니페스트 — "왜 비었는가"가 조용히 사라지지 않게.
        manifest_path = self.output_dir / "_meta_manifest.json"
        manifest = {
            "collected_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
            "last_date": last_date,
            "observation_date": collected_date,
            "attempt_log": str(journal),
            "price_source": "naver_fchart",
            "price_basis": "split_adjusted_observed",
            "d6_status": "blocked_pending_actual_price_source",
            "tradamt_basis": "close_times_volume_estimate",
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
        _record_attempt(journal, {"event": "completed", "at": datetime.now(KST).isoformat()})


if __name__ == "__main__":
    collector = FastDailyCollector()

    # 과거 2022년 샘플 데이터 + 오늘 실시간 데이터 날짜까지 한 번에 수집!
    # (원하시는 기간으로 언제든 변경 가능합니다)
    collector.collect(
        start_date="20220420",
        end_date=observation_date(),  # 2022년부터 오늘 날짜까지
        target_tickers=["000270", "005930", "000660"],
    )
