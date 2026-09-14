"""
scripts/verify_engine_feature_parity.py — 인라인 계산 vs 피처 스토어 거래 목록 대조 (Phase B-1)

**이 대조가 통과하기 전에는 engine/strategy.py 의 인라인 경로를 제거하지 않는다.**

ARCHITECTURE_V2.md §8 이 못박은 Phase B 검증의 두 번째 단계다.

    새 피처 레이어로 계산한 값과 기존 calculate_window_metrics() 의 값이 같은지
    대조하고, 그 다음에 **기존 백테스트와 새 백테스트의 거래 목록이 일치하는지**
    대조한다. 두 대조가 통과하기 전에는 기존 코드를 지우지 않는다.

첫 번째 단계(피처 값 t 단위 대조)는 scripts/verify_features_vs_legacy.py 가 한다.
여기서는 그 위층 — 같은 날짜·종목 범위를 두 경로로 백테스트해서 나온 **거래**가
같은지 본다. 피처 값이 전부 맞아도 엔진 배선이 틀리면(행 정렬, 키 매핑 누락)
거래가 갈리기 때문에, 값 대조만으로는 부족하다.

비교 대상은 표준 Trade 레코드의 모든 필드다 — 진입가·청산가·PnL·진입시각·
청산시각·청산사유·MAE/MFE·보유시간. 불일치가 있으면 **어느 거래의 어느 필드가
어떻게 다른지** 양쪽 값과 함께 출력한다.

속도도 함께 잰다. Phase B 의 이득("파라미터 스윕 시 피처 재계산 0회")이 실제로
몇 배인지는 --repeat 로 스윕을 흉내 내 보면 드러난다.

실행:
    uv run python scripts/verify_engine_feature_parity.py
    uv run python scripts/verify_engine_feature_parity.py --start 20220425 --end 20220504
    uv run python scripts/verify_engine_feature_parity.py --codes 000270 --repeat 3
"""

from __future__ import annotations

import argparse
import contextlib
import io
import shutil
import sys
import tempfile
import time
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from core.contracts import Trade                                        # noqa: E402
from engine.config import SEC_PATH                                      # noqa: E402
from engine.engine import (                                             # noqa: E402
    FEATURE_SOURCE_INLINE,
    FEATURE_SOURCE_STORE,
    BackTestEngine,
)
from engine.strategy import REQUIRED_FEATURES                           # noqa: E402
from features.store import KEY_COLUMNS, FeatureStore                    # noqa: E402

#: 거래 1건에서 대조하는 필드. run_id 는 피처셋 버전이 들어가 있어 당연히 다르고,
#: signal_meta 는 dict 라 따로 다룬다.
COMPARED_FIELDS: tuple[str, ...] = tuple(
    f.name for f in dataclass_fields(Trade)
    if f.name not in ("run_id", "signal_meta")
)

#: signal_meta 안에서 대조할 키 (진입 시점 피처 스냅샷 — 여기가 갈리면 배선 문제다)
COMPARED_META: tuple[str, ...] = ("cbv_1", "ctotal", "trigger", "day_open", "upper_limit")

#: float 비교 허용 오차. 두 경로는 같은 배정밀도 값을 써야 하므로 사실상 0 이어야 한다.
TOLERANCE = 1e-12


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def available_dates(start: str, end: str) -> list[str]:
    """LOB DB 가 실제로 있는 날짜만. 없는 날은 엔진이 조용히 건너뛴다."""
    dates = sorted(p.stem.replace("_LOB", "") for p in SEC_PATH.glob("*_LOB.db"))
    return [d for d in dates if str(start) <= d <= str(end)]


def run_backtest(
    source: str,
    dates: Sequence[str],
    codes: Optional[Sequence[str]],
    runs_root: Path,
    verbose: bool,
) -> tuple[list[Trade], float]:
    """백테스트 1회 실행 -> (거래 목록, 소요 초)."""
    engine = BackTestEngine(
        part=1, split=1, runs_root=runs_root, codes=codes,
        dates=dates, feature_source=source,
    )
    sink = io.StringIO()
    started = time.perf_counter()
    with contextlib.nullcontext() if verbose else contextlib.redirect_stdout(sink):
        engine.run()
    elapsed = time.perf_counter() - started
    return list(engine.trades), elapsed


# ---------------------------------------------------------------------------
# 대조
# ---------------------------------------------------------------------------

