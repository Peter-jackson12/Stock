"""
core/universe.py — 백테스트 유니버스의 단일 소스 (Phase B-4)

ARCHITECTURE_V2.md §3.6.1 '추가 발견' 이 지목한 문제:

    engine/engine.py 는 날짜도 종목도 open.csv 에서 뽑는다. 즉 일봉 매트릭스가
    거시 '필터'의 입력이 아니라 **종목이 백테스트에 보이느냐를 결정하는 게이트**다.
    그 결과 유니버스 정의가 세 계층에서 갈린다 — LOB 1,300~1,500 / fs_v1 200 / 엔진 3.

이 파일은 그 게이트를 **선언**으로 바꾼다. 무엇이 후보였는지를 먼저 적어두고,
실제로 몇 개가 처리됐는지 대조한다. 핵심은 유니버스를 넓히는 게 아니라
**좁아지는 것을 보이게 만드는 것**이다. 지금까지는 종목이 조용히 사라졌고
매니페스트의 universe_size 는 그냥 작은 숫자를 보고할 뿐이었다.

왜 collector/universe.py 가 아니라 여기인가:
    collector/universe.py 는 **실시간 수집** 대상 20종목이다(한투 웹소켓 세션당
    권장 20~40종목이라는 제약에서 나온 목록). 백테스트 유니버스와는 제약도
    수명도 다르다. 게다가 engine 이 collector 를 import 하는 의존 방향은
    거꾸로다 — core/ 는 이미 두 엔진과 대시보드가 함께 쓰는 계층이다.

참고: ARCHITECTURE_V2.md §3.6.1, §6.3
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import yaml

__all__ = [
    "UniverseSpec",
    "UniverseReconciliation",
    "DEFAULT_UNIVERSE_PATH",
    "MISSING_THRESHOLD_PCT",
    "load_universe",
    "resolve_codes",
    "reconcile",
]

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UNIVERSE_PATH = PROJECT_ROOT / "universe.yaml"

#: 선언 대비 미처리 비율 기본 임계치 (%). 초과 시 경고, strict 면 실패.
MISSING_THRESHOLD_PCT = 5.0

#: 규칙 기반 선언이 쓰는 시장 분류 원천. 0행 종목명 / 1행 시장구분.
MARKET_KEY_FILE = "key.csv"

#: 지원하는 선언 방식
SOURCES = ("explicit", "rule", "daily_matrix", "feature_store", "lob")


@dataclass(frozen=True, slots=True)
class UniverseSpec:
    """universe.yaml 의 항목 하나."""

    name: str
    source: str
    description: str = ""
    codes: tuple[str, ...] = ()                 # source="explicit"
    market: tuple[str, ...] = ()                # source="rule"
    common_only: bool = True                    # source="rule"
    feature_set: str = "fs_v1"                  # source="feature_store"

    def as_params(self) -> dict:
        """
        run_id 에 들어갈 표현. **선언**만 담고 해석 결과(종목 수)는 담지 않는다.
        같은 선언이면 같은 런이어야 하는데, 해석 결과는 데이터가 채워지는 대로
        변하기 때문이다.
        """
        params: dict[str, Any] = {"name": self.name, "source": self.source}
        if self.source == "explicit":
            params["codes"] = list(self.codes)
        elif self.source == "rule":
            params["market"] = list(self.market)
            params["common_only"] = self.common_only
        elif self.source == "feature_store":
            params["feature_set"] = self.feature_set
        return params


@dataclass(frozen=True, slots=True)
class UniverseReconciliation:
    """
    선언된 유니버스 vs 실제 처리된 종목.

    빠진 종목을 **사유별로** 센다. 총 개수만 보면 "원래 그 정도인가 보다" 하고
    넘어가지만, no_daily 가 197 이라고 적혀 있으면 일봉 수집을 보게 된다.
    """

    declared: int
    processed: int
    missing: Mapping[str, int] = field(default_factory=dict)
    missing_codes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def missing_pct(self) -> float:
        if not self.declared:
            return 0.0
        return (self.declared - self.processed) / self.declared * 100.0

    def within(self, threshold_pct: float) -> bool:
        return self.missing_pct <= threshold_pct

    def as_manifest(self, *, sample: int = 10) -> dict:
        """매니페스트에 남길 형태. 종목 코드는 진단용으로 앞 몇 개만."""
        return {
            "declared": self.declared,
            "processed": self.processed,
            "missing": dict(self.missing),
            "missing_pct": round(self.missing_pct, 2),
            "missing_sample": {
                reason: list(codes[:sample])
                for reason, codes in self.missing_codes.items() if codes
            },
        }

    def describe(self) -> str:
        if not self.missing:
            return f"유니버스 {self.declared}종목 전부 처리"
        detail = " · ".join(f"{k} {v}" for k, v in sorted(self.missing.items()))
        return (
            f"선언 {self.declared} → 처리 {self.processed} "
            f"(미처리 {self.missing_pct:.1f}% — {detail})"
        )


# ---------------------------------------------------------------------------
# 선언 로딩
# ---------------------------------------------------------------------------

def load_universe(name: Optional[str] = None, path: Path | str | None = None) -> UniverseSpec:
    """
    universe.yaml 에서 선언 하나를 읽는다. name 이 없으면 파일의 default 를 쓴다.

    이름이 틀리면 사용 가능한 목록과 함께 에러를 낸다 — 조용히 기본값으로
    떨어지면 "내가 선언한 유니버스로 돌고 있다"는 착각이 생긴다.
    """
    path = Path(path) if path else DEFAULT_UNIVERSE_PATH
    if not path.exists():
        raise FileNotFoundError(f"유니버스 선언 파일이 없습니다: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("universes") or {}
    if not entries:
        raise ValueError(f"{path}: universes 항목이 비어 있습니다")

    key = name or raw.get("default")
    if not key:
        raise ValueError(f"{path}: default 가 없어 유니버스를 고를 수 없습니다")
    if key not in entries:
        raise KeyError(f"유니버스 선언 없음: {key} (가능: {sorted(entries)})")

    entry = entries[key] or {}
    source = entry.get("source")
    if source not in SOURCES:
        raise ValueError(f"{key}: source 는 {SOURCES} 중 하나여야 합니다 — {source!r}")

    codes = tuple(str(c).zfill(6) for c in (entry.get("codes") or ()))
    if source == "explicit" and not codes:
        raise ValueError(f"{key}: source=explicit 인데 codes 가 비어 있습니다")

    market = entry.get("market") or ()
    if isinstance(market, str):
        market = (market,)

    return UniverseSpec(
        name=key,
        source=source,
        description=str(entry.get("description", "")),
        codes=codes,
        market=tuple(str(m) for m in market),
        common_only=bool(entry.get("common_only", True)),
        feature_set=str(entry.get("feature_set", "fs_v1")),
    )


# ---------------------------------------------------------------------------
# 해석
# ---------------------------------------------------------------------------

def _codes_from_daily_matrix(daily_frame) -> set[str]:
    """일봉 매트릭스 컬럼(A000270 ...) -> 종목코드."""
    return {c[1:] if str(c).startswith("A") else str(c) for c in daily_frame.columns[1:]}


def _codes_from_market_key(csv_path: Path, markets: Sequence[str], common_only: bool) -> set[str]:
    """
    key.csv 로 규칙 기반 선언을 푼다. 0행 종목명, 1행 시장구분(KOSPI/KOSDAQ/외감).

    보통주 판별은 종목코드 끝자리로 한다 — 한국 시장에서 보통주는 0 으로 끝나고
    우선주는 5/7/9/K/L/M 이다. 별도 구분 컬럼이 원천에 없어 이 관행을 쓴다.
    """
    import pandas as pd

    path = Path(csv_path) / MARKET_KEY_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"규칙 기반 유니버스에는 시장 분류가 필요합니다: {path} 가 없습니다"
        )

    raw = pd.read_csv(path, encoding="CP949", low_memory=False)
    codes = [c[1:] if str(c).startswith("A") else str(c) for c in raw.columns[1:]]
    market_of = dict(zip(codes, (str(v) for v in raw.iloc[1, 1:])))

    wanted = set(markets)
    selected = {c for c in codes if market_of.get(c) in wanted}
    if common_only:
        selected = {c for c in selected if c.endswith("0")}
    return selected


def _codes_from_feature_store(feature_set: str, dates: Sequence[str]) -> set[str]:
    """피처 파일에 실제로 들어 있는 종목. 대상 날짜 중 첫 파일 기준."""
    from features.store import FeatureStore

    store = FeatureStore(version=feature_set)
    available = [d for d in store.available_dates() if not dates or d in set(dates)]
    if not available:
        raise FileNotFoundError(
            f"{feature_set}: 대상 날짜의 피처 파일이 없습니다 (dates={list(dates)[:3]}…)"
        )
    frame = store.read(available[0], names=[])
    return set(frame["code"].astype(str))


def _codes_from_lob(sec_path: Path, dates: Sequence[str]) -> set[str]:
    """대상 날짜의 LOB DB 에 테이블이 있는 종목 전체 (합집합)."""
    found: set[str] = set()
    for date in dates:
        db = Path(sec_path) / f"{date}_LOB.db"
        if not db.exists():
            continue
        conn = sqlite3.connect(db)
        try:
            found.update(
                name for (name,) in
                conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            )
        finally:
            conn.close()
    return found


def resolve_codes(
    spec: UniverseSpec,
    *,
    dates: Sequence[str] = (),
    daily_frame=None,
    csv_path: Path | str | None = None,
    sec_path: Path | str | None = None,
) -> tuple[str, ...]:
    """선언을 실제 종목코드 목록으로 푼다. 정렬해 돌려주므로 결과가 결정적이다."""
    from engine.config import CSV_PATH, SEC_PATH

    csv_path = Path(csv_path) if csv_path else CSV_PATH
    sec_path = Path(sec_path) if sec_path else SEC_PATH

    if spec.source == "explicit":
        codes = set(spec.codes)
    elif spec.source == "rule":
        codes = _codes_from_market_key(csv_path, spec.market, spec.common_only)
    elif spec.source == "daily_matrix":
        if daily_frame is None:
            raise ValueError("source=daily_matrix 에는 daily_frame 이 필요합니다")
        codes = _codes_from_daily_matrix(daily_frame)
    elif spec.source == "feature_store":
        codes = _codes_from_feature_store(spec.feature_set, dates)
    else:                                       # lob
        codes = _codes_from_lob(sec_path, dates)

    return tuple(sorted(codes))


# ---------------------------------------------------------------------------
# 대조
# ---------------------------------------------------------------------------

def reconcile(
    declared: Iterable[str],
    *,
    processed: Iterable[str],
    seen_in_lob: Iterable[str],
    seen_in_daily: Iterable[str],
    missing_features: Iterable[str] = (),
) -> UniverseReconciliation:
    """
    선언 대비 실제 처리 결과를 사유별로 집계한다.

    사유는 배타적으로 하나만 붙인다. 판정 순서는 데이터가 흐르는 순서 그대로다.

        no_lob        해당 기간 어느 날에도 LOB DB 에 테이블이 없다 (수집 안 됨)
        no_daily      LOB 은 있는데 일봉 매트릭스에 없다 (종목명·전일종가를 못 구한다)
        no_features   LOB·일봉은 있는데 피처 파일에 그 종목이 없다
        other         위 어디에도 해당하지 않는데 처리되지 않음 (원천 데이터 오류 등)
    """
    declared_set = set(declared)
    processed_set = set(processed) & declared_set
    lob_set = set(seen_in_lob)
    daily_set = set(seen_in_daily)
    feature_gap = set(missing_features)

    buckets: dict[str, list[str]] = {}
    for code in sorted(declared_set - processed_set):
        if code not in lob_set:
            reason = "no_lob"
        elif code not in daily_set:
            reason = "no_daily"
        elif code in feature_gap:
            reason = "no_features"
        else:
            reason = "other"
        buckets.setdefault(reason, []).append(code)

    return UniverseReconciliation(
        declared=len(declared_set),
        processed=len(processed_set),
        missing={k: len(v) for k, v in sorted(buckets.items())},
        missing_codes={k: tuple(v) for k, v in buckets.items()},
    )
