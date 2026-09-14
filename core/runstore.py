"""
core/runstore.py — L5 런 스토어 (Phase A)

"결과는 파일이 아니라 런(run)이다."

대시보드는 오랫동안 results/cross_Today_1.csv 한 파일에 고정되어 있었다.
전략이 N개가 되는 순간 그 구조는 성립하지 않는다. 런 스토어가 그 고정을 끊었고,
dashboard/data_service.py 는 이제 파일 경로가 아니라 이 스토어에 질의한다 (§7.5).

    runs/
    └── <run_id>/
        ├── manifest.json     실행 메타데이터 + 요약 지표 (재현성의 근거)
        └── trades.parquet    표준 Trade 레코드

run_id = hash(strategy_version + param_hash + feature_set_version + date_range + git_sha)
같은 입력이면 같은 run_id 가 나온다. 결과가 달라졌는데 run_id 가 같다면
어딘가에 비결정성이 있다는 뜻이고, 그것 자체가 잡아야 할 버그다.

참고: ARCHITECTURE_V2.md §6
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import warnings
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import pandas as pd

from core.contracts import TRADE_COLUMNS, Trade
from core.metrics import max_drawdown_pct, summary_metrics

__all__ = [
    "RunManifest",
    "RunStore",
    "make_run_id",
    "hash_params",
    "current_git_sha",
    "summarize",
    "trades_to_frame",
]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs"

MANIFEST_NAME = "manifest.json"
TRADES_NAME = "trades.parquet"


# ---------------------------------------------------------------------------
# 매니페스트
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RunManifest:
    """한 번의 실행을 완전히 재현하는 데 필요한 모든 것."""

    run_id: str
    strategy_id: str
    strategy_version: str
    param_variant: str
    param_hash: str
    feature_set_version: str
    engine: str                             # "tick" | "bar"
    mode: str                               # "backtest" | "paper" | "live"
    date_range: tuple[str, str]             # ("20260901", "20260911")

    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    universe_size: int = 0                  # 실제로 처리된 **종목 수** (종목일 수가 아니다)
    git_sha: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    metrics: Mapping[str, Any] = field(default_factory=dict)

    # 🆕 Phase B-4 — 선언된 유니버스 대비 실제 처리 결과. universe_size 만으로는
    # 그 숫자가 의도한 값인지 알 수 없다 (ARCHITECTURE_V2.md §3.6.1 추가 발견).
    # params["universe"] 가 '무엇을 선언했나'라면 이 필드는 '무엇이 실제로 됐나'다.
    universe: Mapping[str, Any] = field(default_factory=dict)

    notes: str = ""

    # 실제 저장 디렉토리 이름. 보통 run_id 와 같지만, 같은 run_id 를 다시 저장하면
    # 기존 결과를 덮어쓰지 않고 <run_id>__2 로 분기되므로 그때 달라진다.
    # (재현성 검증 = 같은 run_id 의 두 디렉토리를 비교하는 것)
    storage_key: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, default=str)

    @classmethod
    def from_json(cls, text: str) -> "RunManifest":
        raw = json.loads(text)
        raw["date_range"] = tuple(raw["date_range"])
        # 모르는 키는 무시한다 (앞으로 필드가 늘어도 과거 매니페스트 읽기가 깨지지 않게).
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})


# ---------------------------------------------------------------------------
# 식별자 생성
# ---------------------------------------------------------------------------

def _stable_hash(payload: Any, length: int = 8) -> str:
    """딕셔너리 순서에 무관한 안정 해시 (sort_keys 로 결정성 보장)."""
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def make_run_id(
    *,
    strategy_id: str,
    strategy_version: str,
    param_hash: str,
    feature_set_version: str,
    date_range: Sequence[str],
    git_sha: str = "",
) -> str:
    """동일 입력 -> 동일 run_id. 재현성 검증의 기준점."""
    return _stable_hash({
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "param_hash": param_hash,
        "feature_set_version": feature_set_version,
        "date_range": list(date_range),
        "git_sha": git_sha,
    })


def hash_params(params: Mapping[str, Any]) -> str:
    """파라미터 딕셔너리의 안정 해시."""
    return _stable_hash(params)


def current_git_sha(short: bool = True) -> str:
    """현재 커밋 해시. git 저장소가 아니면 빈 문자열."""
    try:
        args = ["git", "rev-parse"] + (["--short"] if short else []) + ["HEAD"]
        out = subprocess.run(
            args, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=5, check=True
        )
        return out.stdout.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 직렬화
# ---------------------------------------------------------------------------

def _dump_meta(meta: Any) -> str:
    """
    signal_meta -> JSON 문자열.

    parquet 에 중첩 타입(struct/map)으로 넣지 않는 이유:
      - 엔진마다 키 집합이 다르다. struct 로 넣으면 런마다 스키마가 달라져
        여러 런의 trades.parquet 을 concat 할 때 깨진다.
      - 문자열 1컬럼이면 스키마가 고정되고, 필요할 때만 파싱하면 된다.
    """
    if meta is None:
        return "{}"
    if isinstance(meta, str):
        return meta
    return json.dumps(meta, ensure_ascii=False, sort_keys=True, default=str)


def trades_to_frame(trades: Iterable[Trade], *, serialize_meta: bool = True) -> pd.DataFrame:
    """Trade 목록 -> TRADE_COLUMNS 순서의 DataFrame."""
    rows = []
    for t in trades:
        rows.append({
            "run_id": t.run_id,
            "strategy_id": t.strategy_id,
            "strategy_version": t.strategy_version,
            "exit_rule": t.exit_rule,
            "code": t.code,
            "name": t.name,
            "date": t.date,
            "entry_time": str(t.entry_time),
            "entry_price": float(t.entry_price),
            "exit_time": str(t.exit_time),
            "exit_price": float(t.exit_price),
            "qty": int(t.qty),
            "gross_pnl_pct": float(t.gross_pnl_pct),
            "fee_pct": float(t.fee_pct),
            "net_pnl_pct": float(t.net_pnl_pct),
            "mae_pct": float(t.mae_pct),
            "mfe_pct": float(t.mfe_pct),
            "holding_sec": int(t.holding_sec),
            "exit_reason": str(t.exit_reason),
            "signal_meta": _dump_meta(t.signal_meta) if serialize_meta else t.signal_meta,
        })

    if not rows:
        return pd.DataFrame(columns=list(TRADE_COLUMNS))
    return pd.DataFrame(rows)[list(TRADE_COLUMNS)]


# ---------------------------------------------------------------------------
# 스토어
# ---------------------------------------------------------------------------

class RunStore:
    """
    런 저장/조회. 대시보드는 파일 경로가 아니라 이 객체에 질의한다.

        store = RunStore()
        run_id = store.save(manifest, trades)

        runs = store.query(strategy_id="nxt_breakout", since="20260801")
        df   = store.load_trades([r.run_id for r in runs])
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else DEFAULT_RUNS_DIR
        self.root.mkdir(parents=True, exist_ok=True)

    # -- 경로 ---------------------------------------------------------------

    def run_dir(self, storage_key: str) -> Path:
        return self.root / storage_key

    def _storage_keys_for(self, run_id: str, *, include_reruns: bool = False) -> list[str]:
        """
        run_id 또는 저장 키에 해당하는 디렉토리들.

        정확히 일치하는 디렉토리가 있으면 그것만 돌려준다. 재실행분(__2, __3)까지
        묶어서 돌려주면 같은 거래가 두 번 세어져 건수와 손익 합계가 소리 없이
        부풀어 오른다. 재실행분 비교는 의도를 밝혀야만(include_reruns) 열린다.
        """
        exact = [run_id] if (self.root / run_id / MANIFEST_NAME).exists() else []
        if exact and not include_reruns:
            return exact
        reruns = sorted(
            d.name for d in self.root.glob(run_id + "__*") if (d / MANIFEST_NAME).exists()
        )
        return exact + reruns

    def rerun_keys(self, run_id: str) -> list[str]:
        """
        같은 run_id 로 저장된 모든 실행분의 저장 키.

        길이가 2 이상이면 같은 입력을 두 번 이상 돌렸다는 뜻이다. 각각을
        load_trades 로 읽어 비교하면 비결정성 유무를 알 수 있다 (§6.3).
        """
        return self._storage_keys_for(run_id, include_reruns=True)

    def _next_free_key(self, run_id: str) -> str:
        seq = 2
        while (self.root / (run_id + "__" + str(seq))).exists():
            seq += 1
        return run_id + "__" + str(seq)

    # -- 쓰기 ---------------------------------------------------------------

    def save(
        self,
        manifest: RunManifest,
        trades: Iterable[Trade],
        *,
        on_exists: str = "suffix",
    ) -> str:
        """
        manifest.json + trades.parquet 기록. 실제 저장 키(디렉토리 이름)를 반환한다.

        on_exists — 같은 run_id 가 이미 있을 때의 동작:
          suffix    (기본) <run_id>__2 에 나란히 저장한다. 기존 결과를 보존하면서
                    같은 입력이 같은 결과를 내는지 사후 비교할 수 있다.
          skip      경고만 하고 쓰지 않는다.
          error     FileExistsError 를 낸다.
          overwrite 덮어쓴다. 재현성 검증 기회를 버리는 선택이라 기본값이 아니다.

        metrics 가 비어 있으면 summarize(trades) 결과를 채운다. 매니페스트만 열어도
        요약 지표가 보이게 하기 위함이다.
        """
        trades = list(trades)
        key = manifest.run_id

        if (self.root / key).exists():
            if on_exists == "error":
                raise FileExistsError("이미 존재하는 런입니다: " + str(self.root / key))
            if on_exists == "skip":
                warnings.warn("런 " + key + " 가 이미 존재하여 저장을 건너뜁니다.", stacklevel=2)
                return key
            if on_exists == "suffix":
                key = self._next_free_key(manifest.run_id)
                warnings.warn(
                    "런 " + manifest.run_id + " 가 이미 존재합니다. 덮어쓰지 않고 "
                    + key + " 로 저장합니다. (같은 입력이 같은 결과를 냈는지 비교해 보세요)",
                    stacklevel=2,
                )
            elif on_exists != "overwrite":
                raise ValueError("알 수 없는 on_exists 값: " + str(on_exists))

        if not manifest.metrics:
            manifest.metrics = summarize(trades)
        manifest.storage_key = key

        target = self.root / key
        target.mkdir(parents=True, exist_ok=True)

        frame = trades_to_frame(trades)
        frame.to_parquet(target / TRADES_NAME, engine="pyarrow", compression="zstd", index=False)
        (target / MANIFEST_NAME).write_text(manifest.to_json(), encoding="utf-8")
        return key

    # -- 읽기 ---------------------------------------------------------------

    def query(
        self,
        *,
        strategy_id: Optional[str] = None,
        mode: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> list[RunManifest]:
        """
        runs/*/manifest.json 을 훑어 조건에 맞는 런 목록 반환 (created_at 오름차순).

        since / until 은 런의 date_range 와 겹치는지로 판정한다. "8월 이후 데이터를
        다룬 런"을 찾는 것이지 "8월 이후에 실행한 런"을 찾는 게 아니다.

        런 수가 수천 개를 넘어가면 manifest 를 SQLite 인덱스로 옮긴다.
        그 전까지는 디렉토리 스캔으로 충분하다 (조기 최적화 금지).
        """
        found: list[RunManifest] = []
        for path in sorted(self.root.glob("*/" + MANIFEST_NAME)):
            try:
                manifest = RunManifest.from_json(path.read_text(encoding="utf-8"))
            except Exception as exc:      # 손상된 매니페스트는 건너뛰되 조용히 넘기지 않는다
                warnings.warn("매니페스트를 읽지 못했습니다: " + str(path) + " (" + str(exc) + ")", stacklevel=2)
                continue

            if not manifest.storage_key:
                manifest.storage_key = path.parent.name
            if strategy_id and manifest.strategy_id != strategy_id:
                continue
            if mode and manifest.mode != mode:
                continue

            start = manifest.date_range[0] if manifest.date_range else ""
            end = manifest.date_range[1] if len(manifest.date_range) > 1 else ""
            if since and end and str(end) < str(since):
                continue
            if until and start and str(start) > str(until):
                continue
            found.append(manifest)

        found.sort(key=lambda m: (m.created_at, m.storage_key))
        return found

    def load_trades(
        self,
        run_ids: Sequence[str] | str,
        *,
        parse_signal_meta: bool = False,
        include_reruns: bool = False,
    ) -> pd.DataFrame:
        """
        여러 런의 trades.parquet 을 concat 한 DataFrame 반환.

        run_id 컬럼이 이미 들어 있으므로 그대로 groupby 비교가 가능하다.
        이것이 전략 N개 비교를 가능하게 하는 지점이다. exit_rule 컬럼까지 쓰면
        한 런 안의 3대 청산 컷 비교도 같은 방식으로 된다.

        기본적으로 **하나의 키 = 하나의 실행분**을 읽는다. 같은 입력을 두 번 돌려
        생긴 재실행분(<run_id>__2)까지 한꺼번에 읽으면 같은 거래가 두 번 세어지므로,
        그건 include_reruns=True 로 명시할 때만 일어난다 (재현성 비교용).
        """
        if isinstance(run_ids, str):
            run_ids = [run_ids]

        frames: list[pd.DataFrame] = []
        for rid in run_ids:
            keys = self._storage_keys_for(rid, include_reruns=include_reruns)
            if not keys:
                warnings.warn("런을 찾을 수 없습니다: " + str(rid), stacklevel=2)
                continue
            for key in keys:
                path = self.root / key / TRADES_NAME
                if not path.exists():
                    warnings.warn("거래 파일이 없습니다: " + str(path), stacklevel=2)
                    continue
                frame = pd.read_parquet(path, engine="pyarrow")
                frame.insert(0, "storage_key", key)
                frames.append(frame)

        if not frames:
            return pd.DataFrame(columns=["storage_key", *TRADE_COLUMNS])

        merged = pd.concat(frames, ignore_index=True)
        if parse_signal_meta:
            merged["signal_meta"] = merged["signal_meta"].map(
                lambda s: json.loads(s) if isinstance(s, str) and s else {}
            )
        return merged

    def load_manifest(self, run_id: str) -> RunManifest:
        keys = self._storage_keys_for(run_id)
        key = keys[0] if keys else run_id
        path = self.root / key / MANIFEST_NAME
        manifest = RunManifest.from_json(path.read_text(encoding="utf-8"))
        if not manifest.storage_key:
            manifest.storage_key = key
        return manifest


# ---------------------------------------------------------------------------
# 요약 지표
# ---------------------------------------------------------------------------

def _clean(value: Any) -> Any:
    """NaN/Inf 는 JSON 표준이 아니다 -> None 으로 저장한다."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _summary_block(frame: pd.DataFrame) -> dict[str, Any]:
    """표준 Trade 컬럼 기준 요약 한 덩어리 (전체 / exit_rule 별 공용)."""
    base = summary_metrics(frame)          # core.metrics — 대시보드와 같은 계산식
    return {
        "trades": base["trades"],
        "win_rate": _clean(base["win_rate"]),
        "avg_pnl": _clean(base["avg_pnl"]),
        "net_pnl_sum": _clean(base["total_pnl"]),
        "profit_factor": _clean(base["profit_factor"]),
        "avg_mae_pct": _clean(base["avg_mae_pct"]),
        "avg_mfe_pct": _clean(base["avg_mfe_pct"]),
        "avg_holding_sec": _clean(base["avg_holding_seconds"]),
        "tpi": _clean(base["tpi"]),
        "mdd_pct": _clean(max_drawdown_pct(frame["net_pnl_pct"]) if not frame.empty else 0.0),
    }


def summarize(trades: Sequence[Trade]) -> dict[str, Any]:
    """
    매니페스트에 박아 넣을 요약 지표.

    계산식은 core/metrics.py 에 있고, 그건 dashboard/metrics.py 에서 옮겨온 것이다.
    같은 거래 목록에 대해 대시보드와 매니페스트가 다른 숫자를 내면 안 된다
    (tests/test_runstore.py::test_summarize_matches_dashboard_metrics 가 검증한다).

    반환 키:
        trades, win_rate(%), avg_pnl(%), net_pnl_sum(%), profit_factor,
        avg_mae_pct, avg_mfe_pct, avg_holding_sec, tpi, mdd_pct,
        days, trades_per_day, by_exit_rule{청산규칙 -> 같은 형태의 요약}
    """
    frame = trades_to_frame(trades, serialize_meta=False)
    out = _summary_block(frame)

    days = int(frame["date"].nunique()) if not frame.empty else 0
    out["days"] = days
    out["trades_per_day"] = _clean(round(len(frame) / days, 3)) if days else 0.0

    # 3대 청산 컷 비교: 한 런 안에 여러 exit_rule 이 섞여 들어온다.
    if frame.empty:
        out["by_exit_rule"] = {}
    else:
        out["by_exit_rule"] = {
            str(rule): _summary_block(group)
            for rule, group in frame.groupby("exit_rule", dropna=False)
        }

    return out
