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


#: 거시 피처 커버리지 기본 임계치 (%). 미만이면 경고, --strict 면 실패.
DEFAULT_COVERAGE_THRESHOLD = 95.0


def measure_coverage(day: pd.DataFrame, features: Sequence) -> dict:
    """
    거시 피처가 실제로 몇 %의 종목/행에 채워졌는지 잰다 (ARCHITECTURE_V2.md §3.6.1).

    결손 자체보다 **결손을 모르는 것**이 위험하다. fs_v1 20220425 는 200종목 중
    3종목만 거시 피처를 갖고 있었는데(커버리지 1.5%), 파일 어디에도 그 사실이
    적혀 있지 않았다. 그 상태로 filters.macro.enabled 를 켜면 유니버스의 98.5%가
    에러 없이 사라지거나(reject) 필터가 꺼진 채 켜져 있다고 착각하게 된다(skip_filter).

    종목 단위와 행 단위를 함께 잰다. 거시 피처는 종목당 값이 하나이므로 판단의
    기준은 종목 커버리지이고, 행 커버리지는 §3.5.2 의 중복 저장 규모를 보여준다.
    """
    macro_names = [
        f.name for f in features
        if getattr(f, "effective_native_resolution", f.resolution) == "daily"
    ]
    total_rows = len(day)
    total_codes = day["code"].nunique()

    per_feature: dict[str, dict] = {}
    for name in macro_names:
        if name not in day.columns:
            continue
        present = day[name].notna()
        codes_with = day.loc[present, "code"].nunique()
        per_feature[name] = {
            "code_coverage_pct": round(codes_with / total_codes * 100, 2) if total_codes else 0.0,
            "row_coverage_pct": round(int(present.sum()) / total_rows * 100, 2) if total_rows else 0.0,
            "codes_with_value": int(codes_with),
        }

    worst = min((v["code_coverage_pct"] for v in per_feature.values()), default=100.0)
    return {
        "total_codes": int(total_codes),
        "total_rows": int(total_rows),
        "macro_features": macro_names,
        "min_code_coverage_pct": worst,
        "per_feature": per_feature,
    }


def report_coverage(date: str, coverage: dict, threshold: float, strict: bool) -> bool:
    """커버리지를 출력하고 임계치 충족 여부를 돌려준다."""
    worst = coverage["min_code_coverage_pct"]
    if not coverage["per_feature"]:
        print(f"   ℹ️ {date}: 거시 피처가 없습니다 (커버리지 게이트 해당 없음)")
        return True

    if worst >= threshold:
        print(f"   ✅ 거시 피처 커버리지 최저 {worst:.1f}% (임계 {threshold:.0f}%)")
        return True

    label = "❌" if strict else "⚠️"
    print(f"   {label} 거시 피처 커버리지 최저 {worst:.1f}% < 임계 {threshold:.0f}%")
    for name, stat in sorted(coverage["per_feature"].items(), key=lambda kv: kv[1]["code_coverage_pct"]):
        print(
            f"      {name:<16} 종목 {stat['codes_with_value']:>4}/{coverage['total_codes']} "
            f"({stat['code_coverage_pct']:>6.2f}%) · 행 {stat['row_coverage_pct']:>6.2f}%"
        )
    print(
        "      → 이 상태로 filters.macro.enabled 를 켜면 on_missing 정책에 따라 "
        "유니버스 대부분이 진입 금지(reject)되거나 필터가 무력화(skip_filter)된다"
    )
    return False


