"""
features/store.py — 피처 저장소 (Parquet) (Phase B)

왜 L2 만 Parquet 인가 — 계층마다 쓰기/읽기 패턴이 정반대이기 때문이다.

    L0/L1 (SQLite 유지)   장중 초당 1,500건 행 단위 append / 종목별 전체 행 조회
    L2    (Parquet 신규)  장 마감 후 하루치 1회 배치 쓰기 / 60개 컬럼 중 3개만 조회

SQLite 를 걷어내는 게 아니다. 장중 무유실 append 는 SQLite WAL 이 잘하는 일이고
이미 18만 건 유실 0% 로 검증됐다. 각 계층에 맞는 도구를 쓰는 것뿐이다.

레이아웃:
    sampledata/features/
    └── fs_v1/                      피처셋 버전 (계산식 변경 시 fs_v2 로 분기)
        ├── _manifest.json          포함 피처 목록·버전·생성시각·소스 해시
        ├── 20260911.parquet        code, time, <피처 컬럼들...>  — (code,time) 정렬
        └── 20260912.parquet

버전 디렉토리 분기가 핵심이다. fs_v1 -> fs_v2 로 나누면
**기존 백테스트 결과의 재현성이 깨지지 않는다.**

참고: ARCHITECTURE_V2.md §3.5
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional, Sequence

from features.base import FeatureSet

__all__ = ["FeatureStore"]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEATURE_ROOT = PROJECT_ROOT / "sampledata" / "features"


class FeatureStore:
    """
    피처 읽기/쓰기.

        store = FeatureStore(version="fs_v1")
        store.write(date="20260911", frame=df, feature_set=fs)

        df = store.read(date="20260911", codes=["005930"], names=["cbv_10", "obi_top3"])
    """

    def __init__(self, version: str = "fs_v1", root: Path | str | None = None) -> None:
        self.version = version
        self.root = (Path(root) if root else DEFAULT_FEATURE_ROOT) / version
        self.root.mkdir(parents=True, exist_ok=True)

    # -- 경로 ---------------------------------------------------------------

    def path_for(self, date: str) -> Path:
        return self.root / f"{date}.parquet"

    @property
    def manifest_path(self) -> Path:
        return self.root / "_manifest.json"

    def available_dates(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.parquet"))

    # -- 쓰기 ---------------------------------------------------------------

    def write(self, date: str, frame, feature_set: FeatureSet) -> Path:
        """
        TODO(Phase B): 하루치 피처 DataFrame 을 parquet 로 기록.

        구현 메모:
          - 압축은 zstd (level 3 정도). gzip 대비 빠르고 비율도 좋다.
          - (code, time) 으로 정렬 후 기록하고, code 단위 row group 으로 나눈다.
            -> 특정 종목만 읽을 때 predicate pushdown 이 걸린다.
          - 첫 쓰기 때 _manifest.json 을 남기고, 이후 쓰기마다
            feature_set.manifest() 와 대조해 **불일치면 에러**를 낸다.
            같은 fs_v1 디렉토리에 다른 계산식의 피처가 섞이면
            그 버전 전체의 재현성이 깨진다.
        """
        raise NotImplementedError("Phase B")

    def write_manifest(self, feature_set: FeatureSet) -> None:
        self.manifest_path.write_text(
            json.dumps(feature_set.manifest(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    # -- 읽기 ---------------------------------------------------------------

    def read(
        self,
        date: str,
        *,
        codes: Optional[Sequence[str]] = None,
        names: Optional[Sequence[str]] = None,
    ):
        """
        TODO(Phase B): 필요한 컬럼/종목만 선택해 읽는다.

        핵심: names 를 지정하면 **그 컬럼만 I/O 한다.** 60개 컬럼 중 3개만 읽을 때
        SQLite 는 전체 행을 읽고 버리지만 parquet 은 해당 컬럼만 읽는다.
        이 차이가 파라미터 스윕 속도를 가른다.

        구현 메모: pyarrow.parquet.read_table(path, columns=..., filters=...)
        """
        raise NotImplementedError("Phase B")

    def read_range(
        self,
        start: str,
        end: str,
        *,
        codes: Optional[Sequence[str]] = None,
        names: Optional[Sequence[str]] = None,
    ):
        """
        TODO(Phase B): 날짜 범위 읽기.

        파일당 하루 구조이므로 날짜 범위 = 파일 목록이고,
        multiprocessing 분할이 자연스럽다 (run_parallel.py 의 DEFAULT_SPLIT 과 동일 발상).
        """
        raise NotImplementedError("Phase B")
