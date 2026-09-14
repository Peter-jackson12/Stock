"""
features/builders/macro.py — 거시(일봉) 피처 (Phase B)

ARCHITECTURE_V2.md §1.5 가 진단한 **끊어진 연결선**을 잇는 자리다.

    engine/data_loader.py::load_daily_csvs()
        files = ['mkt', 'open', 'high', 'low', 'close', 'tradamt', 'float', 'shares']

8개 일봉 매트릭스를 전부 로드하지만, engine.py 가 실제로 쓰는 건 open 하나뿐이고
그것도 '날짜 목록과 종목 코드 목록'을 얻는 용도다. 시총·유통비율·거래대금은
로드만 되고 필터에 쓰이지 않으며, 결과 CSV 에는 상수가 박힌다.

    self.trading['mkt_float'].append(1000)      # 하드코딩
    self.trading['ytd_tradamt'].append(100)     # 하드코딩

여기서 그 값들을 진짜로 계산한다.

────────────────────────────────────────────────────────────────────────────
⚠️ 시점 규칙(point-in-time) — 이 파일의 존재 이유의 절반

거시 피처는 반드시 **전일까지의 확정 데이터**만 쓴다. 당일 일봉은 장이 끝나야
확정된다. 당일 종가 기준 시총으로 당일 09:05 진입을 필터링하면, 그건 미래를
보고 과거를 거른 것이다 — 백테스트만 잘 나오는 전형적인 사고다.

규칙을 주석이 아니라 코드로 강제한다. DailyMatrix.prior_rows() 는 조회 날짜
**이전** 행만 돌려주며, 당일 행에 접근할 방법 자체를 제공하지 않는다.

참고: ARCHITECTURE_V2.md §3.6, §3.7
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from engine.config import CSV_PATH
from engine.utils import calculate_upperlimit
from features.base import MacroFeature

__all__ = [
    "DailyMatrix",
    "daily_matrix",
    "MarketCap",
    "FloatRatio",
    "AvgTradeAmount",
    "PrevClose",
    "UpperLimit",
    "default_macro_features",
]


#: 일봉 매트릭스 파일 (engine/data_loader.py 와 같은 목록)
MATRIX_FILES = ("mkt", "open", "high", "low", "close", "tradamt", "float", "shares")


class DailyMatrix:
    """
    일봉 매트릭스 조회기. **전일까지만** 볼 수 있다.

    CSV 구조 (engine/data_loader.py 가 읽는 그대로):

        Code      A000270   A005930   ...
        Name      기아       삼성전자
        20220420  78800.0   67000.0
        20220421  80200.0   67600.0

    0행은 종목명, 1행부터가 날짜별 값이다. 컬럼명은 'A' + 종목코드.
    """

    def __init__(self, csv_path: Path | str | None = None) -> None:
        self.csv_path = Path(csv_path) if csv_path else CSV_PATH
        self._matrices: dict[str, pd.DataFrame] = {}
        self._names: dict[str, str] = {}

    # -- 로딩 ---------------------------------------------------------------

    def _load(self, matrix: str) -> pd.DataFrame:
        if matrix in self._matrices:
            return self._matrices[matrix]

        path = self.csv_path / f"{matrix}.csv"
        if not path.exists():
            raise FileNotFoundError(f"일봉 매트릭스가 없습니다: {path}")

        raw = pd.read_csv(path, encoding="CP949", low_memory=False)
        codes = [c[1:] if str(c).startswith("A") else str(c) for c in raw.columns[1:]]

        if not self._names:                       # 종목명은 0행에 있다
            self._names = {
                code: str(value) for code, value in zip(codes, raw.iloc[0, 1:].tolist())
            }

        body = raw.iloc[1:].copy()
        values = body.iloc[:, 1:].apply(pd.to_numeric, errors="coerce")
        values.columns = codes
        values.index = pd.Index(body.iloc[:, 0].astype(str), name="date")

        # YYYYMMDD 행만 남기고 날짜 오름차순으로
        # (prior_rows 가 '마지막 n개 = 가장 최근 n일' 을 전제한다)
        keep = values.index.str.isdigit() & (values.index.str.len() == 8)
        values = values[keep].sort_index()
        self._matrices[matrix] = values
        return values

    def name_of(self, code: str) -> str:
        if not self._names:
            self._load("open")
        return self._names.get(code, "")

    def has(self, matrix: str, code: str) -> bool:
        try:
            return code in self._load(matrix).columns
        except FileNotFoundError:
            return False

    # -- 시점 규칙 -----------------------------------------------------------

    def prior_rows(self, matrix: str, code: str, date: str, n: int = 1) -> np.ndarray:
        """
        date **이전** 거래일의 값 최대 n개 (오래된 것 -> 최신 순).

        당일 행은 어떤 경로로도 나가지 않는다. 이 메서드가 거시 피처의 유일한
        데이터 출입구이므로, 여기만 지키면 룩어헤드가 구조적으로 막힌다.
        """
        frame = self._load(matrix)
        if code not in frame.columns:
            return np.empty(0, dtype=float)

        series = frame.loc[frame.index < str(date), code].dropna()
        if series.empty:
            return np.empty(0, dtype=float)
        return series.to_numpy(dtype=float)[-n:]

    def prior_value(self, matrix: str, code: str, date: str) -> float:
        """전일 확정값 1개. 없으면 NaN."""
        rows = self.prior_rows(matrix, code, date, n=1)
        return float(rows[0]) if rows.size else float("nan")


@lru_cache(maxsize=4)
def daily_matrix(csv_path: Optional[str] = None) -> DailyMatrix:
    """프로세스당 한 번만 읽는다 (8개 CSV x 1,000행 규모)."""
    return DailyMatrix(csv_path)


# ===========================================================================
# 거시 피처
# ===========================================================================

class _DailyMatrixFeature(MacroFeature):
    """일봉 매트릭스 한 장에서 전일값 하나를 뽑는 피처의 공통 뼈대."""

    matrix = ""

    def __init__(self, csv_path: Optional[str] = None) -> None:
        self._csv_path = csv_path

    @property
    def matrices(self) -> DailyMatrix:
        return daily_matrix(self._csv_path)

    def _daily_value(self) -> float:
        return self.matrices.prior_value(self.matrix, self._code, self._date)


class MarketCap(_DailyMatrixFeature):
    """전일 종가 기준 시가총액 (억원). engine.py 의 mkt_float=1000 상수를 대체한다."""

    name = "mkt_cap"
    version = "1.0.0"
    deps = ("daily:mkt",)
    matrix = "mkt"


class FloatRatio(_DailyMatrixFeature):
    """전일 기준 유통주식 비율 (%)."""

    name = "float_ratio"
    version = "1.0.0"
    deps = ("daily:float",)
    matrix = "float"


class PrevClose(_DailyMatrixFeature):
    """전일 종가. 상한가 계산의 기준이자 갭 판단의 출발점."""

    name = "prev_close"
    version = "1.0.0"
    deps = ("daily:close",)
    matrix = "close"


class AvgTradeAmount(MacroFeature):
    """
    전일까지 n거래일 평균 거래대금 (억원).

    engine.py 의 ytd_tradamt=100 상수를 대체한다. 유동성 필터의 근거가 되는
    값이라 '얼마나 많은 날을 평균했는가'가 이름에 들어간다 (ytd_tradamt_20).
    """

    version = "1.0.0"

    def __init__(self, days: int = 20, csv_path: Optional[str] = None) -> None:
        self.days = int(days)
        self.name = f"ytd_tradamt_{self.days}"
        self.deps = ("daily:tradamt",)
        self._csv_path = csv_path

    @property
    def matrices(self) -> DailyMatrix:
        return daily_matrix(self._csv_path)

    def _daily_value(self) -> float:
        rows = self.matrices.prior_rows("tradamt", self._code, self._date, n=self.days)
        if rows.size == 0:
            return float("nan")
        return float(np.mean(rows))


class UpperLimit(MacroFeature):
    """
    전일 종가 기준 상한가 (+30%, 호가단위 절사).

    engine/engine.py 는 지금 `stock['upper'] = opens[0] * 1.3` 으로 **당일 시가**에
    1.3을 곱한다. 상한가는 전일 종가 기준이고 호가단위로 끊어지므로 그 값은
    실제 상한가가 아니다. engine/utils.calculate_upperlimit() 이 이미 있는데
    호출되지 않고 있었다 (§1.6).

    ⚠️ 그래서 이 피처는 레거시 값과 **일치하지 않는다.** 의도된 차이다.
       상수를 실제 계산으로 바꾸는 것은 전략 거동을 바꾸는 일이라 Phase C 에서
       파라미터와 함께 교체한다. 여기서는 올바른 값을 준비만 해 둔다.
    """

    name = "upper_limit"
    version = "1.0.0"
    deps = ("daily:close",)

    def __init__(self, csv_path: Optional[str] = None) -> None:
        self._csv_path = csv_path

    @property
    def matrices(self) -> DailyMatrix:
        return daily_matrix(self._csv_path)

    def _daily_value(self) -> float:
        prev_close = self.matrices.prior_value("close", self._code, self._date)
        if not np.isfinite(prev_close) or prev_close <= 0:
            return float("nan")
        return float(calculate_upperlimit(prev_close, self._date))


def default_macro_features(csv_path: Optional[str] = None) -> list[MacroFeature]:
    """fs_v1 에 들어갈 거시 피처 목록."""
    return [
        MarketCap(csv_path),
        FloatRatio(csv_path),
        PrevClose(csv_path),
        AvgTradeAmount(20, csv_path),
        AvgTradeAmount(5, csv_path),
        UpperLimit(csv_path),
    ]
