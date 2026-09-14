"""
features/builders/microstructure.py — 미시구조 피처 (Phase B/D)

이 파일은 **패턴 데모**다. 두 개의 피처가 batch/stream 이중 구현으로 완전히
작성되어 있고, tests/test_feature_parity.py 가 둘의 일치를 실제로 검증한다.
나머지 피처는 이 패턴을 따라 ARCHITECTURE_V2.md §3.7 의 매핑표대로 이식한다.

이식 대상 (engine/strategy.py -> 여기):
    cbv_{5,10,30,60}        ✅ 아래 RollingBuyVolMean 로 구현됨
    obi_top3                ✅ 아래 OrderBookImbalance 로 구현됨
    cbv_ratio_max_{w}       TODO — 누적 최대값 상태 유지
    cbv_ratio_tmax_{w}      TODO — 조건부(고가>=시가) 누적 최대
    tick_rate_cum           TODO — 누적 틱수 / 경과초
    amt_{w}s, bamt_{w}s     TODO — rolling sum x 평균가
    trigger_dev_{min,max}_{w}  TODO — ⭐ monotonic deque 로 O(1) 화
                               (현재 strategy.py 는 매 t 마다 min()/max() 재스캔)
    buy_ratio_15t           TODO — 고정 길이 deque
"""

from __future__ import annotations

from collections import deque
from typing import Any, Mapping

import numpy as np

from features.base import BatchContext, MicroFeature

__all__ = ["RollingBuyVolMean", "OrderBookImbalance"]


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
# 표준 피처셋 구성 예시
# ===========================================================================

def default_micro_features() -> list[MicroFeature]:
    """fs_v1 에 들어갈 미시 피처 목록 (Phase B 에서 확장)."""
    return [
        *(RollingBuyVolMean(w) for w in (5, 10, 30, 60)),
        OrderBookImbalance(),
    ]
