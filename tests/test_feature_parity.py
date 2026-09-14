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


# ===========================================================================
# Phase B — §3.7 매핑표로 이식한 피처 전체 검증
#
# 위의 두 피처(cbv_w, obi_top3)에 쓰던 하네스를 그대로 쓴다. 피처가 늘어도
# 검증 방식은 늘지 않는다 — 그게 이중 구현 계약을 클래스 하나에 묶어둔 이유다.
# ===========================================================================

from features import registry                                        # noqa: E402
from features.base import MacroFeature                               # noqa: E402
from features.builders.macro import DailyMatrix                      # noqa: E402
from features.builders.microstructure import (                       # noqa: E402
    MICRO_WINDOWS,
    TRIGGER_WINDOWS,
    BuyVolOnOpenBreak,
    CbvRatioMax,
    CbvRatioTmax,
    CbvRatioTmax1,
    RecentBuyRatio,
    TickRateCum,
    TickSizeRatio,
    TriggerDev,
    WindowAmount,
    _MonotonicWindow,
    _round3,
)


@pytest.fixture
def bar_events() -> list[dict]:
    """
    재현 가능한 합성 1초봉. 실제 LOB 처럼 **1초 간격이 아니다.**

    체결이 있을 때만 행이 생기므로 시각은 불규칙하게 뛴다. tick_rate_cum 과
    amt_{w}s 는 '행 개수' 윈도우와 '시계 초' 분모를 함께 쓰기 때문에, 간격이
    일정한 데이터로만 검증하면 그 차이에서 오는 버그를 놓친다.
    """
    rng = np.random.default_rng(seed=20220425)
    n = 400
    base = 78700.0
    events, sec = [], 32400            # 09:00:00
    for i in range(n):
        sec += int(rng.integers(1, 4))          # 1~3초씩 불규칙하게 전진
        close = base + float(rng.integers(-300, 300))
        spread = float(rng.integers(0, 200))
        events.append({
            "time": f"{sec // 3600:02d}{(sec % 3600) // 60:02d}{sec % 60:02d}",
            "sec": float(sec),
            "date": "20220425",
            "open": base if i == 0 else close - float(rng.integers(-50, 50)),
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "vol": float(rng.integers(0, 5000)),
            "buy_vol": float(rng.integers(0, 3000)),
            "sell_vol": float(rng.integers(0, 3000)),
            "tick": float(rng.integers(0, 12)),
            "bid_v_top3": float(rng.integers(1, 20000)),
            "ask_v_top3": float(rng.integers(1, 20000)),
        })
    return events


@pytest.fixture
def flat_events() -> list[dict]:
    """경계 케이스: 거래량 0, 같은 시각이 이어지는 구간, 가격 고정."""
    return [
        {
            "time": "090000", "sec": 32400.0, "date": "20220425",
            "open": 1000.0, "high": 1000.0, "low": 1000.0, "close": 1000.0,
            "vol": 0.0, "buy_vol": 0.0, "sell_vol": 0.0, "tick": 0.0,
            "bid_v_top3": 0.0, "ask_v_top3": 0.0,
        }
        for _ in range(40)
    ] + [
        {
            "time": "090100", "sec": 32460.0, "date": "20220425",
            "open": 1000.0, "high": 1100.0, "low": 900.0, "close": 1050.0,
            "vol": 500.0, "buy_vol": 400.0, "sell_vol": 100.0, "tick": 3.0,
            "bid_v_top3": 500.0, "ask_v_top3": 0.0,
        }
        for _ in range(40)
    ]


def _all_micro_features():
    """레지스트리에 등록된 미시 피처 전체 (거시는 하루 상수라 따로 검증)."""
    registry.bootstrap()
    return [f for f in registry.all_features().values() if not isinstance(f, MacroFeature)]


def _feature_ids(features):
    return [f.name for f in features]


MICRO_FEATURES = _all_micro_features()


# ---------------------------------------------------------------------------
# 패리티 — 이식한 피처 전부
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("feature", MICRO_FEATURES, ids=_feature_ids(MICRO_FEATURES))
def test_every_micro_feature_has_parity(feature, bar_events):
    """batch 와 stream 이 같은 값을 내지 않으면 실전에 올릴 수 없다."""
    assert_parity(feature, bar_events)


