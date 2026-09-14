"""
core/runstore.py — L5 런 스토어 (Phase A)

"결과는 파일이 아니라 런(run)이다."

지금 dashboard/data_service.py 는 results/cross_Today_1.csv 한 파일에 고정되어 있다.
전략이 N개가 되는 순간 이 구조는 성립하지 않는다. 런 스토어는 그 고정을 끊는다.

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
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from core.contracts import TRADE_COLUMNS, Trade

__all__ = ["RunManifest", "RunStore", "make_run_id", "current_git_sha"]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = PROJECT_ROOT / "runs"


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
    universe_size: int = 0
    git_sha: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    metrics: Mapping[str, Any] = field(default_factory=dict)
    notes: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, default=str)

    @classmethod
    def from_json(cls, text: str) -> "RunManifest":
        raw = json.loads(text)
        raw["date_range"] = tuple(raw["date_range"])
        return cls(**raw)


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

    # -- 쓰기 ---------------------------------------------------------------

    def save(self, manifest: RunManifest, trades: Iterable[Trade]) -> str:
        """
        TODO(Phase A): manifest.json + trades.parquet 기록.

        구현 메모:
          - trades 를 TRADE_COLUMNS 순서의 DataFrame 으로 변환
          - signal_meta 는 json 문자열로 직렬화 (parquet 중첩 타입 회피)
          - 같은 run_id 가 이미 있으면 덮어쓰지 말고 경고 후 스킵하거나
            run_id 뒤에 시퀀스를 붙인다. 재현성 검증 기회를 날리지 않기 위함.
        """
        raise NotImplementedError("Phase A")

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
        TODO(Phase A): runs/*/manifest.json 을 훑어 조건에 맞는 런 목록 반환.

        런 수가 수천 개를 넘어가면 manifest 를 SQLite 인덱스로 옮긴다.
        그 전까지는 디렉토리 스캔으로 충분하다 (조기 최적화 금지).
        """
        raise NotImplementedError("Phase A")

    def load_trades(self, run_ids: Sequence[str]):
        """
        TODO(Phase A): 여러 런의 trades.parquet 을 concat 한 DataFrame 반환.

        run_id 컬럼이 이미 들어 있으므로 그대로 groupby 비교가 가능하다.
        이것이 '전략 N개 비교'를 가능하게 하는 지점이다.
        """
        raise NotImplementedError("Phase A")

    def load_manifest(self, run_id: str) -> RunManifest:
        path = self.root / run_id / "manifest.json"
        return RunManifest.from_json(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 요약 지표
# ---------------------------------------------------------------------------

def summarize(trades: Sequence[Trade]) -> dict[str, Any]:
    """
    매니페스트에 박아 넣을 요약 지표.

    TODO(Phase A): win_rate, net_pnl_sum, avg_pnl, mdd, profit_factor,
                   trades_per_day, avg_holding_sec 산출.
    dashboard/metrics.py 에 이미 유사 로직이 있으므로 그쪽을 옮겨 온다
    (지표 계산이 두 곳에 있으면 반드시 어긋난다).
    """
    raise NotImplementedError("Phase A")
