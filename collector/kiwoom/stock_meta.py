"""opt10001 응답 정규화. Qt/OCX와 분리되어 32비트 수집기에서도 사용 가능.
단위는 호출자가 명시한다. 시총/유통비율 교차검사는 단위 공식 인증이 아니다.
일봉 가격은 생성하지 않는다. 관측값은 수신일에만 귀속한다.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re

KST = timezone(timedelta(hours=9))


def normalize_opt10001(raw: dict[str, str], *, received_at: datetime,
                       shares_multiplier: int, mkt_to_eok: int) -> dict:
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("received_at must include timezone")
    if shares_multiplier not in (1, 1000) or mkt_to_eok != 1:
        raise ValueError("unsupported units; specify shares 1/1000 and mkt in eok")
    code = raw.get("종목코드", "").strip()
    if not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("invalid stock code")
    reasons = {}

    def number(field, *, positive=False, integer=False, signed_price=False):
        text = raw.get(field, "").strip().replace(",", "")
        if not text:
            reasons[field] = "missing"
            return None
        try:
            value = Decimal(text)
            if signed_price:
                value = abs(value)
            if not value.is_finite() or value < 0 or (positive and value == 0):
                raise ValueError()
            if integer and value != value.to_integral_value():
                raise ValueError()
            return value
        except (InvalidOperation, ValueError):
            reasons[field] = "invalid_number"
            return None

    listed = number("상장주식", positive=True, integer=True)
    floating = number("유통주식", integer=True)
    cap = number("시가총액", positive=True)
    ratio = number("유통비율")
    price = number("현재가", positive=True, signed_price=True)
    shares = int(listed * shares_multiplier) if listed is not None else None
    float_shares = int(floating * shares_multiplier) if floating is not None else None
    diagnostics = {}
    if ratio is not None and ratio > 100:
        reasons["유통비율"] = "outside_0_100"
        ratio = None
    if shares is not None and float_shares is not None:
        computed = Decimal(float_shares) / Decimal(shares) * 100
        diagnostics["computed_float_pct"] = float(computed)
        if float_shares > shares or (ratio is not None and abs(computed - ratio) > Decimal("0.1")):
            reasons["유통비율"] = "float_share_ratio_mismatch"
            ratio = None
    if shares is not None and cap is not None and price is not None:
        expected = Decimal(shares) * price / 100_000_000
        # 주식수의 표시 정밀도 한 단위 + 시총 1억원의 반올림/절사 오차 허용.
        tolerance = price * shares_multiplier / 100_000_000 + 1
        diagnostics["computed_mkt_eok"] = float(expected)
        diagnostics["mkt_difference_eok"] = float(cap - expected)
        if abs(cap - expected) > tolerance:
            reasons["상장주식"] = "market_cap_unit_or_value_mismatch"
            reasons["시가총액"] = "market_cap_unit_or_value_mismatch"
            shares = None
            cap = None
    at = received_at.astimezone(KST)
    return {
        "source": "kiwoom_opt10001", "received_at": at.isoformat(),
        "units": {"shares_multiplier": shares_multiplier, "mkt_to_eok": mkt_to_eok,
                  "float": "percent", "status": "explicit_caller_contract"},
        "raw": dict(raw), "reasons": reasons, "diagnostics": diagnostics,
        "snapshot": {"date": at.strftime("%Y%m%d"), "code": code,
                     "name": raw.get("종목명", "").strip(), "shares": shares,
                     "mkt": float(cap) if cap is not None else None,
                     "float": float(ratio) if ratio is not None else None},
        "float_shares": float_shares,
        "price_usage": "consistency_check_only_not_daily_ohlc",
    }


def save_observation(daily_dir, observation: dict):
    """64비트 일봉 저장 단계용 연결. 원응답/사유 기록 후 기존 tidy 저장소에 반영."""
    import json
    import os
    from pathlib import Path
    from uuid import uuid4
    import pandas as pd
    from collector.daily_snapshot import write_snapshot

    row = observation["snapshot"]
    if datetime.fromisoformat(observation["received_at"]).astimezone(KST).strftime("%Y%m%d") != row["date"]:
        raise ValueError("snapshot date differs from received date")
    archive = Path(daily_dir) / "kiwoom_observations"
    archive.mkdir(parents=True, exist_ok=True)
    path = archive / f"{row['date']}_{row['code']}_{uuid4().hex}.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(observation, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    return write_snapshot(daily_dir, row["date"], pd.DataFrame([row]))