@pytest.mark.parametrize("feature", MICRO_FEATURES, ids=_feature_ids(MICRO_FEATURES))
def test_every_micro_feature_has_parity_on_edges(feature, flat_events):
    """거래량 0, 시각 정체, 가격 고정 구간에서도 두 구현이 같아야 한다."""
    assert_parity(feature, flat_events)


@pytest.mark.parametrize("feature", MICRO_FEATURES, ids=_feature_ids(MICRO_FEATURES))
def test_every_micro_feature_has_no_lookahead(feature, bar_events):
    """t 시점 값이 t 이후 데이터를 쓰면 백테스트 전체가 무의미해진다."""
    assert_no_lookahead(feature, bar_events, sample_points=[80, 150, 260, 399])


# ---------------------------------------------------------------------------
# 윈도우 경계 — 피처마다 경계가 다르다. 한 칸 어긋나면 룩어헤드다
# ---------------------------------------------------------------------------

def test_cbv_ratio_max_is_one_at_new_high():
    """최대값을 현재 값으로 먼저 갱신하므로 신고가 시점의 비율은 정확히 1.0."""
    w = 3
    buy = [0, 0, 0, 0, 10, 10, 10, 500, 500, 500, 1, 1, 1]
    events = [
        {"buy_vol": float(v), "high": 100.0, "open": 100.0, "close": 100.0,
         "low": 100.0, "vol": float(v), "tick": 1.0, "sec": float(32400 + i)}
        for i, v in enumerate(buy)
    ]
    feature = CbvRatioMax(w)
    out = feature.batch(_ctx_from_events(feature, events))
    assert out.max() == pytest.approx(1.0)
    assert out.max() <= 1.0


def test_cbv_ratio_tmax_carries_forward_when_condition_fails():
    """
    조건(고가 >= 시가)이 깨진 t 에서는 갱신되지 않고 **직전 값이 남는다.**
    레거시 딕셔너리가 그렇게 동작하므로 전략이 보는 숫자가 그 값이다.
    """
    w = 2
    events = []
    for i in range(12):
        high = 100.0 if i < 8 else 50.0          # 8번째부터 시가 아래로
        events.append({
            "buy_vol": float(10 * (i + 1)), "high": high, "open": 100.0,
            "low": high, "close": high, "vol": 1.0, "tick": 1.0, "sec": float(32400 + i),
        })
    feature = CbvRatioTmax(w)
    out = feature.batch(_ctx_from_events(feature, events))

    # 조건이 깨진 구간의 값은 직전 값 그대로여야 한다
    assert out[8] == out[7]
    assert np.all(out[8:] == out[7])


def test_cbv_1_resets_to_zero_instead_of_carrying():
    """cbv_1 만은 carry forward 가 아니다 — 레거시가 매 t 마다 0 으로 리셋한다."""
    events = [
        {"buy_vol": 100.0, "high": 100.0 if i % 2 == 0 else 50.0, "open": 100.0,
         "low": 50.0, "close": 100.0, "vol": 1.0, "tick": 1.0, "sec": float(32400 + i)}
        for i in range(10)
    ]
    feature = BuyVolOnOpenBreak()
    out = feature.batch(_ctx_from_events(feature, events))
    assert out[0] == 0.0                 # t=0 은 언제나 0
    assert out[2] == 100.0               # 고가 >= 시가
    assert out[3] == 0.0                 # 조건 불충족 -> 직전 값이 아니라 0


def test_window_amount_uses_partial_window_early():
    """
    amt_{w}s 는 cbv 와 경계가 다르다. t >= 1 이면 부분 윈도우로 계산한다
    (t_ws = max(0, t-w)). cbv 처럼 t > w 를 요구하면 장 초반이 통째로 달라진다.
    """
    w = 10
    events = [
        {"vol": 100.0, "buy_vol": 50.0, "close": 1000.0, "high": 1000.0,
         "low": 1000.0, "open": 1000.0, "tick": 1.0, "sec": float(32400 + i)}
        for i in range(5)
    ]
    feature = WindowAmount(w, source="vol")
    out = feature.batch(_ctx_from_events(feature, events))

    assert out[0] == 0.0                      # t=0 은 무효
    # t=1: 윈도우 [0,1) -> vol 100, 평균가 1000, norm=min(10, 경과 1초)=1
    assert out[1] == pytest.approx(100 * 1000 / 10000 / 1)
    # t=3: 윈도우 [0,3) -> vol 300, norm=min(10, 3)=3
    assert out[3] == pytest.approx(300 * 1000 / 10000 / 3)


