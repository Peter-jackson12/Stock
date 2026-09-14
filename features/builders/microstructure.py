"""
features/builders/microstructure.py — 미시구조 피처 (Phase B/D)

이 파일은 **패턴 데모**다. 두 개의 피처가 batch/stream 이중 구현으로 완전히
작성되어 있고, tests/test_feature_parity.py 가 둘의 일치를 실제로 검증한다.
나머지 피처는 이 패턴을 따라 ARCHITECTURE_V2.md §3.7 의 매핑표대로 이식한다.

이식 현황 (engine/strategy.py -> 여기) — ARCHITECTURE_V2.md §3.7 매핑표:
    cbv_{5,10,30,60}           ✅ RollingBuyVolMean
    obi_top3                   ✅ OrderBookImbalance
    cbv_ratio_max_{w}          ✅ CbvRatioMax          (max{w}buyratio)
    cbv_ratio_tmax_{w}         ✅ CbvRatioTmax         (t_max{w}buyratio)
    cbv_1                      ✅ BuyVolOnOpenBreak    (cbv_1)
    cbv_ratio_tmax_1           ✅ CbvRatioTmax1        (t_max1buyratio)
    tick_rate_cum              ✅ TickRateCum          (ctotal)
    amt_{w}s, bamt_{w}s        ✅ WindowAmount         (amt_{w}s / bamt_{w}s)
    trigger_dev_{min,max}_{w}  ✅ TriggerDev           (min/max{w}_trigger)
                                  monotonic deque 로 amortized O(1)
    buy_ratio_15t              ✅ RecentBuyRatio       (nxt 엔진 15틱 매수비중)
    tick_size_ratio            ✅ TickSizeRatio        (utils.calculate_ticksize 연결)

거시(일봉) 피처는 features/builders/macro.py 에 있다.

────────────────────────────────────────────────────────────────────────────
값이 갱신되지 않는 t 의 처리 — **직전 값을 끌고 간다(carry forward)**

engine/strategy.py 는 stock 딕셔너리에 값을 쓴다. 조건이 맞지 않는 t 에서는
그냥 쓰지 않으므로, 전략이 그 시점에 읽는 값은 **마지막으로 갱신된 값**이다.

    if t > w:                       # 조건 불충족 t 에서는
        stock[f'cbv_{w}'] = ...     # 이 줄이 실행되지 않는다
                                    # -> stock['cbv_10'] 은 직전 값 그대로

배치 피처가 그 t 에 0 을 넣으면 전략이 보는 숫자가 달라진다. 그래서 모든
조건부 피처는 _carry_forward 로 직전 값을 끌고 간다. 갱신된 적이 없으면 0.0
(레거시의 stock.get(key, 0) 과 같다).

이 규칙을 지켜야 scripts/verify_features_vs_legacy.py 의 실데이터 대조가
통과한다. 한 칸이라도 어긋나면 거기서 잡힌다.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Mapping, Optional, Sequence

import numpy as np

from engine.utils import calculate_ticksize
from features.base import BatchContext, MicroFeature

__all__ = [
    "RollingBuyVolMean",
    "OrderBookImbalance",
    "CbvRatioMax",
    "CbvRatioTmax",
    "BuyVolOnOpenBreak",
    "CbvRatioTmax1",
    "TickRateCum",
    "WindowAmount",
    "TriggerDev",
    "RecentBuyRatio",
    "TickSizeRatio",
    "default_micro_features",
]

MICRO_WINDOWS = (5, 10, 30, 60)          # strategy.py 의 windows
TRIGGER_WINDOWS = (1, 5, 10, 30, 60)     # strategy.py 의 min/max_{w}_trigger 윈도우


# ===========================================================================
# 공통 도구
# ===========================================================================

def _to_seconds(value: Any) -> int:
    """'HHMMSS' -> 당일 누적 초. engine/utils.calculate_time_spread 와 같은 규칙."""
    raw = str(value).zfill(6)
    return int(raw[0:2]) * 3600 + int(raw[2:4]) * 60 + int(raw[4:6])


def _round3(value: float) -> float:
    """
    소수 3자리 반올림 — **반드시 numpy 규칙으로.**

    레거시 strategy.py 는 numpy float64 끼리 계산한 뒤 round(x, 3) 을 부른다.
    그러면 numpy 의 반올림이 동작한다. 스트리밍 쪽에서 무심코 Python float 으로
    바꿔 계산하면 0.2375 같은 경계값에서 Python 의 round 가 0.237 을,
    numpy 가 0.238 을 내놓는다. 실제로 20220425/000270 의 t=6531 에서 이
    차이가 잡혔다 — 값 하나가 0.001 어긋나는, 에러로는 절대 드러나지 않는 종류의
    불일치다. 배치와 스트리밍이 같은 헬퍼를 쓰게 해서 원천 봉쇄한다.
    """
    return float(np.round(value, 3))


def _carry_forward(values: np.ndarray, valid: np.ndarray, initial: float = 0.0) -> np.ndarray:
    """
    valid 가 False 인 자리에 **직전 유효값**을 채운다 (한 번도 없었으면 initial).

    레거시 딕셔너리가 값을 덮어쓰지 않고 남겨두는 동작을 그대로 옮긴 것이다.
    """
    n = len(values)
    if n == 0:
        return values.astype(float)
    idx = np.where(valid, np.arange(n), -1)
    np.maximum.accumulate(idx, out=idx)
    out = np.full(n, float(initial), dtype=float)
    seen = idx >= 0
    out[seen] = np.asarray(values, dtype=float)[idx[seen]]
    return out


def _rolling_mean_excluding_current(x: np.ndarray, w: int) -> np.ndarray:
    """
    sum(x[t-w:t]) / w  —  t > w 에서만 유효. 그 외는 0.

    strategy.py 의 cbv 계산과 완전히 같은 경계를 쓴다 ([t-w, t), 현재 시점 제외).
    """
    n = x.size
    out = np.zeros(n, dtype=float)
    if n <= w:
        return out
    cumsum = np.concatenate(([0.0], np.cumsum(x)))
    t = np.arange(w + 1, n)
    out[t] = (cumsum[t] - cumsum[t - w]) / w
    return out


class _MonotonicWindow:
    """
    [t-w, t) 구간의 최솟값(또는 최댓값)을 amortized O(1) 로 유지하는 단조 덱.

    strategy.py 는 매 t 마다 min()/max() 로 윈도우를 통째로 재스캔한다.
    종목 1개 하루 13,200행 x 윈도우 5개 x 2(min/max) 면 재스캔만 수백만 회다.
    같은 결과를 한 번의 순회로 얻는다. 배치와 스트리밍이 **같은 클래스**를 쓰므로
    둘이 어긋날 여지도 없다.
    """

    __slots__ = ("w", "_is_min", "_dq", "_n")

    def __init__(self, window: int, is_min: bool) -> None:
        self.w = int(window)
        self._is_min = is_min
        self._dq: deque[tuple[int, float]] = deque()   # (index, value) 단조 유지
        self._n = 0                                     # 지금까지 push 한 개수 == 현재 t

    def current(self) -> Optional[float]:
        """현재 t 기준 [t-w, t) 의 최솟값/최댓값. 아직 유효하지 않으면 None."""
        t = self._n
        while self._dq and self._dq[0][0] < t - self.w:
            self._dq.popleft()
        if t <= self.w:          # strategy.py 와 같이 t > w 에서만 유효
            return None
        return self._dq[0][1] if self._dq else None

    def push(self, value: float) -> None:
        v = float(value)
        if self._is_min:
            while self._dq and self._dq[-1][1] >= v:
                self._dq.pop()
        else:
            while self._dq and self._dq[-1][1] <= v:
                self._dq.pop()
        self._dq.append((self._n, v))
        self._n += 1


# ===========================================================================
# 1. 구간 매수체결량 평균 (cbv_w)
# ===========================================================================
#
# 현재 engine/strategy.py 의 대응 코드:
#     stock[f'cbv_{w}'] = sum(stock['buy_vol'][t - w: t]) / w   # t > w 일 때만
#
# 주의: 윈도우가 [t-w, t) 로 **현재 시점 t 를 제외**한다.
#       이 경계 규칙을 batch/stream 양쪽이 정확히 똑같이 지켜야 한다.
#       한 칸만 어긋나도 룩어헤드가 되거나 실전에서 다른 값이 나온다.
# ===========================================================================

class RollingBuyVolMean(MicroFeature):
    """구간 매수체결량 평균. 윈도우는 [t-w, t) — 현재 시점 제외."""

    version = "1.0.0"
    deps = ("buy_vol",)

    def __init__(self, window: int, field: str = "buy_vol") -> None:
        self.window = int(window)
        self.field = field
        self.name = f"cbv_{self.window}"
        self.warmup = self.window + 1       # t > w 부터 유효
        self.deps = (field,)

    # -- 배치: 누적합 차분으로 벡터화 ------------------------------------
    def batch(self, ctx: BatchContext) -> np.ndarray:
        x = np.asarray(ctx.col(self.field), dtype=float)
        n = x.size
        out = np.zeros(n, dtype=float)
        w = self.window
        if n <= w:
            return out

        cumsum = np.concatenate(([0.0], np.cumsum(x)))   # cumsum[i] == sum(x[:i])
        t = np.arange(w + 1, n)                          # t > w 조건
        out[t] = (cumsum[t] - cumsum[t - w]) / w
        return out

    # -- 스트리밍: 롤링 합계 O(1) ----------------------------------------
    def stream(self) -> "_RollingMeanState":
        return _RollingMeanState(self.window, self.field)


class _RollingMeanState:
    """
    O(1) 증분 롤링 평균.

    호출 순서가 중요하다: **먼저 현재 값을 계산하고, 그 다음에 이벤트를 버퍼에 넣는다.**
    이렇게 해야 윈도우가 [t-w, t) 로 현재 시점을 제외한다 (배치 구현과 동일).
    순서를 뒤집으면 현재 틱이 자기 자신의 지표에 반영되어 룩어헤드가 된다.
    """

    __slots__ = ("w", "field", "_buf", "_total", "_n")

    def __init__(self, window: int, field: str) -> None:
        self.w = int(window)
        self.field = field
        self._buf: deque[float] = deque(maxlen=self.w)
        self._total = 0.0
        self._n = 0                      # 지금까지 소비한 이벤트 수 == 현재 인덱스 t

    def update(self, event: Mapping[str, Any]) -> float:
        # 1) 현재 시점 값 (아직 현재 이벤트 반영 전 = [t-w, t))
        value = self._total / self.w if self._n > self.w else 0.0

        # 2) 현재 이벤트를 버퍼에 반영
        x = float(event[self.field])
        if len(self._buf) == self.w:
            self._total -= self._buf[0]  # maxlen deque 는 append 시 좌측 자동 방출
        self._buf.append(x)
        self._total += x
        self._n += 1

        return value

    @property
    def ready(self) -> bool:
        return self._n > self.w


# ===========================================================================
# 2. 호가잔량 불균형 (obi_top3)
# ===========================================================================
#
# 현재 engine/nxt_tick_engine.py 의 대응 코드:
#     is_obi_bullish = tick["bid_v_top3"] > (tick["ask_v_top3"] * 1.2)
#
# ⚠️ 여기서 1.2 는 **전략 파라미터**이지 피처가 아니다.
#    피처는 비율 자체만 계산하고, 임계값 비교는 L3(전략)이 한다.
#    이 경계가 ARCHITECTURE_V2.md §2 에서 말한 "가장 중요한 경계"다.
# ===========================================================================

class OrderBookImbalance(MicroFeature):
    """매수 1~3호가 잔량 / 매도 1~3호가 잔량. 시점별 독립 계산(무상태)."""

    name = "obi_top3"
    version = "1.0.0"
    deps = ("bid_v_top3", "ask_v_top3")
    warmup = 0
    resolution = "tick"

    def batch(self, ctx: BatchContext) -> np.ndarray:
        bid = np.asarray(ctx.col("bid_v_top3"), dtype=float)
        ask = np.asarray(ctx.col("ask_v_top3"), dtype=float)
        return np.divide(bid, ask, out=np.zeros_like(bid), where=ask > 0)

    def stream(self) -> "_OrderBookImbalanceState":
        return _OrderBookImbalanceState()


class _OrderBookImbalanceState:
    __slots__ = ("_seen",)

    def __init__(self) -> None:
        self._seen = False

    def update(self, event: Mapping[str, Any]) -> float:
        self._seen = True
        ask = float(event["ask_v_top3"])
        if ask <= 0:
            return 0.0
        return float(event["bid_v_top3"]) / ask

    @property
    def ready(self) -> bool:
        return self._seen


# ===========================================================================
# 3. 구간 매수체결량 비율 (cbv_ratio_max_{w} / cbv_ratio_tmax_{w})
# ===========================================================================
#
# strategy.py 대응 코드:
#     if t > w:
#         if cbv_w > max_cbv_w: max_cbv_w = cbv_w          # 먼저 최대값 갱신
#         if max_cbv_w != 0:
#             stock[f'max{w}buyratio'] = round(cbv_w / max_cbv_w, 3)
#
# 순서가 중요하다. 현재 값으로 최대값을 **먼저** 갱신한 뒤 나눈다. 그래서 신고가
# 갱신 시점의 비율은 정확히 1.0 이고, 값은 절대 1 을 넘지 않는다.
# ===========================================================================

class CbvRatioMax(MicroFeature):
    """당일 누적 최대 cbv 대비 현재 cbv 비율. 1.0 이면 지금이 당일 최대."""

    version = "1.0.0"

    def __init__(self, window: int, field: str = "buy_vol") -> None:
        self.window = int(window)
        self.field = field
        self.name = f"cbv_ratio_max_{self.window}"
        self.warmup = self.window + 1
        self.deps = (field,)

    def batch(self, ctx: BatchContext) -> np.ndarray:
        x = np.asarray(ctx.col(self.field), dtype=float)
        cbv = _rolling_mean_excluding_current(x, self.window)
        valid = np.arange(x.size) > self.window
        return _ratio_to_running_max(cbv, valid)

    def stream(self) -> "_CbvRatioMaxState":
        return _CbvRatioMaxState(self.window, self.field, conditional=False)


class CbvRatioTmax(MicroFeature):
    """
    조건부(당일 고가 >= 시가) 누적 최대 cbv 대비 비율.

    strategy.py 의 t_max{w}buyratio. '시가를 회복한 구간에서의' 최대 매수세와
    지금을 비교한다. 조건이 맞지 않는 t 에서는 갱신되지 않고 직전 값이 남는다.
    """

    version = "1.0.0"

    def __init__(self, window: int, field: str = "buy_vol") -> None:
        self.window = int(window)
        self.field = field
        self.name = f"cbv_ratio_tmax_{self.window}"
        self.warmup = self.window + 1
        self.deps = (field, "high", "open")

    def batch(self, ctx: BatchContext) -> np.ndarray:
        x = np.asarray(ctx.col(self.field), dtype=float)
        high = np.asarray(ctx.col("high"), dtype=float)
        day_open = float(np.asarray(ctx.col("open"), dtype=float)[0])
        cbv = _rolling_mean_excluding_current(x, self.window)
        valid = (np.arange(x.size) > self.window) & (high >= day_open)
        return _ratio_to_running_max(cbv, valid)

    def stream(self) -> "_CbvRatioMaxState":
        return _CbvRatioMaxState(self.window, self.field, conditional=True)


def _ratio_to_running_max(values: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """유효 구간의 누적 최대값 대비 비율 (레거시와 같이 round 3자리, 미갱신은 carry)."""
    running = np.maximum.accumulate(np.where(valid, values, -np.inf))
    ok = valid & (running > 0)
    ratio = np.zeros(values.size, dtype=float)
    ratio[ok] = np.round(values[ok] / running[ok], 3)
    return _carry_forward(ratio, ok)


class _CbvRatioMaxState:
    """O(1) 롤링 평균 + 누적 최대값. 조건부/무조건 두 가지를 한 클래스로."""

    __slots__ = ("w", "field", "conditional", "_buf", "_total", "_n", "_max", "_last", "_day_open")

    def __init__(self, window: int, field: str, *, conditional: bool) -> None:
        self.w = int(window)
        self.field = field
        self.conditional = conditional
        self._buf: deque[float] = deque(maxlen=self.w)
        self._total = 0.0
        self._n = 0
        self._max = 0.0
        self._last = 0.0
        self._day_open: Optional[float] = None

    def update(self, event: Mapping[str, Any]) -> float:
        if self._day_open is None and "open" in event:
            self._day_open = float(event["open"])

        cbv = self._total / self.w if self._n > self.w else 0.0

        valid = self._n > self.w
        if valid and self.conditional:
            valid = self._day_open is not None and float(event["high"]) >= self._day_open
        if valid:
            if cbv > self._max:
                self._max = cbv
            if self._max != 0:
                self._last = _round3(cbv / self._max)

        x = float(event[self.field])
        if len(self._buf) == self.w:
            self._total -= self._buf[0]
        self._buf.append(x)
        self._total += x
        self._n += 1
        return self._last

    @property
    def ready(self) -> bool:
        return self._n > self.w


# ===========================================================================
# 4. 시가 회복 구간의 1초 매수체결량 (cbv_1 / cbv_ratio_tmax_1)
# ===========================================================================
#
# strategy.py 대응 코드:
#     stock['cbv_1'] = 0                                   # 매 t 마다 0 으로 리셋
#     if t > 0 and stock['high'][t] >= stock['open'][0]:
#         stock['cbv_1'] = stock['buy_vol'][t]
#
# 다른 피처와 달리 **carry forward 가 아니다.** 매 t 마다 0 으로 초기화되므로
# 조건을 만족하지 않는 t 의 값은 직전 값이 아니라 0 이다.
# ===========================================================================

class BuyVolOnOpenBreak(MicroFeature):
    """시가를 회복한 봉의 매수체결량. 아니면 0 (직전 값을 끌고 가지 않는다)."""

    name = "cbv_1"
    version = "1.0.0"
    deps = ("buy_vol", "high", "open")
    warmup = 1

    def batch(self, ctx: BatchContext) -> np.ndarray:
        buy_vol = np.asarray(ctx.col("buy_vol"), dtype=float)
        high = np.asarray(ctx.col("high"), dtype=float)
        day_open = float(np.asarray(ctx.col("open"), dtype=float)[0])
        cond = (np.arange(buy_vol.size) > 0) & (high >= day_open)
        return np.where(cond, buy_vol, 0.0)

    def stream(self) -> "_BuyVolOnOpenBreakState":
        return _BuyVolOnOpenBreakState()


class _BuyVolOnOpenBreakState:
    __slots__ = ("_n", "_day_open")

    def __init__(self) -> None:
        self._n = 0
        self._day_open: Optional[float] = None

    def update(self, event: Mapping[str, Any]) -> float:
        if self._day_open is None:
            self._day_open = float(event["open"])
        value = 0.0
        if self._n > 0 and float(event["high"]) >= self._day_open:
            value = float(event["buy_vol"])
        self._n += 1
        return value

    @property
    def ready(self) -> bool:
        return self._n > 1


class CbvRatioTmax1(MicroFeature):
    """cbv_1 의 당일 누적 최대 대비 비율 (strategy.py 의 t_max1buyratio)."""

    name = "cbv_ratio_tmax_1"
    version = "1.0.0"
    deps = ("buy_vol", "high", "open")
    warmup = 1

    def batch(self, ctx: BatchContext) -> np.ndarray:
        buy_vol = np.asarray(ctx.col("buy_vol"), dtype=float)
        high = np.asarray(ctx.col("high"), dtype=float)
        day_open = float(np.asarray(ctx.col("open"), dtype=float)[0])
        cond = (np.arange(buy_vol.size) > 0) & (high >= day_open)
        cbv_1 = np.where(cond, buy_vol, 0.0)
        return _ratio_to_running_max(cbv_1, cond)

    def stream(self) -> "_CbvRatioTmax1State":
        return _CbvRatioTmax1State()


class _CbvRatioTmax1State:
    __slots__ = ("_n", "_day_open", "_max", "_last")

    def __init__(self) -> None:
        self._n = 0
        self._day_open: Optional[float] = None
        self._max = 0.0
        self._last = 0.0

    def update(self, event: Mapping[str, Any]) -> float:
        if self._day_open is None:
            self._day_open = float(event["open"])
        if self._n > 0 and float(event["high"]) >= self._day_open:
            cbv_1 = float(event["buy_vol"])
            if cbv_1 > self._max:
                self._max = cbv_1
            if self._max != 0:
                self._last = _round3(cbv_1 / self._max)
        self._n += 1
        return self._last

    @property
    def ready(self) -> bool:
        return self._n > 1


# ===========================================================================
# 5. 누적 체결강도 (tick_rate_cum) — strategy.py 의 ctotal
# ===========================================================================
#
#     stock['ctotal'] = sum(stock['tick'][:t]) / time_spread
#
# 분자는 [0, t) — **현재 봉을 제외**한 누적 틱수. 분모는 첫 봉부터 현재 봉까지의
# 경과 '초'(벽시계)다. 윈도우는 행 개수인데 분모는 시계 초라는 점이 헷갈리기 쉽다.
# LOB 은 체결이 있을 때만 행이 생겨 1초 간격이 아니기 때문에 둘은 다르다.
#
# deps 의 'sec' 는 당일 누적 초로 바꾼 시각 컬럼이다 (BatchContext 는 숫자만 담는다).
# ===========================================================================

class TickRateCum(MicroFeature):
    """초당 누적 체결 틱수. 장 초반 체결 강도를 재는 지표."""

    name = "tick_rate_cum"
    version = "1.0.0"
    deps = ("tick", "sec")
    warmup = 1

    def batch(self, ctx: BatchContext) -> np.ndarray:
        tick = np.asarray(ctx.col("tick"), dtype=float)
        sec = np.asarray(ctx.col("sec"), dtype=float)
        n = tick.size
        if n == 0:
            return np.zeros(0, dtype=float)
        cum_before = np.concatenate(([0.0], np.cumsum(tick)))[:n]   # sum(tick[:t])
        spread = sec - sec[0]
        valid = (np.arange(n) > 0) & (spread > 0)
        safe = np.where(spread > 0, spread, 1.0)
        return _carry_forward(cum_before / safe, valid)

    def stream(self) -> "_TickRateCumState":
        return _TickRateCumState()


class _TickRateCumState:
    __slots__ = ("_n", "_first_sec", "_cum_tick", "_last")

    def __init__(self) -> None:
        self._n = 0
        self._first_sec: Optional[int] = None
        self._cum_tick = 0.0
        self._last = 0.0

    def update(self, event: Mapping[str, Any]) -> float:
        sec = _event_sec(event)
        if self._first_sec is None:
            self._first_sec = sec
        if self._n > 0:
            spread = sec - self._first_sec
            if spread > 0:
                self._last = self._cum_tick / spread
        self._cum_tick += float(event["tick"])
        self._n += 1
        return self._last

    @property
    def ready(self) -> bool:
        return self._n > 1


def _event_sec(event: Mapping[str, Any]) -> int:
    """이벤트에서 당일 누적 초를 얻는다. sec 가 없으면 time(HHMMSS)에서 환산."""
    if "sec" in event:
        return int(event["sec"])
    return _to_seconds(event["time"])


# ===========================================================================
# 6. 구간 거래대금 (amt_{w}s / bamt_{w}s)
# ===========================================================================
#
#     t_ws = max(0, t - w)
#     vol_ws   = sum(stock['vol'][t_ws:t])
#     price_ws = np.mean(stock['close'][t_ws:t])
#     norm     = min(w, time_spread)
#     stock[f'amt_{w}s'] = vol_ws * price_ws / 10000 / norm
#
# 경계가 cbv 와 다르다. cbv 는 t > w 에서만 계산하지만 여기는 t >= 1 이면
# **부분 윈도우**로 계산한다 (t_ws = max(0, t-w)). 한 칸 맞추려다 여기를
# cbv 와 같게 만들면 장 초반 값이 통째로 달라진다.
# 단위: 만원 (/10000), 초당 정규화 (norm).
# ===========================================================================

class WindowAmount(MicroFeature):
    """구간 거래대금(만원/초). source='vol' 이면 amt_{w}s, 'buy_vol' 이면 bamt_{w}s."""

    version = "1.0.0"
    warmup = 1

    def __init__(self, window: int, source: str = "vol") -> None:
        if source not in ("vol", "buy_vol"):
            raise ValueError(f"source 는 vol 또는 buy_vol 이어야 합니다: {source}")
        self.window = int(window)
        self.source = source
        prefix = "amt" if source == "vol" else "bamt"
        self.name = f"{prefix}_{self.window}s"
        self.deps = (source, "close", "sec")

    def batch(self, ctx: BatchContext) -> np.ndarray:
        vol = np.asarray(ctx.col(self.source), dtype=float)
        close = np.asarray(ctx.col("close"), dtype=float)
        sec = np.asarray(ctx.col("sec"), dtype=float)
        n = vol.size
        w = self.window
        if n == 0:
            return np.zeros(0, dtype=float)

        t = np.arange(n)
        start = np.maximum(0, t - w)
        length = np.maximum(t - start, 1)                 # t=0 은 어차피 무효

        cum_vol = np.concatenate(([0.0], np.cumsum(vol)))
        cum_close = np.concatenate(([0.0], np.cumsum(close)))
        vol_ws = cum_vol[t] - cum_vol[start]
        price_ws = (cum_close[t] - cum_close[start]) / length

        spread = sec - sec[0]
        norm = np.minimum(w, np.where(spread > 0, spread, 1.0))
        valid = (t > 0) & (spread > 0)
        values = vol_ws * price_ws / 10000.0 / norm
        return _carry_forward(values, valid)

    def stream(self) -> "_WindowAmountState":
        return _WindowAmountState(self.window, self.source)


class _WindowAmountState:
    __slots__ = ("w", "source", "_vol", "_close", "_vol_sum", "_close_sum",
                 "_n", "_first_sec", "_last")

    def __init__(self, window: int, source: str) -> None:
        self.w = int(window)
        self.source = source
        self._vol: deque[float] = deque(maxlen=self.w)
        self._close: deque[float] = deque(maxlen=self.w)
        self._vol_sum = 0.0
        self._close_sum = 0.0
        self._n = 0
        self._first_sec: Optional[int] = None
        self._last = 0.0

    def update(self, event: Mapping[str, Any]) -> float:
        sec = _event_sec(event)
        if self._first_sec is None:
            self._first_sec = sec

        if self._n > 0:
            spread = sec - self._first_sec
            if spread > 0:
                count = len(self._vol)                    # == min(t, w)
                price = self._close_sum / count
                norm = min(self.w, spread)
                self._last = self._vol_sum * price / 10000.0 / norm

        v = float(event[self.source])
        c = float(event["close"])
        if len(self._vol) == self.w:
            self._vol_sum -= self._vol[0]
            self._close_sum -= self._close[0]
        self._vol.append(v)
        self._close.append(c)
        self._vol_sum += v
        self._close_sum += c
        self._n += 1
        return self._last

    @property
    def ready(self) -> bool:
        return self._n > 1


# ===========================================================================
# 7. 기준가 대비 구간 고저 괴리율 (trigger_dev_min_{w} / trigger_dev_max_{w})
# ===========================================================================
#
#     min_price_w = min(stock['low'][t - w: t])
#     max_price_w = max(stock['high'][t - w: t])
#     stock[f'min{w}_trigger'] = round((trigger / min_price_w - 1) * 100, 3)
#     stock[f'max{w}_trigger'] = round((trigger / max_price_w - 1) * 100, 3)
#
# trigger = 당일 시가. 값이 양수면 그 구간 고/저가가 시가보다 낮았다는 뜻이다.
#
# 여기가 §3.7 이 지목한 최적화 지점이다. 레거시는 매 t 마다 윈도우를 통째로
# 재스캔한다(O(w)). 단조 덱으로 amortized O(1) 이 되고, 같은 클래스를
# 배치와 스트리밍이 함께 쓰므로 두 구현이 어긋날 수 없다.
# ===========================================================================

class TriggerDev(MicroFeature):
    """당일 시가 대비 [t-w, t) 구간 최저가(min) / 최고가(max) 괴리율 (%)."""

    version = "1.0.0"

    def __init__(self, window: int, mode: str = "min") -> None:
        if mode not in ("min", "max"):
            raise ValueError(f"mode 는 min 또는 max 여야 합니다: {mode}")
        self.window = int(window)
        self.mode = mode
        self.field = "low" if mode == "min" else "high"
        self.name = f"trigger_dev_{mode}_{self.window}"
        self.warmup = self.window + 1
        self.deps = (self.field, "open")

    def batch(self, ctx: BatchContext) -> np.ndarray:
        src = np.asarray(ctx.col(self.field), dtype=float)
        trigger = float(np.asarray(ctx.col("open"), dtype=float)[0])
        n = src.size

        window = _MonotonicWindow(self.window, is_min=(self.mode == "min"))
        values = np.zeros(n, dtype=float)
        valid = np.zeros(n, dtype=bool)
        for t in range(n):
            extreme = window.current()
            if extreme is not None and extreme > 0:
                values[t] = _round3((trigger / extreme - 1) * 100)
                valid[t] = True
            window.push(src[t])
        return _carry_forward(values, valid)

    def stream(self) -> "_TriggerDevState":
        return _TriggerDevState(self.window, self.mode, self.field)


class _TriggerDevState:
    __slots__ = ("field", "_window", "_trigger", "_last")

    def __init__(self, window: int, mode: str, field: str) -> None:
        self.field = field
        self._window = _MonotonicWindow(window, is_min=(mode == "min"))
        self._trigger: Optional[float] = None
        self._last = 0.0

    def update(self, event: Mapping[str, Any]) -> float:
        if self._trigger is None:
            self._trigger = float(event["open"])
        extreme = self._window.current()
        if extreme is not None and extreme > 0:
            self._last = _round3((self._trigger / extreme - 1) * 100)
        self._window.push(float(event[self.field]))
        return self._last

    @property
    def ready(self) -> bool:
        return self._window.current() is not None


# ===========================================================================
# 8. 최근 N틱 매수 비중 (buy_ratio_15t)
# ===========================================================================
#
# engine/nxt_tick_engine.py 대응 코드:
#     recent_15_ticks.append(tick)            # 현재 틱을 먼저 넣고
#     if len(recent_15_ticks) > 15: pop(0)
#     buy_ratio = buy_vol_15 / tot_vol_15
#
# 윈도우 경계가 cbv 와 반대다. 현재 틱을 **포함**한 (t-N, t] 구간이다.
# 엔진이 그렇게 쓰고 있으므로 여기서도 그대로 맞춘다. 현재까지의 정보만
# 쓰므로 룩어헤드는 아니다.
# 거래량이 0 이면 0.0 (직전 값을 끌고 가지 않는다 — 엔진과 동일).
# ===========================================================================

class RecentBuyRatio(MicroFeature):
    """최근 N개 이벤트의 매수 체결 비중. 현재 이벤트를 포함한다."""

    version = "1.0.0"
    warmup = 0
    resolution = "tick"

    def __init__(self, window: int = 15, buy_field: str = "buy_vol", total_field: str = "vol") -> None:
        self.window = int(window)
        self.buy_field = buy_field
        self.total_field = total_field
        self.name = f"buy_ratio_{self.window}t"
        self.deps = (buy_field, total_field)

    def batch(self, ctx: BatchContext) -> np.ndarray:
        buy = np.asarray(ctx.col(self.buy_field), dtype=float)
        total = np.asarray(ctx.col(self.total_field), dtype=float)
        n = buy.size
        if n == 0:
            return np.zeros(0, dtype=float)

        cum_buy = np.concatenate(([0.0], np.cumsum(buy)))
        cum_total = np.concatenate(([0.0], np.cumsum(total)))
        end = np.arange(n) + 1                                  # 현재 포함
        start = np.maximum(0, end - self.window)
        buy_sum = cum_buy[end] - cum_buy[start]
        total_sum = cum_total[end] - cum_total[start]
        return np.divide(buy_sum, total_sum, out=np.zeros(n, dtype=float), where=total_sum > 0)

    def stream(self) -> "_RecentBuyRatioState":
        return _RecentBuyRatioState(self.window, self.buy_field, self.total_field)


class _RecentBuyRatioState:
    __slots__ = ("w", "buy_field", "total_field", "_buy", "_total", "_buy_sum", "_total_sum", "_n")

    def __init__(self, window: int, buy_field: str, total_field: str) -> None:
        self.w = int(window)
        self.buy_field = buy_field
        self.total_field = total_field
        self._buy: deque[float] = deque(maxlen=self.w)
        self._total: deque[float] = deque(maxlen=self.w)
        self._buy_sum = 0.0
        self._total_sum = 0.0
        self._n = 0

    def update(self, event: Mapping[str, Any]) -> float:
        b = float(event[self.buy_field])
        v = float(event[self.total_field])
        if len(self._buy) == self.w:                # 현재 이벤트를 먼저 넣는다
            self._buy_sum -= self._buy[0]
            self._total_sum -= self._total[0]
        self._buy.append(b)
        self._total.append(v)
        self._buy_sum += b
        self._total_sum += v
        self._n += 1
        return self._buy_sum / self._total_sum if self._total_sum > 0 else 0.0

    @property
    def ready(self) -> bool:
        return self._n > 0


# ===========================================================================
# 9. 호가단위 비율 (tick_size_ratio)
# ===========================================================================
#
# strategy.py 는 stock['tick_rate'] = 0.1 상수를 쓰고 진입 조건에서
# `stock['tick_rate'] <= 0.18` 로 비교한다. engine/utils.calculate_ticksize()
# 가 이미 있는데 호출되지 않는 상태다 (§1.6).
#
# 이 피처는 레거시 상수(0.1)와 **값이 다르다.** 상수를 실제 계산으로 바꾸는
# 것은 전략 거동을 바꾸는 일이라 Phase C(전략 추상화)에서 파라미터와 함께
# 다룬다. 여기서는 올바른 값을 계산해 두기만 한다.
# ===========================================================================

class TickSizeRatio(MicroFeature):
    """호가단위 / 현재가 (%). 한 틱이 몇 % 인지 — 슬리피지 하한선."""

    name = "tick_size_ratio"
    version = "1.0.0"
    deps = ("close",)
    warmup = 0

    def __init__(self, date: Optional[str] = None) -> None:
        self._date = date          # 호가단위 개편(2023-01-25) 판정용

    def bind(self, date: str) -> "TickSizeRatio":
        """
        기준 날짜를 고정한다. batch() 는 BatchContext 에서 자동으로 부른다.

        날짜 출처를 **한 곳으로 묶는 것**이 핵심이다. 스트리밍이 이벤트의 date 를
        따로 읽게 두면, 배치가 보는 날짜와 스트리밍이 보는 날짜가 갈리는 순간
        2023-01-25 호가 개편을 사이에 두고 값이 5배까지 달라진다. 실제로 이
        구멍이 패리티 테스트에서 0.1 vs 0.5 로 잡혔다.
        """
        self._date = date
        return self

    def _require_date(self) -> str:
        if not self._date:
            raise ValueError(
                "tick_size_ratio 는 기준 날짜가 필요합니다 (2023-01-25 호가 개편). "
                "batch() 를 먼저 부르거나 bind('YYYYMMDD') 로 지정하세요."
            )
        return self._date

    def batch(self, ctx: BatchContext) -> np.ndarray:
        self.bind(ctx.date)
        close = np.asarray(ctx.col("close"), dtype=float)
        date = self._require_date()
        sizes = np.array([calculate_ticksize(p, date) for p in close], dtype=float)
        return np.divide(sizes, close, out=np.zeros_like(close), where=close > 0) * 100.0

    def stream(self) -> "_TickSizeRatioState":
        return _TickSizeRatioState(self._require_date())


class _TickSizeRatioState:
    """날짜는 생성 시점에 고정된다. 이벤트의 date 는 보지 않는다."""

    __slots__ = ("_date", "_seen")

    def __init__(self, date: str) -> None:
        self._date = date
        self._seen = False

    def update(self, event: Mapping[str, Any]) -> float:
        self._seen = True
        close = float(event["close"])
        if close <= 0:
            return 0.0
        return calculate_ticksize(close, self._date) / close * 100.0

    @property
    def ready(self) -> bool:
        return self._seen


# ===========================================================================
# 표준 피처셋 구성 예시
# ===========================================================================

def default_micro_features() -> list[MicroFeature]:
    """fs_v1 에 들어갈 미시 피처 목록 (ARCHITECTURE_V2.md §3.7 매핑표 전체)."""
    features: list[MicroFeature] = []
    features += [RollingBuyVolMean(w) for w in MICRO_WINDOWS]
    features += [CbvRatioMax(w) for w in MICRO_WINDOWS]
    features += [CbvRatioTmax(w) for w in MICRO_WINDOWS]
    features += [BuyVolOnOpenBreak(), CbvRatioTmax1()]
    features += [TickRateCum()]
    features += [WindowAmount(w, source="vol") for w in MICRO_WINDOWS]
    features += [WindowAmount(w, source="buy_vol") for w in MICRO_WINDOWS]
    features += [TriggerDev(w, mode="min") for w in TRIGGER_WINDOWS]
    features += [TriggerDev(w, mode="max") for w in TRIGGER_WINDOWS]
    features += [OrderBookImbalance(), RecentBuyRatio(), TickSizeRatio()]
    return features
