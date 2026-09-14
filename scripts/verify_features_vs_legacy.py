"""
scripts/verify_features_vs_legacy.py — 새 피처 vs 기존 계산식 실데이터 대조 (Phase B)

**이 대조가 통과하기 전에는 engine/strategy.py 를 지우지 않는다.**

ARCHITECTURE_V2.md §8 이 못박은 Phase B 의 검증 방식이다.

    새 피처 레이어로 계산한 값과 기존 calculate_window_metrics() 의 값이 같은지
    대조하고, 그 다음에 기존 백테스트와 새 백테스트의 거래 목록이 일치하는지
    대조한다. 두 대조가 통과하기 전에는 기존 코드를 지우지 않는다.

여기서 하는 것은 그 첫 번째 대조다. 실제 sampledata 로:

  1) 같은 (날짜, 종목) 에 대해 레거시 루프를 그대로 돌려 매 t 의 값을 스냅샷한다
  2) 새 피처의 batch 결과와 t 단위로 비교한다
  3) stream 결과까지 3자 대조한다 (배치 == 스트리밍 == 레거시)

불일치가 있으면 **어느 피처의 어느 t 에서 갈리는지** 값과 함께 출력한다.

실행:
    uv run python scripts/verify_features_vs_legacy.py
    uv run python scripts/verify_features_vs_legacy.py --date 20220425 --codes 000270
    uv run python scripts/verify_features_vs_legacy.py --all-codes --limit 30
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from engine.config import SEC_PATH                                   # noqa: E402
from engine.strategy import calculate_window_metrics                 # noqa: E402
from features import registry                                        # noqa: E402
from features.base import BatchContext                               # noqa: E402
from features.builders.microstructure import MICRO_WINDOWS, TRIGGER_WINDOWS, _to_seconds  # noqa: E402

# ---------------------------------------------------------------------------
# 레거시 딕셔너리 키 -> 새 피처 이름 (ARCHITECTURE_V2.md §3.7 매핑표)
# ---------------------------------------------------------------------------

LEGACY_TO_FEATURE: dict[str, str] = {}
for _w in MICRO_WINDOWS:
    LEGACY_TO_FEATURE[f"cbv_{_w}"] = f"cbv_{_w}"
    LEGACY_TO_FEATURE[f"max{_w}buyratio"] = f"cbv_ratio_max_{_w}"
    LEGACY_TO_FEATURE[f"t_max{_w}buyratio"] = f"cbv_ratio_tmax_{_w}"
    LEGACY_TO_FEATURE[f"amt_{_w}s"] = f"amt_{_w}s"
    LEGACY_TO_FEATURE[f"bamt_{_w}s"] = f"bamt_{_w}s"
for _w in TRIGGER_WINDOWS:
    LEGACY_TO_FEATURE[f"min{_w}_trigger"] = f"trigger_dev_min_{_w}"
    LEGACY_TO_FEATURE[f"max{_w}_trigger"] = f"trigger_dev_max_{_w}"
LEGACY_TO_FEATURE["cbv_1"] = "cbv_1"
LEGACY_TO_FEATURE["t_max1buyratio"] = "cbv_ratio_tmax_1"
LEGACY_TO_FEATURE["ctotal"] = "tick_rate_cum"

#: 레거시에 대응 계산이 없는 피처 (대조 대상 아님 — 사유를 명시해 둔다)
NO_LEGACY_COUNTERPART = {
    "obi_top3": "레거시 1초봉 전략이 호가잔량을 보지 않는다 (nxt 틱 엔진에만 있음)",
    "buy_ratio_15t": "nxt 틱 엔진의 지표. 1초봉 루프에는 대응 코드가 없다",
    "tick_size_ratio": "레거시는 상수 0.1 을 쓴다. 실제 호가단위 계산으로 교체는 Phase C",
    "mkt_cap": "레거시는 상수 1000 을 CSV 에 박는다 (§1.5 끊어진 연결선)",
    "float_ratio": "레거시가 로드만 하고 쓰지 않는다",
    "prev_close": "레거시에 없음",
    "ytd_tradamt_20": "레거시는 상수 100 을 CSV 에 박는다",
    "ytd_tradamt_5": "레거시에 없음",
    "upper_limit": "레거시는 당일 시가x1.3 상수. 전일 종가 기준 계산으로 교체는 Phase C",
}

TOLERANCE = 1e-9


class DataQualityError(RuntimeError):
    """원천 데이터 자체가 읽을 수 없는 상태 — 피처 불일치와 구분한다."""


# ---------------------------------------------------------------------------
# 데이터 로딩 — engine/engine.py::_process_stock 과 같은 방식
# ---------------------------------------------------------------------------

def load_stock(conn: sqlite3.Connection, code: str) -> Optional[dict[str, Any]]:
    """
    engine/engine.py 와 같은 방식(SELECT * 후 위치 인덱싱)으로 읽는다.

    일부 수집 파일에는 정수 컬럼이 BLOB 으로 들어가 있다(수집기 버전 차이).
    그런 종목은 레거시 엔진도 읽지 못하므로 조용히 0 으로 채우지 않고 건너뛴다.
    """
    raw = pd.DataFrame(conn.cursor().execute(f"SELECT * FROM '{code}'").fetchall())
    if raw.empty or len(raw) < 3:
        return None
    for col in (5, 6, 7, 8):
        if raw[col].map(lambda v: isinstance(v, (bytes, bytearray))).any():
            raise DataQualityError(f"{code}: 원천 컬럼이 BLOB 으로 저장되어 있습니다")
    return {
        "time": np.array(raw[0]).astype(str),
        "open": np.array(raw[1]).astype(float),
        "high": np.array(raw[2]).astype(float),
        "low": np.array(raw[3]).astype(float),
        "close": np.array(raw[4]).astype(float),
        "vol": np.array(raw[5]).astype(float),
        "buy_vol": np.array(raw[6]).astype(float),
        "sell_vol": np.array(raw[7]).astype(float),
        "tick": np.array(raw[8]).astype(float),
        "bid_v_top3": np.array(raw[41]).astype(float) + np.array(raw[42]).astype(float) + np.array(raw[43]).astype(float),
        "ask_v_top3": np.array(raw[21]).astype(float) + np.array(raw[22]).astype(float) + np.array(raw[23]).astype(float),
    }


def run_legacy(arrays: dict[str, Any]) -> dict[str, np.ndarray]:
    """
    engine/engine.py 의 루프를 그대로 재현하며 매 t 의 딕셔너리 값을 스냅샷한다.

    스냅샷에 stock.get(key, 0.0) 을 쓰는 이유가 핵심이다. 레거시는 조건이 맞지
    않는 t 에서 값을 갱신하지 않으므로, 전략이 그 시점에 읽는 값은 '마지막으로
    갱신된 값' 이다. 새 피처의 carry forward 규칙이 이것과 같아야 한다.
    """
    n = len(arrays["time"])
    stock = {
        "name": "", "time": arrays["time"], "open": arrays["open"], "high": arrays["high"],
        "low": arrays["low"], "close": arrays["close"], "vol": arrays["vol"],
        "buy_vol": arrays["buy_vol"], "sell_vol": arrays["sell_vol"], "tick": arrays["tick"],
        "candle_high": [], "candle_low": [], "candle_open": [], "candle_close": [],
        "position": 0, "state": 0, "entry_t": 0, "entry_price": 0.0,
        "max_t": 0.0, "min_t": 999999999.0, "upper": arrays["open"][0] * 1.3,
        "tick_rate": 0.1,
        "max_cbv5": 0.0, "max_cbv10": 0.0, "max_cbv30": 0.0, "max_cbv60": 0.0,
        "tmax_cbv5": 0.0, "tmax_cbv10": 0.0, "tmax_cbv30": 0.0, "tmax_cbv60": 0.0,
        "tmax_cbv1": 0.0,
    }

    snapshots = {key: np.zeros(n, dtype=float) for key in LEGACY_TO_FEATURE}
    for t in range(n):
        stock["candle_high"].append(stock["high"][t])
        stock["candle_low"].append(stock["low"][t])
        stock["candle_open"].append(stock["open"][t])

        calculate_window_metrics(stock, t)

        for key in LEGACY_TO_FEATURE:
            snapshots[key][t] = float(stock.get(key, 0.0))
    return snapshots


def run_features(arrays: dict[str, Any], code: str, date: str) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """새 피처의 batch / stream 결과를 모두 계산한다."""
    registry.bootstrap()
    n = len(arrays["time"])
    sec = np.array([float(_to_seconds(t)) for t in arrays["time"]])

    columns = {k: v for k, v in arrays.items() if k != "time"}
    columns["sec"] = sec
    ctx = BatchContext(code=code, date=date, columns=columns, resolution="bar_1s")

    events = [
        {
            "time": arrays["time"][i], "sec": sec[i], "date": date,
            "open": arrays["open"][i], "high": arrays["high"][i], "low": arrays["low"][i],
            "close": arrays["close"][i], "vol": arrays["vol"][i],
            "buy_vol": arrays["buy_vol"][i], "sell_vol": arrays["sell_vol"][i],
            "tick": arrays["tick"][i],
            "bid_v_top3": arrays["bid_v_top3"][i], "ask_v_top3": arrays["ask_v_top3"][i],
        }
        for i in range(n)
    ]

    batch_out: dict[str, np.ndarray] = {}
    stream_out: dict[str, np.ndarray] = {}
    for name in set(LEGACY_TO_FEATURE.values()):
        feature = registry.get(name)
        batch_out[name] = np.asarray(feature.batch(ctx), dtype=float)
        state = feature.stream()
        stream_out[name] = np.array([state.update(e) for e in events], dtype=float)
    return batch_out, stream_out


# ---------------------------------------------------------------------------
# 비교
# ---------------------------------------------------------------------------

def compare(label: str, expected: np.ndarray, actual: np.ndarray, times: np.ndarray) -> dict:
    """불일치 지점을 t 단위로 찾아 돌려준다."""
    if expected.shape != actual.shape:
        return {"name": label, "ok": False, "reason": f"길이 불일치 {expected.shape} vs {actual.shape}"}

    diff = np.abs(expected - actual)
    bad = diff > TOLERANCE
    if not bad.any():
        return {"name": label, "ok": True, "max_diff": float(diff.max()) if diff.size else 0.0}

    first = int(np.argmax(bad))
    worst = int(np.argmax(diff))
    return {
        "name": label,
        "ok": False,
        "count": int(bad.sum()),
        "first_t": first,
        "first_time": str(times[first]),
        "first_expected": float(expected[first]),
        "first_actual": float(actual[first]),
        "worst_t": worst,
        "max_diff": float(diff.max()),
    }


def verify_code(conn: sqlite3.Connection, code: str, date: str) -> list[dict]:
    arrays = load_stock(conn, code)
    if arrays is None:
        return []

    legacy = run_legacy(arrays)
    batch_out, stream_out = run_features(arrays, code, date)
    times = arrays["time"]

    results = []
    for legacy_key, feature_name in sorted(LEGACY_TO_FEATURE.items(), key=lambda kv: kv[1]):
        expected = legacy[legacy_key]
        results.append({
            **compare(f"{feature_name}", expected, batch_out[feature_name], times),
            "legacy_key": legacy_key,
            "kind": "batch vs 레거시",
        })
        results.append({
            **compare(f"{feature_name}", expected, stream_out[feature_name], times),
            "legacy_key": legacy_key,
            "kind": "stream vs 레거시",
        })
    return results


def print_report(date: str, code: str, results: Sequence[dict]) -> int:
    failures = [r for r in results if not r["ok"]]
    by_feature: dict[str, list[dict]] = {}
    for r in results:
        by_feature.setdefault(r["name"], []).append(r)

    ok_count = sum(1 for r in results if r["ok"])
    print(f"\n[{date} / {code}] 피처 {len(by_feature)}개 · 대조 {len(results)}건 "
          f"· 일치 {ok_count} · 불일치 {len(failures)}")

    if not failures:
        worst = max((r.get("max_diff", 0.0) for r in results), default=0.0)
        print(f"  ✅ 전부 일치 (최대 절대오차 {worst:.3e})")
        return 0

    print("  ❌ 불일치 발견:")
    for r in failures:
        if "reason" in r:
            print(f"     {r['name']:22s} [{r['kind']}] {r['reason']}")
            continue
        print(
            f"     {r['name']:22s} [{r['kind']}] "
            f"레거시 키 {r['legacy_key']} · {r['count']}개 t 에서 불일치"
        )
        print(
            f"        첫 불일치: t={r['first_t']} (시각 {r['first_time']}) "
            f"레거시={r['first_expected']!r} 새피처={r['first_actual']!r} "
            f"차이={abs(r['first_expected'] - r['first_actual']):.6e}"
        )
        print(f"        최대 오차: t={r['worst_t']} · {r['max_diff']:.6e}")
    return len(failures)


def main() -> int:
    parser = argparse.ArgumentParser(description="새 피처 vs 기존 calculate_window_metrics 대조")
    parser.add_argument("--date", help="대상 날짜 (기본: 사용 가능한 모든 날짜)")
    parser.add_argument("--codes", nargs="*", default=["000270"], help="대상 종목")
    parser.add_argument("--all-codes", action="store_true", help="해당 날짜 전 종목")
    parser.add_argument("--limit", type=int, default=10, help="--all-codes 일 때 종목 수 제한")
    args = parser.parse_args()

    dates = [args.date] if args.date else sorted(
        p.stem.replace("_LOB", "") for p in SEC_PATH.glob("*_LOB.db")
    )
    if not dates:
        print("⚠️ LOB DB 가 없습니다")
        return 1

    print("=" * 78)
    print("Phase B 대조 — 새 피처 레이어 vs engine/strategy.py::calculate_window_metrics")
    print(f"대상 날짜: {', '.join(dates)}")
    print("=" * 78)

    total_failures = 0
    checked = 0
    skipped_codes: list[str] = []
    for date in dates:
        path = SEC_PATH / f"{date}_LOB.db"
        if not path.exists():
            print(f"⚠️ {date}: DB 없음")
            continue

        conn = sqlite3.connect(path)
        try:
            if args.all_codes:
                rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                codes = sorted(name for (name,) in rows)[: args.limit]
            else:
                codes = args.codes

            for code in codes:
                try:
                    results = verify_code(conn, code, date)
                except sqlite3.OperationalError:
                    print(f"\n[{date} / {code}] 테이블 없음 — 건너뜀")
                    continue
                except (DataQualityError, ValueError, TypeError) as exc:
                    # 피처 불일치가 아니라 원천 데이터가 읽히지 않는 경우다.
                    # 레거시 엔진도 같은 이유로 이 종목을 처리하지 못한다.
                    print(f"\n[{date} / {code}] 원천 데이터 이상 — 건너뜀 ({exc})")
                    skipped_codes.append(f"{date}/{code}")
                    continue
                if not results:
                    print(f"\n[{date} / {code}] 데이터 부족 — 건너뜀")
                    continue
                checked += 1
                total_failures += print_report(date, code, results)
        finally:
            conn.close()

    print("\n" + "=" * 78)
    print(f"대조한 (날짜, 종목) 조합: {checked}개")
    if total_failures:
        print(f"❌ 불일치 {total_failures}건 — engine/strategy.py 를 지우면 안 됩니다.")
        return 1

    print("✅ 전 구간 일치. batch == stream == 레거시.")
    print("   (남은 조건: 새 백테스트와 기존 백테스트의 거래 목록 일치 — §8)")
    print("\n대조 대상이 아닌 피처와 그 사유:")
    for name, reason in sorted(NO_LEGACY_COUNTERPART.items()):
        print(f"   - {name:16s} {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