def test_trigger_dev_requires_full_window():
    """t > w 에서만 유효. t == w 는 아직 아니다 (레거시가 그렇다)."""
    w = 3
    events = [
        {"low": 100.0 + i, "high": 200.0 + i, "open": 150.0, "close": 150.0,
         "vol": 1.0, "buy_vol": 1.0, "tick": 1.0, "sec": float(32400 + i)}
        for i in range(8)
    ]
    feature = TriggerDev(w, mode="min")
    out = feature.batch(_ctx_from_events(feature, events))
    assert np.all(out[: w + 1] == 0.0)        # t <= w 는 미갱신 -> 0
    assert out[w + 1] != 0.0


def test_trigger_dev_matches_naive_scan():
    """
    단조 덱이 레거시의 min()/max() 재스캔과 같은 값을 내는지 직접 비교한다.
    O(1) 로 바꾸면서 값이 달라지면 최적화가 아니라 버그다.
    """
    rng = np.random.default_rng(7)
    n, w = 200, 10
    lows = rng.integers(900, 1100, n).astype(float)
    highs = lows + rng.integers(1, 50, n)
    trigger = 1000.0
    events = [
        {"low": lows[i], "high": highs[i], "open": trigger, "close": lows[i],
         "vol": 1.0, "buy_vol": 1.0, "tick": 1.0, "sec": float(32400 + i)}
        for i in range(n)
    ]

    for mode, source in (("min", lows), ("max", highs)):
        feature = TriggerDev(w, mode=mode)
        fast = feature.batch(_ctx_from_events(feature, events))

        naive = np.zeros(n)
        last = 0.0
        for t in range(n):
            if t > w:
                extreme = (min if mode == "min" else max)(source[t - w: t])
                if extreme > 0:
                    last = _round3((trigger / extreme - 1) * 100)
            naive[t] = last
        np.testing.assert_array_equal(fast, naive, err_msg=f"{mode} 윈도우 불일치")


def test_monotonic_window_is_amortized_o1():
    """덱에 남는 원소 수가 윈도우 크기를 넘지 않는다 (재스캔이 아니라는 증거)."""
    window = _MonotonicWindow(5, is_min=True)
    rng = np.random.default_rng(1)
    for value in rng.integers(0, 100, 500):
        window.current()
        window.push(float(value))
        assert len(window._dq) <= 5


def test_recent_buy_ratio_includes_current_event():
    """
    nxt 틱 엔진은 현재 틱을 먼저 큐에 넣고 비중을 구한다. 그 경계를 그대로 따른다.
    (현재까지의 정보만 쓰므로 룩어헤드는 아니다)
    """
    events = [
        {"buy_vol": 0.0, "vol": 100.0, "close": 1000.0, "high": 1000.0, "low": 1000.0,
         "open": 1000.0, "tick": 1.0, "sec": float(32400 + i)}
        for i in range(3)
    ]
    events.append({"buy_vol": 100.0, "vol": 100.0, "close": 1000.0, "high": 1000.0,
                   "low": 1000.0, "open": 1000.0, "tick": 1.0, "sec": 32403.0})
    feature = RecentBuyRatio(window=15)
    out = feature.batch(_ctx_from_events(feature, events))
    # 마지막 이벤트가 포함되어야 400 중 100 = 0.25
    assert out[-1] == pytest.approx(0.25)


def test_tick_rate_cum_excludes_current_bar():
    """분자는 sum(tick[:t]) — 현재 봉을 제외한다."""
    events = [
        {"tick": 10.0, "sec": float(32400 + i), "vol": 1.0, "buy_vol": 1.0,
         "close": 1000.0, "high": 1000.0, "low": 1000.0, "open": 1000.0}
        for i in range(5)
    ]
    feature = TickRateCum()
    out = feature.batch(_ctx_from_events(feature, events))
    # t=2: 경과 2초, 이전 틱 합 20 -> 10.0
    assert out[2] == pytest.approx(20 / 2)


# ---------------------------------------------------------------------------
# 반올림 — 실제로 잡혔던 불일치의 회귀 테스트
# ---------------------------------------------------------------------------