def build_day(
    date: str,
    *,
    codes: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    version: str = "fs_v1",
    feature_root: Optional[str] = None,
    coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD,
    strict: bool = False,
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

    # validate before publish: 커버리지를 게시 전에 재고, --strict 실패면 canonical
    # parquet/매니페스트/coverage 를 하나도 건드리지 않고 멈춘다. 기존 날짜 산출물은 그대로다.
    # non-strict 는 기존처럼 경고만 하고 게시하며, 그 coverage 를 함께 기록한다.
    coverage = measure_coverage(day, ordered)
    if not report_coverage(date, coverage, coverage_threshold, strict) and strict:
        raise SystemExit(
            f"{date}: 거시 피처 커버리지가 임계치({coverage_threshold:.0f}%) 미만입니다 "
            "(--strict, 게시하지 않음 — 기존 산출물 유지)"
        )

    store = FeatureStore(version=version, root=feature_root)
    out_path = store.write(date, day, feature_set, coverage=coverage)

    stats = store.file_stats(date)
    elapsed = time.perf_counter() - started
    print(
        f"✅ {date}: {stats['rows']:,}행 x {stats['columns']}컬럼 "
        f"({len(frames)}종목, 스킵 {skipped}) · {elapsed:.1f}s · "
        f"{stats['file_bytes'] / 1024 / 1024:.2f}MB · row group {stats['row_groups']}개"
    )
    print(f"   ➔ {out_path}")
    return out_path


def refresh_declarations(store: FeatureStore, features: Sequence) -> int:
    """
    매니페스트의 해상도 표기를 현재 피처 선언과 맞춘다 (§3.6.2).

    피처 **값**은 건드리지 않는다. 고치는 것은 "이 파일에 무엇이 어떤 해상도로
    들어 있는가"라는 서술뿐이다. obi_top3 가 resolution:"tick" 으로 적혀 있었지만
    실제로는 1초봉 행에 저장돼 있던, 매니페스트가 사실과 달랐던 상태를 바로잡는다.

    version 이 다르면 계산식이 바뀐 것이므로 조용히 덮어쓰지 않고 에러를 낸다 —
    그건 표기 오류가 아니라 fs_v2 로 분기해야 할 사안이다.
    """
    manifest = store.read_manifest()
    if not manifest:
        return 0

    by_name = {f.name: f for f in features}
    changed = 0
    for entry in manifest.get("features", []):
        feature = by_name.get(entry["name"])
        if feature is None:
            continue
        if entry.get("version") != feature.version:
            raise ValueError(
                f"{entry['name']}: 매니페스트 버전 {entry.get('version')} vs 코드 "
                f"{feature.version}. 계산식이 바뀌었다면 fs_v2 로 분기하세요"
            )
        native = feature.effective_native_resolution
        if entry.get("resolution") != feature.resolution or entry.get("native_resolution") != native:
            entry["resolution"] = feature.resolution
            entry["native_resolution"] = native
            changed += 1

    if changed:
        store.replace_manifest(manifest)
    return changed


def recompute_coverage(
    date: str,
    *,
    version: str = "fs_v1",
    feature_root: Optional[str] = None,
    coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD,
    strict: bool = False,
) -> bool:
    """
    이미 만들어진 parquet 을 다시 계산하지 않고 커버리지만 재측정한다.

    커버리지 게이트가 없던 시절에 빌드된 파일에 사후로 기록을 채워 넣기 위한 경로다.
    피처 값은 건드리지 않으므로 재현성에 영향이 없다.
    """
    registry.bootstrap()
    features = list(registry.all_features().values())
    store = FeatureStore(version=version, root=feature_root)

    refreshed = refresh_declarations(store, features)
    if refreshed:
        print(f"🏷️ 해상도 표기 {refreshed}건을 실제 저장 형태에 맞춰 갱신했습니다 (§3.6.2)")

    macro_names = [
        f.name for f in features
        if getattr(f, "effective_native_resolution", f.resolution) == "daily"
    ]
    frame = store.read(date, names=macro_names)
    coverage = measure_coverage(frame, features)
    store.record_coverage(date, coverage)
    print(f"🔁 {date}: 커버리지 재측정 ({coverage['total_codes']}종목 {coverage['total_rows']:,}행)")
    return report_coverage(date, coverage, coverage_threshold, strict)


def main() -> int:
    parser = argparse.ArgumentParser(description="하루치 LOB -> 피처 parquet")
    parser.add_argument("--date", help="YYYYMMDD 하루만")
    parser.add_argument("--start", help="시작일 (범위)")
    parser.add_argument("--end", help="종료일 (범위)")
    parser.add_argument("--codes", nargs="*", help="대상 종목코드 (기본: 전 종목)")
    parser.add_argument("--limit", type=int, help="종목 수 제한 (테스트용)")
    parser.add_argument("--version", default="fs_v1", help="피처셋 버전 디렉토리")
    parser.add_argument("--feature-root", help="피처 저장 루트 (기본: sampledata/features)")
    parser.add_argument(
        "--coverage-threshold", type=float, default=DEFAULT_COVERAGE_THRESHOLD,
        help=f"거시 피처 커버리지 임계치 %% (기본 {DEFAULT_COVERAGE_THRESHOLD:.0f})",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="커버리지가 임계치 미만이면 경고가 아니라 실패로 처리한다",
    )
    parser.add_argument(
        "--coverage-only", action="store_true",
        help="피처를 다시 만들지 않고 기존 parquet 의 커버리지만 재측정해 기록한다",
    )
    args = parser.parse_args()

    if args.date:
        dates = [args.date]
    elif args.start and args.end:
        if args.coverage_only:
            available = FeatureStore(version=args.version, root=args.feature_root).available_dates()
        else:
            available = sorted(p.stem.replace("_LOB", "") for p in SEC_PATH.glob("*_LOB.db"))
        dates = [d for d in available if args.start <= d <= args.end]
    else:
        parser.error("--date 또는 --start/--end 가 필요합니다")

    if not dates:
        print("⚠️ 대상 날짜가 없습니다")
        return 1

    if args.coverage_only:
        ok = True
        for date in dates:
            ok &= recompute_coverage(
                date,
                version=args.version,
                feature_root=args.feature_root,
                coverage_threshold=args.coverage_threshold,
                strict=args.strict,
            )
        return 0 if ok or not args.strict else 1

    for date in dates:
        build_day(
            date,
            codes=args.codes,
            limit=args.limit,
            version=args.version,
            feature_root=args.feature_root,
            coverage_threshold=args.coverage_threshold,
            strict=args.strict,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
