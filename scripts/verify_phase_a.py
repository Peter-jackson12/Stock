"""
scripts/verify_phase_a.py — Phase A 수용 검증 (실데이터)

tests/ 의 단위 테스트는 합성 데이터로 계약을 검증한다. 이 스크립트는 **실제
sampledata 로 두 엔진을 돌려** 다음 세 가지를 확인한다.

  1) 재현성   같은 입력으로 두 번 돌리면 run_id 가 같은가
              (나아가 두 번의 거래 목록이 완전히 같은가 — 비결정성 탐지)
  2) 등가성   레거시 CSV 와 새 trades.parquet 의 거래 건수 / PnL 합계가 같은가
  3) 저장     지금까지 stdout 에만 찍던 nxt 틱 엔진이 결과를 남기는가

실행:
    uv run python scripts/verify_phase_a.py
    uv run python scripts/verify_phase_a.py --code 005930

이 스크립트는 results/ 의 기존 CSV 를 건드리지 않는다. 레거시 출력은 임시
디렉토리로 돌려 비교만 하고, 기존 결과 파일은 그대로 둔다.
**이 검증이 통과하기 전에는 레거시 CSV 경로를 지우지 않는다.**

참고: ARCHITECTURE_V2.md §6, §8 (Phase A)
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import engine.engine as bar_engine_module                       # noqa: E402
from core.runstore import RunStore                              # noqa: E402
from engine.config import ENCODING, RESULT_DIR, STRATEGY_NAME   # noqa: E402
from engine.engine import BackTestEngine                        # noqa: E402
from engine.nxt_tick_engine import NextradeTickEngine           # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))
    return ok


def _trades_only(df: pd.DataFrame) -> pd.DataFrame:
    """저장 디렉토리 이름(storage_key)은 비교 대상이 아니다."""
    return df.drop(columns=["storage_key"], errors="ignore").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 1. 1초봉 엔진 (engine/engine.py) — 레거시 CSV vs 런 스토어
# ---------------------------------------------------------------------------

#: 검증용 대상 종목. engine.py 의 하드코딩 필터(§1.6)가 제거되면서 기본값이
#: "전체 종목"이 되었으므로, 이 스크립트의 빠른 회귀 검증 범위를 유지하려면
#: 명시적으로 지정해야 한다.
VERIFY_CODES = ("000270",)


def verify_bar_engine(runs_root: Path, tmp_results: Path) -> None:
    print("\n[1] 1초봉 엔진 (engine/engine.py)")

    # 레거시 CSV 를 임시 디렉토리로 돌린다 — results/ 의 기존 파일을 보호하기 위함
    original_result_dir = bar_engine_module.RESULT_DIR
    bar_engine_module.RESULT_DIR = tmp_results
    try:
        first = BackTestEngine(part=1, split=12, runs_root=runs_root, codes=VERIFY_CODES)
        first.run()
        csv_path = tmp_results / f"{STRATEGY_NAME}_1.csv"
        csv_df = pd.read_csv(csv_path, encoding=ENCODING)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")     # 두 번째 저장은 __2 로 분기된다(정상)
            second = BackTestEngine(part=1, split=12, runs_root=runs_root, codes=VERIFY_CODES)
            second.run()
    finally:
        bar_engine_module.RESULT_DIR = original_result_dir

    check(
        "재현성: 두 번 실행의 run_id 가 같다",
        first.run_id == second.run_id,
        f"{first.run_id} vs {second.run_id}",
    )

    store = RunStore(runs_root)
    run_one = _trades_only(store.load_trades(first.storage_key))
    run_two = _trades_only(store.load_trades(second.storage_key))
    check(
        "재현성: 두 번 실행의 거래 목록이 완전히 같다",
        run_one.equals(run_two),
        f"{len(run_one)}건 vs {len(run_two)}건",
    )

    csv_count, parquet_count = len(csv_df), len(run_one)
    check("등가성: 거래 건수 일치", csv_count == parquet_count, f"CSV {csv_count}건 / parquet {parquet_count}건")

    csv_pnl = float(pd.to_numeric(csv_df["pnl"], errors="coerce").fillna(0).sum())
    parquet_pnl = float(run_one["net_pnl_pct"].sum())
    check(
        "등가성: PnL 합계 일치",
        abs(csv_pnl - parquet_pnl) < 1e-9,
        f"CSV {csv_pnl:+.6f}% / parquet {parquet_pnl:+.6f}%",
    )

    if csv_count == parquet_count and csv_count:
        same_prices = (
            csv_df["entry_price"].astype(float).tolist() == run_one["entry_price"].tolist()
            and csv_df["exit_price"].astype(float).tolist() == run_one["exit_price"].tolist()
        )
        check("등가성: 진입/청산 가격이 행 단위로 일치", same_prices)

        same_reason = csv_df["msg"].astype(str).tolist() == run_one["exit_reason"].tolist()
        check("등가성: 청산 사유(msg -> exit_reason) 일치", same_reason)

        same_mae = csv_df["mdd"].astype(float).tolist() == run_one["mae_pct"].tolist()
        same_mfe = csv_df["mdu"].astype(float).tolist() == run_one["mfe_pct"].tolist()
        check("등가성: mdd -> mae_pct / mdu -> mfe_pct 일치", same_mae and same_mfe)

        # 상수로 박혀 있던 값은 Trade 필드가 아니라 signal_meta 안에 있어야 한다
        meta = store.load_trades(first.storage_key, parse_signal_meta=True)["signal_meta"].iloc[0]
        check(
            "격리: mkt_float/ytd_tradamt/cum_amt 가 signal_meta.placeholders 에 있다",
            set(meta.get("placeholders", {})) == {"mkt_float", "ytd_tradamt", "cum_amt"},
            str(meta.get("placeholders")),
        )

    # 레거시 경로가 살아 있는지 (검증 전에 지우지 않는다)
    legacy_csv = RESULT_DIR / f"{STRATEGY_NAME}_1.csv"
    check("병행 출력: 레거시 CSV 경로가 그대로 살아 있다", legacy_csv.exists(), str(legacy_csv))

    if legacy_csv.exists():
        committed = pd.read_csv(legacy_csv, encoding=ENCODING)
        same_as_committed = (
            len(committed) == csv_count
            and abs(float(pd.to_numeric(committed["pnl"], errors="coerce").fillna(0).sum()) - csv_pnl) < 1e-9
        )
        check(
            "회귀: 이번 실행의 CSV 가 기존 results/ CSV 와 같다",
            same_as_committed,
            f"기존 {len(committed)}건 / 이번 {csv_count}건",
        )


# ---------------------------------------------------------------------------
# 2. 틱 엔진 (engine/nxt_tick_engine.py) — 이제 결과를 남긴다
# ---------------------------------------------------------------------------

def verify_tick_engine(runs_root: Path, date_str: str, code: str) -> None:
    print(f"\n[2] 틱 엔진 (engine/nxt_tick_engine.py) — {date_str} {code}")

    first = NextradeTickEngine(date_str=date_str, target_code=code, runs_root=runs_root)
    key_one = first.run_strategy(latency_sec=1, cooldown_sec=10)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        second = NextradeTickEngine(date_str=date_str, target_code=code, runs_root=runs_root)
        key_two = second.run_strategy(latency_sec=1, cooldown_sec=10)

    if key_one is None or key_two is None:
        check("틱 엔진 실행", False, "해당 종목의 체결 데이터가 없습니다")
        return

    check(
        "재현성: 두 번 실행의 run_id 가 같다",
        first.run_id == second.run_id,
        f"{first.run_id} vs {second.run_id}",
    )

    store = RunStore(runs_root)
    run_one = _trades_only(store.load_trades(key_one))
    run_two = _trades_only(store.load_trades(key_two))
    check("재현성: 두 번 실행의 거래 목록이 완전히 같다", run_one.equals(run_two), f"{len(run_one)}건")

    check("저장: 이제 결과가 남는다 (이전에는 stdout 뿐)", len(run_one) > 0, f"{len(run_one)}건")

    rules = set(run_one["exit_rule"]) if len(run_one) else set()
    check(
        "비교축: 3대 청산 컷이 exit_rule 로 구분된다",
        rules.issubset({"fixed", "tick_trail", "step_trail"}) and len(rules) > 0,
        ", ".join(sorted(rules)) or "없음",
    )

    # 화면 성적표와 저장된 숫자가 같은가 — 매니페스트의 by_exit_rule 과 대조
    manifest = store.load_manifest(first.run_id)
    by_rule = manifest.metrics.get("by_exit_rule", {})
    from_trades = run_one.groupby("exit_rule")["net_pnl_pct"].sum().round(6).to_dict()
    from_manifest = {k: round(v["net_pnl_sum"], 6) for k, v in by_rule.items()}
    check("일관성: 매니페스트 요약과 거래 합계가 같다", from_trades == from_manifest, str(from_manifest))

    # 두 엔진의 결과가 한 스키마 위에 놓였는가
    both = store.load_trades([k for k in (key_one,) ] + _bar_keys(store))
    engines = both.groupby("strategy_id")["net_pnl_pct"].agg(["count", "sum"])
    check(
        "통합: 두 엔진 결과를 한 DataFrame 에서 나란히 비교할 수 있다",
        len(engines) >= 2,
        " | ".join(f"{idx}: {int(r['count'])}건 {r['sum']:+.3f}%" for idx, r in engines.iterrows()),
    )


def _bar_keys(store: RunStore) -> list[str]:
    return [m.storage_key for m in store.query(strategy_id=STRATEGY_NAME)][:1]


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Phase A (런 스토어) 수용 검증")
    parser.add_argument("--code", default="053260", help="틱 엔진 검증 대상 종목")
    parser.add_argument("--date", default="20260911", help="틱 엔진 검증 대상 날짜")
    parser.add_argument("--keep", action="store_true", help="검증용 임시 런 디렉토리를 남긴다")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="phase_a_verify_"))
    runs_root = workdir / "runs"
    tmp_results = workdir / "results"
    tmp_results.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("Phase A 검증 — 런 스토어 + 표준 Trade 스키마")
    print(f"검증용 임시 디렉토리: {workdir}")
    print("=" * 72)

    try:
        verify_bar_engine(runs_root, tmp_results)
        verify_tick_engine(runs_root, args.date, args.code)
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [name for name, ok, _ in RESULTS if not ok]

    print("\n" + "=" * 72)
    print(f"검증 결과: {passed}/{len(RESULTS)} 통과")
    if failed:
        print("실패 항목:")
        for name in failed:
            print(f"  - {name}")
        print("\n⚠️ 검증이 통과하지 않았습니다. 레거시 CSV 경로를 제거하지 마세요.")
        return 1

    print("🎉 전부 통과. 레거시 CSV 와 런 스토어가 같은 거래를 담고 있습니다.")
    print("   (CSV 제거는 대시보드를 런 스토어 기반으로 옮긴 뒤에 결정하세요 — §7.5)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
