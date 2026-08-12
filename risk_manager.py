from utils import calculate_time_spread

def check_exit_signals(stock_data: dict, t: int, current_time_str: str) -> str | None:
    """
    청산(Stop) 조건들을 검사하여 exit_msg 문자열 반환 (청산 조건 미충족 시 None)
    """
    entry_t = stock_data['entry_t']
    entry_time_str = stock_data['time'][entry_t]
    time_spend = calculate_time_spread('second', entry_time_str, current_time_str)

    current_high = stock_data['high'][t]
    current_low = stock_data['low'][t]
    
    # 최고가/최저가 갱신
    stock_data['max_t'] = max(current_high, stock_data['max_t'])
    stock_data['min_t'] = min(current_low, stock_data['min_t'])

    current_time_int = int(current_time_str)

    # 1. 장 마감 청산
    if current_time_int > 152060:
        return 'time_1'
    # 2. 상한가 도달 청산
    elif current_high >= stock_data['upper']:
        return 'upper'
    # 3. 손절 비율 도달 (-6%)
    elif current_low < stock_data['max_t'] * 0.94:
        return 'los'
    # 4. 트레일링 스탑
    elif len(stock_data['candle_high']) > 1:
        lookback = min(len(stock_data['candle_low']) - 1, 2)
        trailing_price = min(stock_data['candle_low'][-(lookback + 1):-1])
        if current_low <= trailing_price:
            return 'trail'

    return None