"""
2단계: Raw 틱데이터를 백테스트용 1초봉 LOB DB로 변환(Resampling)하는 배치 엔진
- 입력: sampledata/raw_ticks/{YYYYMMDD}_raw.db
- 출력: sampledata/temp/{YYYYMMDD}_LOB.db (테이블별 51개 컬럼)
"""

from datetime import datetime
from pathlib import Path
import argparse
import sqlite3
import sys
import time
import traceback
import numpy as np
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


NUMERIC_COLUMNS = LOB_COLUMNS[1:]  # "time"(TEXT) 제외, 전부 REAL 선언 컬럼

# raw_quotes 의 콤마 문자열 컬럼 -> LOB 의 10호가 컬럼. 순서는 LOB_COLUMNS 와 같다.
QUOTE_FIELDS = (
    ("offer_p", "float64"),
    ("offer_v", "int64"),
    ("bid_p", "float64"),
    ("bid_v", "int64"),
)
QUOTE_LEVELS = 10
QUOTE_COLUMNS = [
    f"{field}{i}" for field, _ in QUOTE_FIELDS for i in range(1, QUOTE_LEVELS + 1)
]

INDEXES = (
    ("idx_trades_code", "raw_trades(code, t_time)"),
    ("idx_quotes_code", "raw_quotes(code, q_time)"),
)


def validate_numeric_column_types(lob_conn: sqlite3.Connection, table: str) -> None:
    """table 의 숫자 컬럼이 BLOB/TEXT 로 저장되지 않았는지 검사한다.

    numpy 스칼라(int64 등)를 sqlite3 파라미터로 그대로 바인딩하면 컬럼을
    REAL/INTEGER 로 선언해 두어도 값이 BLOB 으로 저장되는 사례가 있었다
    (버퍼 프로토콜을 통해 바인딩되어 타입 변환 없이 원본 바이트가 들어감).
    같은 문제가 재발하면 피처 빌드가 조용히 실패하지 않고 여기서 즉시
    예외를 낸다.
    """
    bad = []
    for col in NUMERIC_COLUMNS:
        row = lob_conn.execute(
            f"SELECT typeof({col}) FROM '{table}' "
            f"WHERE typeof({col}) NOT IN ('integer', 'real') LIMIT 1"
        ).fetchone()
        if row is not None:
            bad.append((col, row[0]))
    if bad:
        detail = ", ".join(f"{col}={t}" for col, t in bad)
        raise TypeError(
            f"[{table}] 숫자 컬럼이 BLOB/TEXT 로 저장되었습니다: {detail}. "
            "INSERT 시 파이썬 int/float 로 명시 변환했는지 확인하세요."
        )


def _fmt_secs(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _db_size_gb(path: Path) -> float:
    """WAL 을 포함한 실제 점유 용량(GB)."""
    total = path.stat().st_size
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            total += side.stat().st_size
    return total / 1024**3


def create_raw_indexes(raw_conn: sqlite3.Connection) -> float:
    """종목별 조회용 인덱스를 1회 생성한다(데이터는 변경하지 않는다).

    인덱스가 없으면 종목마다 raw_trades/raw_quotes 전체를 훑는다. 3,600종목이면
    같은 4,000만 행을 3,600번 다시 읽는다는 뜻이다.
    """
    t0 = time.time()
    for name, target in INDEXES:
        raw_conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target};")
    raw_conn.commit()
    return time.time() - t0


def drop_raw_indexes(raw_conn: sqlite3.Connection) -> float:
    """리샘플이 끝난 뒤 인덱스를 제거한다(raw DB 는 장기 보관 대상이라 기본 동작).

    ⚠️ DROP INDEX 는 페이지를 freelist 로 돌릴 뿐 파일 크기를 줄이지 않는다.
       실제로 용량을 회수하려면 VACUUM 이 필요하다(전체 재작성이라 별도 판단).
    """
    t0 = time.time()
    for name, _ in INDEXES:
        raw_conn.execute(f"DROP INDEX IF EXISTS {name};")
    raw_conn.commit()
    return time.time() - t0


