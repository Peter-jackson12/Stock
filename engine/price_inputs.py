"""Strict daily reference input checks; no exchange calendar or split adjustment."""
from datetime import datetime
import math

from core.price_policy import PriceBasisError


def _dates(frame, label, context):
    if frame is None or "Code" not in frame:
        raise PriceBasisError(f"D-6 {context}: {label} 날짜 열이 없습니다")
    values = frame["Code"].astype(str).tolist()
    if not values or values[0] != "Name":
        raise PriceBasisError(f"D-6 {context}: {label} Name 행이 없습니다")
    dates = values[1:]
    try:
        for date in dates:
            if len(date) != 8 or datetime.strptime(date, "%Y%m%d").strftime("%Y%m%d") != date:
                raise ValueError(date)
    except ValueError as exc:
        raise PriceBasisError(f"D-6 {context}: {label} 날짜 형식 오류") from exc
    if dates != sorted(set(dates)):
        raise PriceBasisError(f"D-6 {context}: {label} 날짜 중복/역순")
    return dates


def strict_previous_close(close_frame, open_frame, code_col, today):
    """Require matching preceding rows in both daily matrices, without filling nulls.

    This checks the supplied dates; it cannot detect a trading date omitted from
    both matrices, nor establish the exchange reference price on corporate actions.
    """
    context = f"{today}/{code_col}"
    close_dates = _dates(close_frame, "close", context)
    open_dates = _dates(open_frame, "open", context)
    if code_col not in close_frame or code_col not in open_frame:
        raise PriceBasisError(f"D-6 {context}: 일봉 종목 열이 없습니다")
    if today not in close_dates or today not in open_dates:
        raise PriceBasisError(f"D-6 {context}: 당일 날짜 행이 없어 전일 경계를 확인할 수 없습니다")
    close_idx, open_idx = close_dates.index(today), open_dates.index(today)
    if close_idx == 0 or open_idx == 0:
        raise PriceBasisError(f"D-6 {context}: 전일 날짜 행이 없습니다")
    if close_dates[close_idx - 1] != open_dates[open_idx - 1]:
        raise PriceBasisError(f"D-6 {context}: open/close의 전일 날짜가 다릅니다")
    # +1 for Name, -1 for predecessor: iloc position is close_idx.
    raw = close_frame[code_col].iloc[close_idx]
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise PriceBasisError(f"D-6 {context}: 전일 종가가 숫자가 아닙니다") from exc
    if not math.isfinite(value) or value <= 0:
        raise PriceBasisError(f"D-6 {context}: 전일 종가가 양의 유한값이 아닙니다")
    return value
