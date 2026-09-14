from typing import Mapping

import numpy as np
from engine.utils import calculate_time_spread

# ---------------------------------------------------------------------------
# 이 전략이 피처 스토어에서 읽는 피처 (Phase B-1)
# ---------------------------------------------------------------------------
# calculate_window_metrics() 는 42종을 계산하지만 진입/청산/결과기록이 실제로
# 읽는 값은 아래 4개뿐이다. FeatureStore.read(names=...) 에 이 4개만 넘기면
# parquet 이 나머지 38개 컬럼은 디스크에서 꺼내지도 않는다 — 컬럼 선택 읽기가
# 피처 스토어를 택한 이유다 (ARCHITECTURE_V2.md §3.5).
#
#   피처 이름(fs_v1) -> 레거시 stock 딕셔너리 키 (§3.7 매핑표)
#
# 여기 없는 피처를 전략이 새로 쓰게 되면 이 표에 추가해야 한다. 표에 없는 키는
# 스토어 경로에서 영원히 0 이므로, 빠뜨리면 조건이 조용히 거짓이 된다.
STORE_FEATURE_KEYS: dict[str, str] = {
    "cbv_1": "cbv_1",                       # 결과 기록 (진입 시점 스냅샷)
    "tick_rate_cum": "ctotal",              # check_entry_conditions + 결과 기록
    "amt_10s": "amt_10s",                   # check_entry_conditions
    "trigger_dev_max_10": "max10_trigger",  # check_entry_conditions
}

REQUIRED_FEATURES: tuple[str, ...] = tuple(STORE_FEATURE_KEYS)


def apply_stored_metrics(stock: dict, t: int, columns: Mapping[str, np.ndarray]) -> None:
    """
    calculate_window_metrics() 의 피처 스토어 버전 — **계산하지 않고 조회만 한다.**

    윈도우 합계·재스캔이 사라지므로 `amt_10s > 700` 의 700 을 750 으로 바꿔도
    피처 계산은 0회다 (§3.1). 값 자체는 배치 빌더가 미리 계산해 둔 것이고,
    scripts/verify_engine_feature_parity.py 가 두 경로의 거래 목록을 대조한다.
    """
    for name, key in STORE_FEATURE_KEYS.items():
        stock[key] = columns[name][t]


def calculate_window_metrics(stock: dict, t: int):
    """5초, 10초, 30초, 60초 윈도우 지표 계산"""
    windows = [5, 10, 30, 60]
    stock['cbv_1'] = 0

    for w in windows:
        if t > w:
            stock[f'cbv_{w}'] = sum(stock['buy_vol'][t - w: t]) / w
            if stock[f'cbv_{w}'] > stock[f'max_cbv{w}']:
                stock[f'max_cbv{w}'] = stock[f'cbv_{w}']
            if stock[f'max_cbv{w}'] != 0:
                stock[f'max{w}buyratio'] = round(stock[f'cbv_{w}'] / stock[f'max_cbv{w}'], 3)

        if t > w and stock['high'][t] >= stock['open'][0]:
            if stock[f'cbv_{w}'] > stock[f'tmax_cbv{w}']:
                stock[f'tmax_cbv{w}'] = stock[f'cbv_{w}']
            if stock[f'tmax_cbv{w}'] != 0:
                stock[f't_max{w}buyratio'] = round(stock[f'cbv_{w}'] / stock[f'tmax_cbv{w}'], 3)

    if t > 0 and stock['high'][t] >= stock['open'][0]:
        stock['cbv_1'] = stock['buy_vol'][t]
        if stock['cbv_1'] > stock['tmax_cbv1']:
            stock['tmax_cbv1'] = stock['cbv_1']
        if stock['tmax_cbv1'] != 0:
            stock['t_max1buyratio'] = round(stock['cbv_1'] / stock['tmax_cbv1'], 3)

    # ⭐️ 진입 조건(check_entry_conditions)에서 사용하는 파생 지표
    # legacy_engine.py Control_Strategy() 로직 복원 (모듈화 과정에서 누락되었던 부분)
    if t > 0:
        trigger = stock['open'][0]
        time_spread = calculate_time_spread('second', stock['time'][0], stock['time'][t])

        if time_spread > 0:
            # 초당 체결 강도(ctotal) - 진입 조건 필터에 사용
            stock['ctotal'] = sum(stock['tick'][:t]) / time_spread

            # 구간별 거래대금(amt_{w}s / bamt_{w}s)
            for w in windows:
                t_ws = max(0, t - w)
                if t_ws < t:
                    vol_ws = sum(stock['vol'][t_ws:t])
                    bvol_ws = sum(stock['buy_vol'][t_ws:t])
                    price_ws = np.mean(stock['close'][t_ws:t])
                    norm_factor = min(w, time_spread)
                    stock[f'amt_{w}s'] = vol_ws * price_ws / 10000 / norm_factor
                    stock[f'bamt_{w}s'] = bvol_ws * price_ws / 10000 / norm_factor

        # 구간별 trigger 대비 최고/최저가 괴리율(min/max_{w}_trigger)
        for w in [1, 5, 10, 30, 60]:
            if t > w:
                min_price_w = min(stock['low'][t - w: t])
                max_price_w = max(stock['high'][t - w: t])
                if min_price_w > 0:
                    stock[f'min{w}_trigger'] = round((trigger / min_price_w - 1) * 100, 3)
                if max_price_w > 0:
                    stock[f'max{w}_trigger'] = round((trigger / max_price_w - 1) * 100, 3)


def check_entry_conditions(stock: dict, t: int, set_time: int) -> bool:
    """초봉 전략 진입 조건 체킹"""
    time_int = int(stock['time'][t])
    
    if not (90100 <= time_int < 140000 and len(stock['candle_high']) > 1 and 5 < t <= set_time):
        return False
        
    if stock['position'] != 0 or stock['state'] != 0 or t + 1 >= len(stock['time']):
        return False

    trigger = stock['open'][0]
    stock['trigger'] = trigger

    # 전략 세부 수치 필터링
    if (stock['upper'] > trigger and 
        stock['high'][t] >= trigger > stock['high'][t - 1] and 
        stock['tick_rate'] <= 0.18):
        
        # 상세 전략 조건 만족 여부
        if (stock.get('max10_trigger', 0) > 0 and
            2.3 < stock.get('ctotal', 0) < 31 and
            stock.get('amt_10s', 0) > 700):
            return True

    return False