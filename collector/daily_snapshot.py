"""D-5: 날짜가 명시된 관측 스냅샷과 파생 wide CSV.
동일 날짜는 최초 유효값을 보존하며 결측과 새 종목만 보충한다.
단일 수집 프로세스에서 사용한다(동시 writer는 지원하지 않음).
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime
import os
import tempfile

import pandas as pd

SNAPSHOT_COLUMNS = ["date", "code", "name", "mkt", "shares", "float"]

#: 스냅샷에서 파생하는 wide CSV. 나머지(open/high/low/close/tradamt)는
#: daily_collector 가 직접 쓴다.
DERIVED_METRICS = ["mkt", "shares", "float"]


def snapshot_dir(daily_dir: Path | str) -> Path:
    d = Path(daily_dir) / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _validate(date: str, rows: pd.DataFrame) -> pd.DataFrame:
    if datetime.strptime(date, "%Y%m%d").strftime("%Y%m%d") != date:
        raise ValueError("date must be YYYYMMDD")
    missing = set(SNAPSHOT_COLUMNS) - set(rows.columns)
    if missing:
        raise ValueError(f"스냅샷에 컬럼 누락: {sorted(missing)}")
    rows = rows[SNAPSHOT_COLUMNS].copy()
    rows["date"] = rows["date"].astype(str)
    if not rows["date"].eq(date).all():
        raise ValueError("snapshot date does not match filename")
    rows["code"] = rows["code"].astype(str).str.zfill(6)
    if not rows["code"].str.fullmatch(r"[0-9]{6}").all():
        raise ValueError("invalid stock code")
    if rows["code"].duplicated().any():
        raise ValueError("duplicate snapshot code")
    return rows


def _read_snapshot(path: Path) -> pd.DataFrame:
    rows = pd.read_csv(path, encoding="utf-8-sig", dtype={"code": str, "date": str})
    # 이전 파일은 읽기만 호환한다. 파일명은 과거 관측 시점의 진위를 증명하지 않는다.
    if "date" not in rows:
        rows.insert(0, "date", path.stem)
    return _validate(path.stem, rows)


def write_snapshot(daily_dir: Path | str, date: str, rows: pd.DataFrame) -> Path:
    """최초 유효값 보존, 결측 보충, 새 종목 추가. 실패 시 기존 파일 보존."""
    rows = _validate(date, rows)
    out = snapshot_dir(daily_dir) / f"{date}.csv"
    if out.exists():
        old = _read_snapshot(out).set_index("code")
        new = rows.set_index("code")
        # shares가 달라졌으면 기존 shares에 새 시총을 붙이지 않는다.
        for code in old.index.intersection(new.index):
            if pd.notna(old.loc[code, "shares"]) and old.loc[code, "shares"] != new.loc[code, "shares"]:
                new.loc[code, "mkt"] = float("nan")
        rows = old.combine_first(new).reset_index()[SNAPSHOT_COLUMNS]
    fd, tmp = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=out.parent)
    os.close(fd)
    try:
        rows.to_csv(tmp, index=False, encoding="utf-8-sig")
        os.replace(tmp, out)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return out


def read_all_snapshots(daily_dir: Path | str) -> pd.DataFrame:
    frames = [_read_snapshot(p) for p in sorted(snapshot_dir(daily_dir).glob("*.csv"))]
    if not frames:
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    return pd.concat(frames, ignore_index=True)


def rebuild_wide_csvs(daily_dir: Path | str, *, name_map: dict[str, str]) -> None:
    """
    쌓인 스냅샷을 피벗해 mkt.csv / shares.csv / float.csv 를 다시 만든다.

    엔진(DataLoader)이 읽는 기존 규격(1행 Name, 1열 Code, 열 A000000, CP949)을
    그대로 따른다 — 엔진·features 쪽 코드는 이 변경을 몰라도 된다.
    """
    daily_dir = Path(daily_dir)
    tidy = read_all_snapshots(daily_dir)
    if tidy.empty:
        return

    tidy = tidy.copy()
    stored_names = {"A" + r.code: r.name for r in tidy.itertuples() if pd.notna(r.name)}
    name_map = {**stored_names, **name_map}
    tidy["col"] = "A" + tidy["code"].astype(str).str.zfill(6)

    for metric in DERIVED_METRICS:
        # dropna=False: 전부 NaN 인 행/열도 유지한다. 기본값(True)이면 아직
        # shares/float 를 한 번도 못 구한 종목이 wide CSV 에서 통째로 사라진다
        # — 결측과 "종목이 없음"을 구분 못 하게 되는 또 다른 조용한 실패다.
        pivot = tidy.pivot_table(
            index="date", columns="col", values=metric, aggfunc="last", dropna=False,
        )
        pivot = pivot.sort_index()

        name_row = pd.DataFrame(
            [{c: name_map.get(c, "") for c in pivot.columns}], index=["Name"]
        )
        final_df = pd.concat([name_row, pivot])
        final_df.index.name = "Code"
        final_df.reset_index(inplace=True)

        out_path = daily_dir / f"{metric}.csv"
        final_df.to_csv(out_path, encoding="CP949", index=False)
