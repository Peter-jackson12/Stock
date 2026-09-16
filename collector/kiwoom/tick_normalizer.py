"""Pure Kiwoom FID conversion prototype; no OCX/login, clocks, or DB writes.

OpenAPI+ guide 1.7 §8.2/8.4: 14 is cumulative turnover, NOT direction.
Direction remains unknown unless caller explicitly selects the signed-volume
policy after verifying its feed. Price sign stripping is an explicit policy too.
The caller supplies capture-time sequence/clock; do not invoke on legacy rows.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

from engine.tick_ordering import OrderedTick, ReceiveOrderReplay


INTEGER = re.compile(r"[+-]?[0-9]+\Z")
KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class NormalizedTick:
    event: OrderedTick
    received_at_utc: str
    raw_fields: dict
    exchange_ts_raw: str | None
    source_time_precision: str
    issues: tuple[str, ...]


def normalize_tick(*, source, session_id, seq, received_ns, received_at_utc,
                   code, venue, real_type, fids, price_policy, direction_policy="unknown"):
    """Preserve every supplied FID verbatim; parse failures become null + reasons.

    price_policy: positive_only | signed_magnitude (explicit provider convention).
    direction_policy: unknown | signed_volume (only explicit + / - accepted).
    Provider HHMMSS remains separate from receipt wall time; no exchange date or
    millisecond precision is invented. All 10 price/size source fields stay raw.
    """
    if real_type not in ("주식체결", "주식호가잔량"):
        raise ValueError("unsupported event type; preserve via future control-event journal")
    if price_policy not in ("positive_only", "signed_magnitude") or direction_policy not in ("unknown", "signed_volume"):
        raise ValueError("explicit supported normalization policies required")
    if not isinstance(fids, dict) or any(not isinstance(k, str) or (v is not None and not isinstance(v, str))
                                         for k, v in fids.items()):
        raise ValueError("FID keys and values must be raw text (or null values)")
    clock = datetime.fromisoformat(received_at_utc.replace("Z", "+00:00"))
    if clock.utcoffset() != timedelta(0):
        raise ValueError("explicit UTC receipt time required")
    local = clock.astimezone(KST)
    issues = []

    def integer(fid, *, magnitude=False, zero=False):
        raw = fids.get(str(fid))
        if raw is None or not INTEGER.fullmatch(raw.strip()):
            issues.append(f"invalid_or_missing_fid_{fid}")
            return None
        number = int(raw)
        if magnitude:
            number = abs(number)
        if number < 0 or (number == 0 and not zero):
            issues.append(f"out_of_range_fid_{fid}")
            return None
        return number

    time_fid = "20" if real_type == "주식체결" else "21"
    exchange_raw = fids.get(time_fid)
    precision = "unknown"
    if (isinstance(exchange_raw, str) and re.fullmatch(r"[0-9]{6}", exchange_raw)
            and int(exchange_raw[:2]) < 24 and int(exchange_raw[2:4]) < 60 and int(exchange_raw[4:]) < 60):
        precision = "second"
    else:
        issues.append(f"invalid_or_missing_fid_{time_fid}")
    values = {}
    if real_type == "주식체결":
        values["price"] = integer(10, magnitude=price_policy == "signed_magnitude")
        values["volume"] = integer(15, magnitude=True)
        raw_volume = fids.get("15")
        direction = None
        if (direction_policy == "signed_volume" and values["volume"] is not None
                and raw_volume.strip().startswith(("+", "-"))):
            direction = raw_volume.strip().startswith("+")
        if direction is None:
            issues.append("trade_direction_unverified")
        values["is_buy"] = direction
    else:
        values["ask"] = integer(41, magnitude=price_policy == "signed_magnitude")
        values["bid"] = integer(51, magnitude=price_policy == "signed_magnitude")
        ask_sizes = tuple(integer(fid, zero=True) for fid in range(61, 71))
        bid_sizes = tuple(integer(fid, zero=True) for fid in range(71, 81))
        values["ask_size"], values["bid_size"] = ask_sizes[0], bid_sizes[0]
        # Keep top-three if valid even when optional deeper levels are absent.
        values["ask_sizes"] = ask_sizes if None not in ask_sizes else (ask_sizes[:3] if None not in ask_sizes[:3] else None)
        values["bid_sizes"] = bid_sizes if None not in bid_sizes else (bid_sizes[:3] if None not in bid_sizes[:3] else None)
    event = OrderedTick(source=source, session_id=session_id, seq=seq, received_ns=received_ns,
                        code=code, venue=venue, kind="trade" if real_type == "주식체결" else "quote",
                        market_second=local.hour * 3600 + local.minute * 60 + local.second, **values)
    ReceiveOrderReplay(source=source, session_id=session_id, max_quote_age_ns=0).accept(event)
    raw = dict(real_type=real_type, fids=dict(fids), normalization="kiwoom_fids_prototype_1",
               price_policy=price_policy, direction_policy=direction_policy, issues=list(issues))
    return NormalizedTick(event, received_at_utc, raw, exchange_raw, precision, tuple(issues))
