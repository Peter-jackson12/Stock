"""
scripts/build_features.py — 배치 피처 빌더 (Phase B)

하루치 LOB DB 한 개를 읽어 피처 parquet 한 개를 만든다.

    sampledata/temp/20220425_LOB.db  ->  sampledata/features/fs_v1/20220425.parquet

왜 미리 계산하는가. 피처는 시장 데이터의 **순수 함수**다. 파라미터가 바뀌어도
피처는 바뀌지 않는다. 그런데 지금은 engine/strategy.py 의 calculate_window_metrics()
가 백테스트 루프 안에 있어서, `amt_10s > 700` 의 700 을 750 으로 바꿔보는 순간
250만 회의 덧셈이 통째로 다시 돈다. 한 번 계산해 두면 임계값을 100번 바꿔도
피처 계산은 0번이다 (§3.1).

실행:
    uv run python scripts/build_features.py --date 20220425
    uv run python scripts/build_features.py --date 20220425 --codes 000270 005930
    uv run python scripts/build_features.py --start 20220425 --end 20220429 --limit 50

참고: ARCHITECTURE_V2.md §3
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable, Optional, Sequence

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
from features import registry                                        # noqa: E402
from features.base import BatchContext, FeatureSet, MacroFeature     # noqa: E402
from features.builders.microstructure import _to_seconds             # noqa: E402
from features.store import FeatureStore                              # noqa: E402

#: LOB 에서 읽어오는 원천 컬럼 (51컬럼 중 피처가 쓰는 것만)
SOURCE_COLUMNS = (
    "time", "open", "high", "low", "close", "vol", "buy_vol", "sell_vol", "tick",
    "bid_v1", "bid_v2", "bid_v3", "offer_v1", "offer_v2", "offer_v3",
)


def lob_path(date: str) -> Path:
    return SEC_PATH / f"{date}_LOB.db"


def list_codes(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return sorted(name for (name,) in rows)


def load_code(conn: sqlite3.Connection, code: str) -> Optional[pd.DataFrame]:
    """한 종목의 하루치 1초봉. 피처가 쓰는 컬럼만 꺼낸다."""
    columns = ", ".join(SOURCE_COLUMNS)
    frame = pd.read_sql_query(f"SELECT {columns} FROM '{code}'", conn)
    if frame.empty:
        return None

    # 일부 수집 파일은 정수 컬럼이 BLOB 으로 들어가 있다(수집기 버전 차이).
    # to_numeric 이 조용히 NaN -> 0 으로 만들면 '거래량 0' 인 가짜 피처가 생긴다.
    # 그런 종목은 계산하지 않고 건너뛴다.
    for col in ("vol", "buy_vol", "sell_vol", "tick"):
        if frame[col].map(lambda v: isinstance(v, (bytes, bytearray))).any():
            raise ValueError(f"{code}: 원천 컬럼 {col} 이 BLOB 으로 저장되어 있습니다")

    frame["time"] = frame["time"].astype(str).str.zfill(6)
    for col in SOURCE_COLUMNS[1:]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0.0)

    # 파생 원천: 누적 초와 1~3호가 잔량 합
    # (BatchContext 는 숫자 배열만 담는다. 시각 문자열은 여기서 초로 바꾼다)
    frame["sec"] = [float(_to_seconds(t)) for t in frame["time"]]
    frame["bid_v_top3"] = frame[["bid_v1", "bid_v2", "bid_v3"]].sum(axis=1)
    frame["ask_v_top3"] = frame[["offer_v1", "offer_v2", "offer_v3"]].sum(axis=1)
    return frame


def build_code_frame(code: str, raw: pd.DataFrame, date: str, features: Sequence) -> pd.DataFrame:
    """한 종목의 피처를 전부 계산해 (code, time, 피처...) 프레임으로."""
    columns = {name: raw[name].to_numpy(dtype=float) for name in raw.columns if name != "time"}
    ctx = BatchContext(code=code, date=date, columns=columns, resolution="bar_1s")

    out = pd.DataFrame({"code": code, "time": raw["time"].to_numpy()})
    for feature in features:
        if isinstance(feature, MacroFeature):
            feature.bind(code, date)
        values = np.asarray(feature.batch(ctx), dtype=float)
        if values.size != len(raw):
            raise ValueError(
                f"{code}/{feature.name}: 길이 불일치 {values.size} != {len(raw)}"
            )
        out[feature.name] = values
        ctx.computed[feature.name] = values          # 피처가 피처를 참조할 수 있게
    return out


def build_day(
    date: str,
    *,
    codes: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    version: str = "fs_v1",
    feature_root: Optional[str] = None,
) -> Optional[Path]:
    path = lob_path(date)
    if not path.exists():
        print(f"⚠️ {date}: LOB DB 가 없습니다 ({path})")
        return None

    features = registry.bootstrap()
    feature_set = FeatureSet(version=version, features=tuple(registry.all_features().values()))
    ordered = feature_set.resolve_order()

    conn = sqlite3.connect(path)
    try:
        targets = list(codes) if codes else list_codes(conn)
        if limit:
            targets = targets[:limit]

        print(f"🔧 {date}: 종목 {len(targets)}개 x 피처 {len(ordered)}개 계산")
        started = time.perf_counter()
        frames, skipped = [], 0
        for i, code in enumerate(targets, 1):
            try:
                raw = load_code(conn, code)
            except Exception as exc:
                print(f"   ! {code} 로드 실패: {exc}")
                skipped += 1
                continue
            if raw is None or len(raw) < 2:
                skipped += 1
                continue
            frames.append(build_code_frame(code, raw, date, ordered))
            if i % 200 == 0:
                print(f"   … {i}/{len(targets)} ({time.perf_counter() - started:.1f}s)")
    finally:
        conn.close()

    if not frames:
        print(f"⚠️ {date}: 계산된 피처가 없습니다")
        return None

    day = pd.concat(frames, ignore_index=True)
    store = FeatureStore(version=version, root=feature_root)
    out_path = store.write(date, day, feature_set)

    stats = store.file_stats(date)
    elapsed = time.perf_counter() - started
    print(
        f"✅ {date}: {stats['rows']:,}행 x {stats['columns']}컬럼 "
        f"({len(frames)}종목, 스킵 {skipped}) · {elapsed:.1f}s · "
        f"{stats['file_bytes'] / 1024 / 1024:.2f}MB · row group {stats['row_groups']}개"
    )
    print(f"   ➔ {out_path}")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="하루치 LOB -> 피처 parquet")
    parser.add_argument("--date", help="YYYYMMDD 하루만")
    parser.add_argument("--start", help="시작일 (범위)")
    parser.add_argument("--end", help="종료일 (범위)")
    parser.add_argument("--codes", nargs="*", help="대상 종목코드 (기본: 전 종목)")
    parser.add_argument("--limit", type=int, help="종목 수 제한 (테스트용)")
    parser.add_argument("--version", default="fs_v1", help="피처셋 버전 디렉토리")
    parser.add_argument("--feature-root", help="피처 저장 루트 (기본: sampledata/features)")
    args = parser.parse_args()

    if args.date:
        dates = [args.date]
    elif args.start and args.end:
        available = sorted(p.stem.replace("_LOB", "") for p in SEC_PATH.glob("*_LOB.db"))
        dates = [d for d in available if args.start <= d <= args.end]
    else:
        parser.error("--date 또는 --start/--end 가 필요합니다")

    if not dates:
        print("⚠️ 대상 날짜가 없습니다")
        return 1

    for date in dates:
        build_day(
            date,
            codes=args.codes,
            limit=args.limit,
            version=args.version,
            feature_root=args.feature_root,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
