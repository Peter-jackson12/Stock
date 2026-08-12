import numpy as np

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
            stock.get('t_max1buyratio', 0) > 0.1 and
            stock.get('max60_trigger', 0) > -0.35 and
            stock.get('max60buyratio', 0) > 0.6 and
            stock.get('cbv_5', 0) > 50 and
            stock.get('amt_10s', 0) > 700):
            return True

    return False