def aggregate_seconds(trades_df: pd.DataFrame) -> pd.DataFrame:
    """체결 틱 -> 1초봉 OHLCV/틱 집계 (파이썬 루프의 벡터화 버전).

    정의는 루프판과 한 글자도 다르지 않다.
      open  = 그 초의 첫 체결가      close = 마지막 체결가
      high  = max                    low   = min
      vol/buy_vol/sell_vol = 합계    tick  = 건수
      buy_tick/sell_tick   = is_buy 가 정확히 1/0 인 건수

    open/close 는 agg("first"/"last") 대신 위치 기반으로 뽑는다. agg 의
    first/last 는 NaN 을 건너뛰지만 원본의 iloc[0]/iloc[-1] 은 건너뛰지 않아,
    결측이 섞인 날 결과가 갈린다.
    """
    is_buy = trades_df["is_buy"]
    buy_mask = is_buy == 1
    sell_mask = is_buy == 0
    work = trades_df.assign(
        _buy_vol=trades_df["vol"].where(buy_mask, 0),
        _sell_vol=trades_df["vol"].where(sell_mask, 0),
        _buy_tick=buy_mask.astype("int64"),
        _sell_tick=sell_mask.astype("int64"),
    )

    agg = work.groupby("t_time", sort=True).agg(
        high=("price", "max"),
        low=("price", "min"),
        vol=("vol", "sum"),
        buy_vol=("_buy_vol", "sum"),
        sell_vol=("_sell_vol", "sum"),
        tick=("price", "size"),
        buy_tick=("_buy_tick", "sum"),
        sell_tick=("_sell_tick", "sum"),
    )
    agg["open"] = trades_df.drop_duplicates("t_time", keep="first").set_index("t_time")["price"]
    agg["close"] = trades_df.drop_duplicates("t_time", keep="last").set_index("t_time")["price"]
    return agg


def _parse_quote_levels(quotes_df: pd.DataFrame) -> pd.DataFrame:
    """'3565,3610,...' 콤마 문자열 4개를 40개 숫자 컬럼으로 1회만 파싱한다.

    루프판은 초마다 문자열을 다시 쪼갰다. 같은 호가 행이 여러 초에 매핑되면
    그만큼 중복 파싱이다. 여기서는 호가 행당 정확히 한 번만 쪼갠다.
    """
    parsed = pd.DataFrame(index=quotes_df.index)
    for field, dtype in QUOTE_FIELDS:
        levels = quotes_df[field].str.split(",", expand=True)
        if levels.shape[1] != QUOTE_LEVELS:
            raise ValueError(
                f"호가 문자열 '{field}' 의 항목 수가 {QUOTE_LEVELS} 이 아닙니다 "
                f"(관측 {levels.shape[1]}). raw DB 수집 포맷을 확인하세요"
            )
        levels.columns = [f"{field}{i}" for i in range(1, QUOTE_LEVELS + 1)]
        parsed[levels.columns] = levels.astype(dtype)
    return parsed


def map_quotes_backward(sec_index: pd.Index, quotes_df: pd.DataFrame) -> pd.DataFrame:
    """각 초에 '그 초 이전(포함) 가장 최신' 호가를 붙인다 (merge_asof 판).

    루프판의 `quotes_df[quotes_df["q_time"] <= sec].iloc[-1]` 과 같은 의미다.
      - direction="backward" = q_time <= t_time 중 가장 늦은 행
      - 같은 q_time 이 여러 행이면 merge_asof 도 iloc[-1] 과 마찬가지로 뒤엣것
      - 첫 호가보다 이른 초(오늘 데이터는 08:30~08:50 체결 구간)는 0 으로 채운다
    """
    price_cols = [c for c in QUOTE_COLUMNS if c.startswith(("offer_p", "bid_p"))]
    vol_cols = [c for c in QUOTE_COLUMNS if c.startswith(("offer_v", "bid_v"))]

    if quotes_df.empty:
        filled = pd.DataFrame(0, index=range(len(sec_index)), columns=QUOTE_COLUMNS)
    else:
        # HHMMSS 6자리 고정이라 정수 비교 == 원본의 문자열 사전식 비교
        left = pd.DataFrame({"_sec": sec_index.astype("int64")})
        right = _parse_quote_levels(quotes_df)
        right.insert(0, "_sec", quotes_df["q_time"].astype("int64").to_numpy())
        merged = pd.merge_asof(left, right, on="_sec", direction="backward")
        filled = merged[QUOTE_COLUMNS]

    out = pd.DataFrame(index=range(len(sec_index)))
    out[price_cols] = filled[price_cols].fillna(0.0).astype("float64")
    out[vol_cols] = filled[vol_cols].fillna(0).astype("int64")
    return out[QUOTE_COLUMNS]


