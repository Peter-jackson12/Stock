import numpy as np

def calculate_window_metrics(stock: dict, t: int):
    """5초, 10초, 30초, 60초 윈도우 지표 연산"""
    windows = [5, 10, 30, 60]
    stock['cbv_1'] = 0

    for w in windows:
        if t > w:
            # w초 동안의 초당 평균 매수량
            stock[f'cbv_{w}'] = sum(stock['buy_vol'][t - w: t]) / w
            if stock[f'cbv_{w}'] > stock.get(f'max_cbv{w}', 0):
                stock[f'max_cbv{w}'] = stock[f'cbv_{w}']
            if stock.get(f'max_cbv{w}', 0) != 0:
                stock[f'max{w}buyratio'] = round(stock[f'cbv_{w}'] / stock[f'max_cbv{w}'], 3)

        if t > w and stock['high'][t] >= stock['open'][0]:
            if stock[f'cbv_{w}'] > stock.get(f'tmax_cbv{w}', 0):
                stock[f'tmax_cbv{w}'] = stock[f'cbv_{w}']
            if stock.get(f'tmax_cbv{w}', 0) != 0:
                stock[f't_max{w}buyratio'] = round(stock[f'cbv_{w}'] / stock[f'tmax_cbv{w}'], 3)

    if t > 0 and stock['high'][t] >= stock['open'][0]:
        stock['cbv_1'] = stock['buy_vol'][t]
        if stock['cbv_1'] > stock.get('tmax_cbv1', 0):
            stock['tmax_cbv1'] = stock['cbv_1']
        if stock.get('tmax_cbv1', 0) != 0:
            stock['t_max1buyratio'] = round(stock['cbv_1'] / stock['tmax_cbv1'], 3)


def check_entry_conditions(stock: dict, t: int, set_time: int) -> bool:
    """초봉 전략 진입 조건 체킹"""
    time_int = int(stock['time'][t])
    
    # 1. 시간 조건 (09:00:05 ~ 14:00:00)
    if not (90005 <= time_int < 140000 and len(stock['candle_high']) > 1 and 5 < t <= set_time):
        return False
        
    # 2. 포지션 미보유 상태 검사
    if stock['position'] != 0 or stock['state'] != 0 or t + 1 >= len(stock['time']):
        return False

    trigger = stock['open'][0]  # 당일 시가
    stock['trigger'] = trigger

    # 3. 진입 조건: 현재 고가가 시가 이상이고, 5초 매수량(cbv_5)이 존재할 때
    if stock['high'][t] >= trigger:
        # 테스트를 위해 간단한 매수세(cbv_5 > 0) 조건만 적용
        if stock.get('cbv_5', 0) > 0:
            return True

    return False