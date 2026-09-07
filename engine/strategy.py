import numpy as np
from engine.utils import calculate_time_spread

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