def rows_for_sqlite(frame: pd.DataFrame) -> list:
    """DataFrame -> sqlite3 바인딩용 파이썬 네이티브 튜플 목록.

    ⚠️ numpy 스칼라(int64 등)를 그대로 바인딩하면 REAL 컬럼이어도 BLOB 으로
       저장된다(e6ba18c). Series 를 직접 순회하면 numpy 스칼라가 나오므로,
       컬럼마다 ndarray.tolist() 로 파이썬 int/float/str 로 변환한 뒤 zip 한다.
    """
    columns = [frame[col].to_numpy().tolist() for col in LOB_COLUMNS]
    return list(zip(*columns))


def build_lob_frame(trades_df: pd.DataFrame, quotes_df: pd.DataFrame) -> pd.DataFrame:
    """한 종목의 1초봉 LOB 프레임(51컬럼)을 만든다."""
    agg = aggregate_seconds(trades_df)
    quotes = map_quotes_backward(agg.index, quotes_df)

    frame = pd.DataFrame({"time": agg.index.to_numpy()})
    for col in ("open", "high", "low", "close"):
        frame[col] = agg[col].to_numpy(dtype="float64")
    for col in ("vol", "buy_vol", "sell_vol", "tick", "buy_tick", "sell_tick"):
        frame[col] = agg[col].to_numpy(dtype="int64")
    for col in QUOTE_COLUMNS:
        frame[col] = quotes[col].to_numpy()
    return frame[LOB_COLUMNS]


def _check_time_format(series: pd.Series, label: str, code: str) -> None:
    """HHMMSS 6자리 고정 가정을 검사한다 (정수 비교로 바꾸는 전제)."""
    lengths = series.str.len()
    if not (lengths == 6).all():
        raise ValueError(
            f"[{code}] {label} 이 HHMMSS 6자리가 아닙니다 "
            f"(길이 {sorted(set(lengths.unique().tolist()))}). 시간 비교 전제가 깨집니다"
        )


