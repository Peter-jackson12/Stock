"""
주식 백테스팅 유틸리티 함수 모음
- 호가 단위(Tick Size) 계산
- 상한가(Upper Limit) 계산
- 시간 간격(Time Spread) 계산
"""

def calculate_ticksize(price: float, back_test_date: str | int) -> int:
    """
    주가와 날짜(2023-01-25 호가 개편 기준)에 따른 대한민국 주식 호가 단위를 반환합니다.
    """
    date_int = int(back_test_date)
    price_val = float(price)

    # 2023년 1월 25일 이전 호가 단위
    if date_int < 20230125:
        if price_val >= 500000:
            return 1000
        elif price_val >= 100000:
            return 500
        elif price_val >= 50000:
            return 100
        elif price_val >= 10000:
            return 50
        elif price_val >= 5000:
            return 10
        elif price_val >= 1000:
            return 5
        else:
            return 1
    # 2023년 1월 25일 이후 호가 단위 (개편 후)
    else:
        if price_val >= 500000:
            return 1000
        elif price_val >= 200000:
            return 500
        elif price_val >= 50000:
            return 100
        elif price_val >= 20000:
            return 50
        elif price_val >= 5000:
            return 10
        elif price_val >= 2000:
            return 5
        else:
            return 1


def calculate_upperlimit(price: float, today_date: str | int) -> float:
    """
    전일 종가 기준 상한가(+30%) 가격을 호가 단위에 맞춰 정밀 계산합니다.
    """
    raw_upper = price * 1.3
    tick_size = calculate_ticksize(raw_upper, today_date)

    num = int(raw_upper / tick_size)
    upper_limit = num * tick_size
    return float(upper_limit)


def calculate_time_spread(unit: str, start_time: str, end_time: str) -> int:
    """
    'HHMMSS' 또는 'HHMM' 형태의 시간 문자열 간 차이를 초(second) 또는 분(minute) 단위로 계산합니다.
    """
    start_str = str(start_time).zfill(6)
    end_str = str(end_time).zfill(6)

    if unit == 'second':
        # (시, 분, 초)를 전부 초 단위로 환산하여 차이 계산
        start_sec = int(start_str[0:2]) * 3600 + int(start_str[2:4]) * 60 + int(start_str[4:6])
        end_sec = int(end_str[0:2]) * 3600 + int(end_str[2:4]) * 60 + int(end_str[4:6])
        return end_sec - start_sec

    elif unit == 'minute':
        # (시, 분)을 분 단위로 환산하여 차이 계산
        start_min = int(start_str[0:2]) * 60 + int(start_str[2:4])
        end_min = int(end_str[0:2]) * 60 + int(end_str[2:4])
        return end_min - start_min

    else_msg = f"[LOGGING] 잘못된 유닛 단위 전달됨: {unit}"
    print(else_msg)
    return 0


def log_message(message: str) -> None:
    """로그 메시지 출력 유틸리티"""
    print(f"[LOG] {message}")