def trade_key(trade: Trade) -> tuple[str, str]:
    """이 엔진은 종목당 하루 1거래(state=1 이후 재진입 없음)라 (날짜, 종목)이 키다."""
    return (str(trade.date), trade.code)


def index_trades(trades: Sequence[Trade], label: str) -> tuple[dict, list[str]]:
    """거래 목록 -> 키 인덱스. 키가 겹치면 그 자체가 결함이므로 보고한다."""
    index: dict[tuple[str, str], Trade] = {}
    problems: list[str] = []
    for trade in trades:
        key = trade_key(trade)
        if key in index:
            problems.append(
                f"[{label}] 같은 (날짜, 종목) 에 거래가 2건: {key[0]} {key[1]} "
                f"— {index[key].entry_time} / {trade.entry_time}"
            )
        index[key] = trade
    return index, problems


def values_differ(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) > TOLERANCE
        except (TypeError, ValueError):
            return a != b
    return a != b


def compare_trade(left: Trade, right: Trade) -> list[str]:
    """거래 1건의 필드별 차이. 빈 리스트면 완전 일치."""
    diffs: list[str] = []
    for name in COMPARED_FIELDS:
        a, b = getattr(left, name), getattr(right, name)
        if values_differ(a, b):
            diffs.append(f"      {name:<14} 인라인={a!r}  스토어={b!r}")

    for name in COMPARED_META:
        a = left.signal_meta.get(name)
        b = right.signal_meta.get(name)
        if values_differ(a, b):
            diffs.append(f"      signal_meta.{name:<10} 인라인={a!r}  스토어={b!r}")
    return diffs


def report_parity(inline: Sequence[Trade], store: Sequence[Trade]) -> bool:
    """전체 대조 결과를 출력하고 통과 여부를 돌려준다."""
    left, left_problems = index_trades(inline, "인라인")
    right, right_problems = index_trades(store, "스토어")

    only_inline = sorted(set(left) - set(right))
    only_store = sorted(set(right) - set(left))
    common = sorted(set(left) & set(right))

    mismatched: list[tuple[tuple[str, str], list[str]]] = []
    for key in common:
        diffs = compare_trade(left[key], right[key])
        if diffs:
            mismatched.append((key, diffs))

    print()
    print("=" * 72)
    print("거래 목록 대조 (인라인 계산 vs 피처 스토어)")
    print("=" * 72)
    print(f"  인라인 거래 {len(inline)}건 / 스토어 거래 {len(store)}건")
    print(f"  공통 키 {len(common)}건 · 인라인에만 {len(only_inline)}건 · 스토어에만 {len(only_store)}건")

    for problem in left_problems + right_problems:
        print(f"  ⚠️ {problem}")

    if only_inline:
        print("\n  ❌ 인라인에만 있는 거래 (스토어 경로가 진입하지 못함)")
        for date, code in only_inline:
            t = left[(date, code)]
            print(f"      {date} {code} {t.name} 진입 {t.entry_time}@{t.entry_price} "
                  f"청산 {t.exit_time}@{t.exit_price} PnL {t.net_pnl_pct:+.3f}% ({t.exit_reason})")

    if only_store:
        print("\n  ❌ 스토어에만 있는 거래 (인라인 경로가 진입하지 못함)")
        for date, code in only_store:
            t = right[(date, code)]
            print(f"      {date} {code} {t.name} 진입 {t.entry_time}@{t.entry_price} "
                  f"청산 {t.exit_time}@{t.exit_price} PnL {t.net_pnl_pct:+.3f}% ({t.exit_reason})")

    if mismatched:
        print(f"\n  ❌ 필드가 다른 거래 {len(mismatched)}건")
        for (date, code), diffs in mismatched:
            print(f"    {date} {code} {left[(date, code)].name}")
            for line in diffs:
                print(line)

    passed = not (only_inline or only_store or mismatched or left_problems or right_problems)
    if passed:
        pnl = sum(t.net_pnl_pct for t in inline)
        print(f"\n  ✅ 거래 {len(common)}건 전 필드 일치 (누적 PnL {pnl:+.3f}%)")
        for date, code in common:
            t = left[(date, code)]
            print(f"      {date} {code} {t.name:<8} {t.entry_time}@{t.entry_price:>10,.0f} → "
                  f"{t.exit_time}@{t.exit_price:>10,.0f}  {t.net_pnl_pct:+.3f}%  {t.exit_reason}")
    return passed


# ---------------------------------------------------------------------------
# 컬럼 선택 읽기 이득
# ---------------------------------------------------------------------------