def resample_raw_to_lob(date_str: str, limit: int | None = None, keep_index: bool = False):
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

    size_before = _db_size_gb(raw_db_path)
    print(f"🗂️ raw DB 인덱스 생성 전: {size_before:.2f} GB")
    index_sec = create_raw_indexes(raw_conn)
    size_after = _db_size_gb(raw_db_path)
    print(
        f"🗂️ raw DB 인덱스 생성 후: {size_after:.2f} GB "
        f"(+{size_after - size_before:.2f} GB, {_fmt_secs(index_sec)} 소요)"
    )

    # 수집된 종목 리스트 확인
    codes = [
        r[0]
        for r in raw_conn.execute(
            "SELECT DISTINCT code FROM raw_trades"
        ).fetchall()
    ]
    if limit is not None:
        codes = codes[:limit]
        print(f"🔎 --limit {limit} 적용 — {len(codes)}종목만 처리합니다")

    col_defs = ", ".join([f"{col} REAL" for col in LOB_COLUMNS[1:]])
    placeholders = ", ".join(["?"] * len(LOB_COLUMNS))

    started = time.time()
    empty_codes, failures, total_rows = [], [], 0

    for idx, code in enumerate(codes, 1):
        try:
            # 1. 체결 데이터 로드
            trades_df = pd.read_sql_query(
                "SELECT * FROM raw_trades WHERE code=? ORDER BY t_time ASC",
                raw_conn,
                params=(code,),
            )
            if trades_df.empty:
                empty_codes.append(code)
                continue

            # 2. 호가 데이터 로드
            quotes_df = pd.read_sql_query(
                "SELECT * FROM raw_quotes WHERE code=? ORDER BY q_time ASC",
                raw_conn,
                params=(code,),
            )

            _check_time_format(trades_df["t_time"], "t_time", code)
            if not quotes_df.empty:
                _check_time_format(quotes_df["q_time"], "q_time", code)

            # 3. 1초봉 집계 + 호가 매핑 (벡터화)
            frame = build_lob_frame(trades_df, quotes_df)
            lob_rows = rows_for_sqlite(frame)

            # 4. 대상 테이블 생성 및 INSERT
            lob_conn.execute(f"DROP TABLE IF EXISTS '{code}';")
            lob_conn.execute(
                f"CREATE TABLE IF NOT EXISTS '{code}' (time TEXT, {col_defs});"
            )
            lob_conn.executemany(
                f"INSERT INTO '{code}' VALUES ({placeholders})", lob_rows
            )
            lob_conn.commit()
        except Exception as exc:
            failures.append((code, traceback.format_exc()))
            print(f"  ❌ [{idx}/{len(codes)}] 종목 [{code}] 실패: {exc}", flush=True)
            continue

        # 숫자 컬럼 오염은 한 종목의 데이터 문제가 아니라 파이프라인 전체의 문제다.
        # 종목별 실패로 삼켜서 계속 돌면 몇 시간짜리 런이 통째로 쓰레기가 되므로,
        # 일부러 try 밖에 두어 즉시 런을 멈춘다.
        validate_numeric_column_types(lob_conn, code)
        total_rows += len(lob_rows)

        elapsed = time.time() - started
        eta = elapsed / idx * (len(codes) - idx)
        print(
            f"  🔨 [{idx}/{len(codes)} {idx / len(codes) * 100:5.1f}%] {code} "
            f"{len(lob_rows):>6,}초봉 · 경과 {_fmt_secs(elapsed)} · ETA {_fmt_secs(eta)}",
            flush=True,
        )

    resample_sec = time.time() - started

    if keep_index:
        print("🗂️ --keep-index — raw DB 인덱스를 유지합니다")
    else:
        drop_sec = drop_raw_indexes(raw_conn)
        print(
            f"🗂️ raw DB 인덱스 삭제 완료 ({_fmt_secs(drop_sec)}) — "
            f"현재 {_db_size_gb(raw_db_path):.2f} GB "
            "(DROP 은 페이지를 재사용 대상으로 돌릴 뿐이라 파일은 줄지 않는다. "
            "실제 회수는 VACUUM 필요)"
        )

    raw_conn.close()
    lob_conn.close()

    print(
        f"\n🎉 [{date_str}_LOB.db] 변환 완료 — {len(codes) - len(empty_codes) - len(failures)}종목 "
        f"/ {total_rows:,}초봉 / 리샘플 {_fmt_secs(resample_sec)} "
        f"(인덱스 {_fmt_secs(index_sec)} 별도)"
    )
    print(f"   체결 0건 스킵: {len(empty_codes)}종목")
    if failures:
        print(f"   ❌ 실패 {len(failures)}종목:")
        for code, tb in failures:
            print(f"   ── {code}\n{tb}")
    return {
        "index_sec": index_sec,
        "resample_sec": resample_sec,
        "codes": len(codes),
        "empty": empty_codes,
        "failures": failures,
        "rows": total_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "date",
        nargs="?",
        default=datetime.now().strftime("%Y%m%d"),
        help="변환할 날짜 (YYYYMMDD, 기본: 오늘)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="처리할 종목 수 제한 (기본: 전 종목)"
    )
    parser.add_argument(
        "--keep-index",
        action="store_true",
        help="리샘플 후에도 raw DB 인덱스를 유지한다 (기본: 삭제)",
    )
    args = parser.parse_args()

    result = resample_raw_to_lob(args.date, limit=args.limit, keep_index=args.keep_index)
    return 1 if result and result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())