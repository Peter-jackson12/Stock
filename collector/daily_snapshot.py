"""
collector/daily_snapshot.py — 일별 tidy 스냅샷 저장소 (rev.2 §5, D-5)

daily_collector 는 매 실행마다 shares/float/mkt 를 포함한 8개 wide 매트릭스
(open.csv 등, 행=날짜·열=종목)를 통째로 다시 썼다. open/high/low/close/tradamt
는 매일 다시 받아도 그 자체가 그날그날의 실제 시세라 문제가 없지만, shares
(상장주식수)와 float(유통비율)는 "수집 시점 하나의 스냅샷"일 뿐이다. 이걸
wide 포맷에 직접 쓰면 다음 수집이 전체를 다시 덮어쓸 때 과거 날짜에 오늘
값이 새어 들어가거나(발견 #0-3 broadcast 버그), 결측이 이전 값으로 조용히
남는 사고가 반복된다.

그래서 shares/float/mkt 세 컬럼은 **tidy(날짜 컬럼이 있는 long 포맷) 스냅샷을
유일한 소스**로 삼는다. 하루치 스냅샷 파일(sampledata/Daily/snapshots/
YYYYMMDD.csv)은 그날 실제로 관측한 값만 담고, wide CSV(엔진이 읽는 레거시
포맷)는 스냅샷 전체를 피벗해 **파생**한다 — 스냅샷에 없는 날짜는 구조적으로
NaN 이 된다. 과거 날짜에 오늘 값을 broadcast 하는 경로 자체가 없어진다.

open/high/low/close/tradamt 는 이 모듈이 다루지 않는다. daily_collector 가
기존 방식(네이버 fchart 다년치 재수집)대로 직접 wide CSV 에 쓴다.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

#: 스냅샷 파일 한 장에 들어가는 컬럼(날짜는 파일명에 있으므로 제외).
SNAPSHOT_COLUMNS = ["code", "name", "mkt", "shares", "float"]

#: 스냅샷에서 파생하는 wide CSV. 나머지(open/high/low/close/tradamt)는
#: daily_collector 가 직접 쓴다.
DERIVED_METRICS = ["mkt", "shares", "float"]


def snapshot_dir(daily_dir: Path | str) -> Path:
    d = Path(daily_dir) / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_snapshot(daily_dir: Path | str, date: str, rows: pd.DataFrame) -> Path:
    """
    그날 전체 유니버스의 (code, name, mkt, shares, float) 행을 저장한다.

    같은 날짜에 다시 수집하면 파일을 통째로 덮어쓴다 — 멱등이며, 하루 안의
    재실행은 최신 수집이 그날의 정답이라는 의미다.
    """
    missing = set(SNAPSHOT_COLUMNS) - set(rows.columns)
    if missing:
        raise ValueError(f"스냅샷에 컬럼 누락: {sorted(missing)}")

    out = snapshot_dir(daily_dir) / f"{date}.csv"
    rows[SNAPSHOT_COLUMNS].to_csv(out, index=False, encoding="utf-8-sig")
    return out


def read_all_snapshots(daily_dir: Path | str) -> pd.DataFrame:
    """지금까지 쌓인 스냅샷 전부를 날짜 컬럼을 붙여 하나의 tidy DataFrame 으로."""
    frames = []
    for path in sorted(snapshot_dir(daily_dir).glob("*.csv")):
        date = path.stem
        df = pd.read_csv(path, encoding="utf-8-sig")
        df.insert(0, "date", date)
        frames.append(df)

    if not frames:
        return pd.DataFrame(columns=["date", *SNAPSHOT_COLUMNS])
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
