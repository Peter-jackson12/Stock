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
import os
import uuid
from pathlib import Path
from typing import Iterable, Optional, Sequence

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from features.base import FeatureSet

__all__ = ["FeatureStore", "KEY_COLUMNS"]

#: 모든 피처 파일이 공통으로 갖는 키 컬럼. 컬럼 선택 읽기에서도 항상 따라온다.
KEY_COLUMNS = ("code", "time")

#: zstd level 3 — gzip 보다 빠르면서 압축률도 좋다 (§3.5)
COMPRESSION = "zstd"
COMPRESSION_LEVEL = 3


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEATURE_ROOT = PROJECT_ROOT / "sampledata" / "features"


def _fsync_file(path: Path) -> None:
    # Windows 는 읽기 전용 핸들의 fsync 를 거부한다(EBADF).
    with open(path, "r+b") as stream:
        os.fsync(stream.fileno())


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

    def write(
        self,
        date: str,
        frame: "pd.DataFrame",
        feature_set: FeatureSet,
        *,
        coverage: Optional[dict] = None,
    ) -> Path:
        """
        하루치 피처 DataFrame 을 parquet 로 기록한다.

          - (code, time) 정렬 후 기록하고 **종목 하나당 row group 하나**로 나눈다.
            특정 종목만 읽을 때 그 row group 만 건드리면 되기 때문이다.
          - 압축은 zstd level 3.
          - 첫 쓰기 때 _manifest.json 을 남기고, 이후 쓰기마다 대조해
            **계산식이 다르면 에러**를 낸다. 같은 fs_v1 디렉토리에 다른 버전의
            피처가 섞이면 그 버전 전체의 재현성이 조용히 깨진다.

        게시 계약 (PIPELINE_AUDIT 2026-09-26 P2):
          - parquet 는 같은 디렉토리의 임시 파일(`.<date>.<id>.tmp`, glob `*.parquet` 에
            걸리지 않음)에 다 쓴 뒤 os.replace 로 한 번에 바꾼다. 도중 실패는 기존 파일을 남긴다.
          - 매니페스트는 **병합**한다. 다른 날짜의 coverage 와 기존 피처 선언을 보존하고,
            이번 피처셋의 선언만 추가/갱신한다. 매니페스트도 임시 파일 + os.replace 로 바꾼다.
          - 같은 날짜를 다시 쓰면 그 날짜의 기존 coverage 는 새 파일을 설명하지 않으므로
            parquet 교체 **전에** 지운다(중간 중단 시 '모름'이지 '틀린 값'이 아니다).
            coverage 를 넘기면 parquet 교체 뒤 같은 매니페스트 갱신에서 그 날짜에 기록한다.
          - strict 판정 같은 검증은 호출자가 이 메서드를 부르기 **전에** 끝내야 한다.
        """
        if frame is None or len(frame) == 0:
            raise ValueError(f"{date}: 기록할 피처가 없습니다")

        missing = [c for c in KEY_COLUMNS if c not in frame.columns]
        if missing:
            raise ValueError(f"키 컬럼이 없습니다: {missing}")

        self._check_manifest(feature_set)

        ordered = frame.sort_values(list(KEY_COLUMNS), kind="stable").reset_index(drop=True)
        ordered["code"] = ordered["code"].astype(str)
        ordered["time"] = ordered["time"].astype(str)

        table = pa.Table.from_pandas(ordered, preserve_index=False)
        path = self.path_for(date)
        staged = self.root / f".{date}.{uuid.uuid4().hex}.tmp"

        try:
            # 종목 단위로 나눠 쓰면 write_table 호출 하나가 row group 하나가 된다
            boundaries = self._code_boundaries(ordered["code"])
            with pq.ParquetWriter(
                staged, table.schema, compression=COMPRESSION, compression_level=COMPRESSION_LEVEL
            ) as writer:
                for start, length in boundaries:
                    writer.write_table(table.slice(start, length))
            _fsync_file(staged)

            existing = self.read_manifest()
            if str(date) in existing.get("coverage", {}):
                # 이전 파일의 coverage 를 새 파일 설명으로 남기지 않는다.
                self.replace_manifest(self._merged_manifest(existing, feature_set, date, None))
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)

        self.replace_manifest(self._merged_manifest(self.read_manifest(), feature_set, date, coverage))
        return path

    @staticmethod
    def _merged_manifest(existing: dict, feature_set: FeatureSet, date: str,
                         coverage: Optional[dict]) -> dict:
        """기존 매니페스트에 이번 피처셋 선언을 합친다. 다른 날짜 coverage 는 그대로 둔다."""
        incoming = feature_set.manifest()
        merged = dict(existing)
        merged["feature_set_version"] = incoming["feature_set_version"]
        by_name = {entry["name"]: entry for entry in existing.get("features", [])}
        for entry in incoming["features"]:
            by_name[entry["name"]] = entry
        merged["features"] = list(by_name.values())
        dates = dict(existing.get("coverage", {}))
        dates.pop(str(date), None)
        if coverage is not None:
            dates[str(date)] = coverage
        if dates or "coverage" in existing:
            merged["coverage"] = dict(sorted(dates.items()))
        return merged

    @staticmethod
    def _code_boundaries(codes: "pd.Series") -> list[tuple[int, int]]:
        """정렬된 code 시리즈 -> [(시작 행, 행 수)] — row group 경계."""
        changed = codes.ne(codes.shift())
        starts = changed.to_numpy().nonzero()[0].tolist()
        ends = starts[1:] + [len(codes)]
        return [(s, e - s) for s, e in zip(starts, ends)]

    def _check_manifest(self, feature_set: FeatureSet) -> None:
        existing = self.read_manifest()
        if not existing:
            return
        incoming = feature_set.manifest()
        if existing.get("feature_set_version") != incoming.get("feature_set_version"):
            raise ValueError(
                f"피처셋 버전이 다릅니다: 저장된 {existing.get('feature_set_version')} "
                f"vs 새로 쓰려는 {incoming.get('feature_set_version')}"
            )

        old = {f["name"]: f["version"] for f in existing.get("features", [])}
        new = {f["name"]: f["version"] for f in incoming.get("features", [])}
        conflicts = [
            f"{name}: 저장된 {old[name]} vs 새로운 {new[name]}"
            for name in set(old) & set(new) if old[name] != new[name]
        ]
        if conflicts:
            raise ValueError(
                f"같은 이름의 피처가 다른 버전으로 기록되려 합니다 ({self.version}). "
                f"계산식이 바뀌었다면 fs_v2 로 분기하세요: " + "; ".join(conflicts)
            )

    def write_manifest(self, feature_set: FeatureSet) -> None:
        """피처셋 선언을 매니페스트에 병합한다. 기존 날짜별 coverage 는 보존한다."""
        existing = self.read_manifest()
        merged = self._merged_manifest(existing, feature_set, "", None) if existing else feature_set.manifest()
        self.replace_manifest(merged)

    def replace_manifest(self, manifest: dict) -> None:
        """매니페스트 전체를 임시 파일 + fsync + os.replace 로 바꾼다. 반쪽 JSON 을 남기지 않는다."""
        staged = self.root / f"._manifest.{uuid.uuid4().hex}.tmp"
        try:
            with staged.open("w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, self.manifest_path)
        finally:
            staged.unlink(missing_ok=True)

    def read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def record_coverage(self, date: str, coverage: dict) -> dict:
        """
        하루치 커버리지를 매니페스트에 누적 기록한다 (§3.6.1).

        커버리지는 날짜마다 다르므로 피처 목록과 달리 date 별로 쌓는다. 기록해 두는
        이유는 결손 자체가 아니라 **결손을 모른 채 지나가는 것**이 문제이기 때문이다.
        거시 필터를 켜는 순간 커버리지 1.5% 는 유니버스의 98.5% 를 조용히 날린다.
        런을 나중에 들여다볼 때 "그때 그 파일의 커버리지가 얼마였나"를 답할 수 있어야 한다.
        같은 날짜를 다시 기록하면 그 날짜 값만 교체한다.
        """
        manifest = self.read_manifest()
        if not manifest:
            raise FileNotFoundError(
                f"매니페스트가 없습니다: {self.manifest_path}. 먼저 write() 로 피처를 기록하세요"
            )
        manifest.setdefault("coverage", {})[str(date)] = coverage
        self.replace_manifest(manifest)
        return manifest

    # -- 읽기 ---------------------------------------------------------------

    def read(
        self,
        date: str,
        *,
        codes: Optional[Sequence[str]] = None,
        names: Optional[Sequence[str]] = None,
    ) -> "pd.DataFrame":
        """
        필요한 컬럼/종목만 선택해 읽는다.

        **핵심: names 를 지정하면 그 컬럼만 I/O 한다.** 60개 컬럼 중 3개만 쓸 때
        SQLite 는 전체 행을 읽고 나머지를 버리지만, parquet 은 해당 컬럼 청크만
        디스크에서 꺼낸다. 파라미터 스윕처럼 같은 데이터를 수백 번 읽는 작업에서
        이 차이가 그대로 시간이 된다 (§3.5).

        codes 를 지정하면 code 단위 row group 에 predicate pushdown 이 걸린다.
        """
        path = self.path_for(date)
        if not path.exists():
            raise FileNotFoundError(f"피처 파일이 없습니다: {path}")

        columns = None
        if names is not None:
            available = set(self.columns_of(date))
            unknown = [n for n in names if n not in available]
            if unknown:
                raise KeyError(
                    f"{date} 에 없는 피처: {unknown}. "
                    f"있는 피처: {sorted(available - set(KEY_COLUMNS))}"
                )
            # 키 컬럼은 항상 함께 — 없으면 어느 종목의 몇 시 값인지 알 수 없다
            columns = list(KEY_COLUMNS) + [n for n in names if n not in KEY_COLUMNS]

        filters = [("code", "in", list(codes))] if codes else None
        table = pq.read_table(path, columns=columns, filters=filters)
        return table.to_pandas()

    def columns_of(self, date: str) -> list[str]:
        """파일을 열지 않고 스키마만 본다 (푸터만 읽는다)."""
        return list(pq.ParquetFile(self.path_for(date)).schema_arrow.names)

    def file_stats(self, date: str) -> dict:
        """
        컬럼별 압축 크기 등 물리 통계. 컬럼 선택 읽기의 이점을 숫자로 확인할 때 쓴다.
        """
        meta = pq.ParquetFile(self.path_for(date)).metadata
        sizes: dict[str, int] = {}
        for rg in range(meta.num_row_groups):
            group = meta.row_group(rg)
            for c in range(group.num_columns):
                col = group.column(c)
                name = col.path_in_schema
                sizes[name] = sizes.get(name, 0) + col.total_compressed_size
        return {
            "rows": meta.num_rows,
            "row_groups": meta.num_row_groups,
            "columns": meta.num_columns,
            "file_bytes": self.path_for(date).stat().st_size,
            "column_bytes": sizes,
        }

    def read_range(
        self,
        start: str,
        end: str,
        *,
        codes: Optional[Sequence[str]] = None,
        names: Optional[Sequence[str]] = None,
    ) -> "pd.DataFrame":
        """
        날짜 범위 읽기. date 컬럼이 붙어 나온다.

        파일당 하루 구조이므로 '날짜 범위 = 파일 목록' 이고, 그래서 병렬 처리가
        자연스럽다 (run_parallel.py 의 DEFAULT_SPLIT 과 같은 발상).
        """
        dates = [d for d in self.available_dates() if str(start) <= d <= str(end)]
        frames = []
        for date in dates:
            frame = self.read(date, codes=codes, names=names)
            frame.insert(0, "date", date)
            frames.append(frame)

        if not frames:
            return pd.DataFrame(columns=["date", *KEY_COLUMNS])
        return pd.concat(frames, ignore_index=True)