def test_rounding_matches_numpy_not_python():
    """
    레거시는 numpy float64 끼리 계산한 뒤 round 를 부른다 -> numpy 반올림.
    스트리밍에서 Python float 으로 바꿔 계산하면 0.2375 같은 경계값이 갈린다.

    실제로 20220425/000270 t=6531 에서 cbv_ratio_tmax_10 이
    레거시 0.238 vs 스트리밍 0.237 로 어긋났다. 에러가 아니라 '값 하나가
    0.001 다른' 형태라 테스트 없이는 영원히 못 잡는다.
    """
    assert _round3(0.2375) == float(np.round(0.2375, 3))
    assert _round3(98.8 / 416.0) == pytest.approx(0.238)


def test_batch_and_stream_round_identically(bar_events):
    """반올림이 들어가는 피처들은 배치와 스트리밍이 비트 단위로 같아야 한다."""
    for feature in (CbvRatioMax(10), CbvRatioTmax(10), CbvRatioTmax1(),
                    TriggerDev(10, "min"), TriggerDev(10, "max")):
        ctx = _ctx_from_events(feature, bar_events)
        batch_out = np.asarray(feature.batch(ctx), dtype=float)
        state = feature.stream()
        stream_out = np.array([state.update(e) for e in bar_events], dtype=float)
        np.testing.assert_array_equal(
            batch_out, stream_out, err_msg=f"{feature.name}: 반올림이 갈린다"
        )


# ---------------------------------------------------------------------------
# 거시 피처 — 시점 규칙(point-in-time)이 본체다
# ---------------------------------------------------------------------------

@pytest.fixture
def tiny_daily(tmp_path) -> DailyMatrix:
    """3일치 일봉 매트릭스를 직접 만든다 (CP949, 0행은 종목명)."""
    rows = [
        ["Name", "테스트"],
        ["20220420", "100"],
        ["20220421", "200"],
        ["20220422", "300"],
        ["20220425", "999"],          # 당일 — 절대 새어 나오면 안 되는 값
    ]
    for matrix in ("close", "mkt", "float", "tradamt", "open"):
        lines = ["Code,A000001"] + [f"{d},{v}" for d, v in rows]
        (tmp_path / f"{matrix}.csv").write_text("\n".join(lines), encoding="CP949")
    return DailyMatrix(tmp_path)


def test_macro_never_sees_today(tiny_daily):
    """
    당일 행은 어떤 경로로도 나가지 않는다. 당일 종가 기준 시총으로 당일 진입을
    거르면 그 자체가 룩어헤드다 (§3.6).
    """
    rows = tiny_daily.prior_rows("close", "000001", "20220425", n=10)
    assert 999.0 not in rows
    assert list(rows) == [100.0, 200.0, 300.0]
    assert tiny_daily.prior_value("close", "000001", "20220425") == 300.0


def test_macro_prior_rows_are_bounded_by_date(tiny_daily):
    assert list(tiny_daily.prior_rows("close", "000001", "20220421")) == [100.0]
    assert tiny_daily.prior_rows("close", "000001", "20220420").size == 0


def test_macro_features_are_constant_over_the_day(tiny_daily, bar_events):
    """하루 중 값이 변하지 않는다 -> batch 는 상수 배열, stream 은 상수 상태."""
    from features.builders.macro import AvgTradeAmount, MarketCap

    for feature in (MarketCap(str(tiny_daily.csv_path)), AvgTradeAmount(20, str(tiny_daily.csv_path))):
        feature.bind("000001", "20220425")
        ctx = _ctx_from_events(feature, bar_events)
        ctx.code, ctx.date = "000001", "20220425"
        out = feature.batch(ctx)
        assert len(out) == len(bar_events)
        assert len(set(out.tolist())) == 1

        state = feature.stream()
        assert state.update(bar_events[0]) == pytest.approx(out[0])
        assert state.ready is True


def test_avg_trade_amount_averages_prior_days_only(tiny_daily):
    from features.builders.macro import AvgTradeAmount

    feature = AvgTradeAmount(3, str(tiny_daily.csv_path)).bind("000001", "20220425")
    assert feature._daily_value() == pytest.approx((100 + 200 + 300) / 3)


def test_unknown_code_returns_nan_instead_of_guessing(tiny_daily):
    """모르는 종목에 0 을 돌려주면 필터가 조용히 통과한다. NaN 이어야 한다."""
    from features.builders.macro import MarketCap

    feature = MarketCap(str(tiny_daily.csv_path)).bind("999999", "20220425")
    assert np.isnan(feature._daily_value())