def report_column_selection(dates: Sequence[str]) -> None:
    """
    전략이 읽는 4개 컬럼이 파일에서 차지하는 비중. Parquet 을 택한 근거가
    압축(실측 1.26배)이 아니라 컬럼 선택 읽기라는 §3.5 의 주장을 숫자로 확인한다.
    """
    store = FeatureStore()
    wanted = set(REQUIRED_FEATURES) | set(KEY_COLUMNS)
    total_bytes = read_bytes = 0
    total_cols = 0

    for date in dates:
        try:
            stats = store.file_stats(date)
        except FileNotFoundError:
            continue
        total_cols = stats["columns"]
        total_bytes += sum(stats["column_bytes"].values())
        read_bytes += sum(v for k, v in stats["column_bytes"].items() if k in wanted)

    if not total_bytes:
        return

    print()
    print("=" * 72)
    print("컬럼 선택 읽기 (§3.5)")
    print("=" * 72)
    print(f"  파일 전체 {total_cols}컬럼 {total_bytes / 1024 / 1024:.1f}MB")
    print(f"  실제 읽는 {len(wanted)}컬럼 {read_bytes / 1024 / 1024:.1f}MB "
          f"({read_bytes / total_bytes * 100:.1f}%) — {', '.join(sorted(wanted))}")
    print(f"  I/O 절감 {total_bytes / read_bytes:.1f}배")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="인라인 계산 vs 피처 스토어 백테스트 거래 목록 대조"
    )
    parser.add_argument("--start", default="20220425", help="시작일 YYYYMMDD")
    parser.add_argument("--end", default="20220504", help="종료일 YYYYMMDD")
    parser.add_argument("--codes", nargs="*", default=None,
                        help="대상 종목 (기본: 일봉 매트릭스의 전체 종목)")
    parser.add_argument("--repeat", type=int, default=1,
                        help="각 경로를 N회 반복 실행해 파라미터 스윕 비용을 흉내 낸다")
    parser.add_argument("--verbose", action="store_true", help="엔진 출력을 그대로 보여준다")
    args = parser.parse_args()

    dates = available_dates(args.start, args.end)
    if not dates:
        print(f"⚠️ {args.start}~{args.end} 구간에 LOB DB 가 없습니다 ({SEC_PATH})")
        return 1

    print(f"📅 대상 날짜 {len(dates)}일: {dates[0]} ~ {dates[-1]}")
    print(f"🎯 대상 종목: {', '.join(args.codes) if args.codes else '일봉 매트릭스 전체'}")
    print(f"🔁 각 경로 {args.repeat}회 실행")

    # 대조용 런은 runs/ 를 오염시키지 않는다 — 실제 연구 결과가 아니다
    runs_root = Path(tempfile.mkdtemp(prefix="parity_runs_"))
    try:
        inline_times: list[float] = []
        store_times: list[float] = []
        inline_trades: list[Trade] = []
        store_trades: list[Trade] = []

        for i in range(args.repeat):
            inline_trades, elapsed = run_backtest(
                FEATURE_SOURCE_INLINE, dates, args.codes, runs_root, args.verbose)
            inline_times.append(elapsed)
            print(f"   [{i + 1}/{args.repeat}] 인라인 {elapsed:6.2f}s · 거래 {len(inline_trades)}건")

            store_trades, elapsed = run_backtest(
                FEATURE_SOURCE_STORE, dates, args.codes, runs_root, args.verbose)
            store_times.append(elapsed)
            print(f"   [{i + 1}/{args.repeat}] 스토어 {elapsed:6.2f}s · 거래 {len(store_trades)}건")

        passed = report_parity(inline_trades, store_trades)

        inline_total, store_total = sum(inline_times), sum(store_times)
        print()
        print("=" * 72)
        print(f"속도 ({args.repeat}회 합계)")
        print("=" * 72)
        print(f"  인라인 {inline_total:7.2f}s  (1회 평균 {inline_total / args.repeat:.2f}s)")
        print(f"  스토어 {store_total:7.2f}s  (1회 평균 {store_total / args.repeat:.2f}s)")
        if store_total > 0:
            print(f"  배율   {inline_total / store_total:.2f}x")

        report_column_selection(dates)

        print()
        if passed:
            print("✅ 대조 통과 — 두 경로의 거래가 완전히 같다")
            return 0
        print("❌ 대조 실패 — 인라인 경로를 제거하면 안 된다")
        return 1
    finally:
        shutil.rmtree(runs_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
