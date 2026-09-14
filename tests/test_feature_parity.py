"""
tests/test_feature_parity.py — 피처 레이어의 두 가지 필수 검증

이 두 테스트는 '있으면 좋은' 테스트가 아니다. 실전 투입의 전제 조건이다.

  1) 패리티 테스트   batch 와 stream 이 같은 값을 내는가
                    -> 아니면 백테스트 결과를 실전에서 믿을 수 없다
  2) 룩어헤드 테스트  t 시점 피처가 t 이후 데이터를 쓰지 않는가
                    -> 새면 백테스트 전체가 무의미해진다

실행:
    uv run pytest tests/test_feature_parity.py -v

참고: ARCHITECTURE_V2.md §3.3, §3.4
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from features.base import BatchContext, Feature                      # noqa: E402
from features.builders.microstructure import (                       # noqa: E402
    OrderBookImbalance,
    RollingBuyVolMean,
)


# ---------------------------------------------------------------------------
# 재사용 가능한 검증 하네스
# ---------------------------------------------------------------------------

def assert_parity(feature: Feature, events: list[dict], *, tol: float = 1e-9) -> None:
    """
    같은 하루 데이터에 대해 batch 와 stream 이 같은 값을 내는지 검증.

    이 테스트가 통과하지 않는 피처는 실전에 올릴 수 없다.
    """
    ctx = _ctx_from_events(feature, events)
    batch_out = np.asarray(feature.batch(ctx), dtype=float)

    state = feature.stream()
    stream_out = np.array([state.update(e) for e in events], dtype=float)

    assert batch_out.shape == stream_out.shape, (
        f"{feature.name}: 길이 불일치 batch={batch_out.shape} stream={stream_out.shape}"
    )

    w = feature.warmup
    np.testing.assert_allclose(
        batch_out[w:], stream_out[w:], rtol=tol, atol=tol,
        err_msg=f"{feature.name}: batch/stream 값 불일치 (warmup={w} 이후 구간)",
    )


def assert_no_lookahead(
    feature: Feature, events: list[dict], sample_points: list[int], *, tol: float = 1e-9
) -> None:
    """
    t 시점까지 잘라서 계산한 마지막 값 == 전체로 계산한 t 시점 값.

    다르면 피처가 미래 데이터를 보고 있다는 뜻이다.
    """
    full = np.asarray(feature.batch(_ctx_from_events(feature, events)), dtype=float)

    for t in sample_points:
        if t < feature.warmup:
            continue
        truncated = np.asarray(
            feature.batch(_ctx_from_events(feature, events[: t + 1])), dtype=float
        )
        assert truncated[-1] == pytest.approx(full[t], rel=tol, abs=tol), (
            f"{feature.name}: t={t} 에서 룩어헤드 감지 "
            f"(잘라서 계산={truncated[-1]}, 전체에서 읽음={full[t]})"
        )


def _ctx_from_events(feature: Feature, events: list[dict]) -> BatchContext:
    """이벤트 딕셔너리 목록 -> BatchContext (테스트 편의용)."""
    if not events:
        raise ValueError("빈 이벤트 목록")
    columns = {
        key: np.array([e[key] for e in events], dtype=float)
        for key in events[0]
        if isinstance(events[0][key], (int, float))
    }
    return BatchContext(code="TEST", date="20260911", columns=columns)


# ---------------------------------------------------------------------------
# 테스트 데이터
# ---------------------------------------------------------------------------

@pytest.fixture
def synthetic_events() -> list[dict]:
    """재현 가능한 합성 1초봉 이벤트 (하루 분량의 축소판)."""
    rng = np.random.default_rng(seed=20260911)
    n = 500
    return [
        {
            "buy_vol": float(rng.integers(0, 5000)),
            "sell_vol": float(rng.integers(0, 5000)),
            "bid_v_top3": float(rng.integers(1, 20000)),
            "ask_v_top3": float(rng.integers(1, 20000)),
        }
        for _ in range(n)
    ]


@pytest.fixture
def edge_events() -> list[dict]:
    """경계 케이스: 0 잔량, 0 거래량이 섞인 구간."""
    return [
        {"buy_vol": 0.0, "sell_vol": 0.0, "bid_v_top3": 0.0, "ask_v_top3": 0.0}
        for _ in range(40)
    ] + [
        {"buy_vol": 100.0, "sell_vol": 50.0, "bid_v_top3": 500.0, "ask_v_top3": 0.0}
        for _ in range(40)
    ]


# ---------------------------------------------------------------------------
# 패리티
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("window", [5, 10, 30, 60])
def test_cbv_parity(synthetic_events, window):
    assert_parity(RollingBuyVolMean(window), synthetic_events)


def test_obi_parity(synthetic_events):
    assert_parity(OrderBookImbalance(), synthetic_events)


@pytest.mark.parametrize("window", [5, 10])
def test_cbv_parity_on_edge_cases(edge_events, window):
    assert_parity(RollingBuyVolMean(window), edge_events)


def test_obi_parity_on_zero_ask(edge_events):
    """매도 잔량 0 일 때 배치는 0, 스트림도 0 이어야 한다 (division by zero 방어)."""
    assert_parity(OrderBookImbalance(), edge_events)


# ---------------------------------------------------------------------------
# 룩어헤드
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("window", [5, 10, 30])
def test_cbv_no_lookahead(synthetic_events, window):
    assert_no_lookahead(
        RollingBuyVolMean(window), synthetic_events,
        sample_points=[70, 120, 250, 400, 499],
    )


def test_obi_no_lookahead(synthetic_events):
    assert_no_lookahead(
        OrderBookImbalance(), synthetic_events,
        sample_points=[0, 50, 250, 499],
    )


# ---------------------------------------------------------------------------
# 윈도우 경계 규칙 — [t-w, t) 로 현재 시점을 제외하는가
# ---------------------------------------------------------------------------

def test_cbv_window_excludes_current_bar():
    """
    현재 시점이 자기 자신의 지표에 반영되면 룩어헤드다.
    engine/strategy.py 의 sum(buy_vol[t-w:t]) 경계 규칙과 일치해야 한다.
    """
    w = 3
    events = [{"buy_vol": float(v)} for v in [0, 0, 0, 0, 9999, 1, 1, 1]]
    feature = RollingBuyVolMean(w)
    out = feature.batch(_ctx_from_events(feature, events))

    # t=4 의 값은 [1,2,3] 구간 -> 전부 0. 9999 가 섞이면 안 된다.
    assert out[4] == pytest.approx(0.0)
    # t=5 에서 비로소 9999 가 윈도우 [2,3,4] 에 들어온다.
    assert out[5] == pytest.approx(9999 / w)


def test_warmup_values_are_not_trusted():
    """warmup 이전 구간은 0 이고, stream 의 ready 도 False 여야 한다."""
    w = 10
    events = [{"buy_vol": 100.0} for _ in range(30)]
    feature = RollingBuyVolMean(w)

    out = feature.batch(_ctx_from_events(feature, events))
    assert np.all(out[: w + 1] == 0.0)

    state = feature.stream()
    for i, e in enumerate(events):
        state.update(e)
        assert state.ready == (i + 1 > w), f"i={i} 에서 ready 상태가 잘못됨"
