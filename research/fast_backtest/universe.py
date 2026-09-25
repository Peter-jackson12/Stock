"""Historical EOD metadata adapter와 인과적 cheap universe 선택."""
from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping


class UnsupportedUniverseFilter(ValueError):
    pass


ALIASES = {
    "as_of_date": ("as_of_date", "Date"),
    "code": ("code", "Code"),
    "name": ("name", "Name"),
    "market": ("market", "Market"),
    "close_krw": ("close_krw", "close", "Close"),
    "volume": ("volume", "volume_shares", "Volume"),
    "trading_value_krw": ("trading_value_krw", "trading_value", "Amount"),
    "market_cap_krw": ("market_cap_krw", "market_cap", "Marcap"),
    "listed_shares": ("listed_shares", "Stocks"),
    "float_ratio_pct": ("float_ratio_pct",),
    "float_shares": ("float_shares",),
    "float_market_cap_krw": ("float_market_cap_krw",),
    "source": ("source", "source_upstream", "source_identifier"),
    "validation_status": ("validation_status",),
}


def _value(record: Mapping[str, str], name: str, *, required: bool = True) -> str | None:
    for alias in ALIASES[name]:
        if alias in record and record[alias] not in (None, ""):
            return str(record[alias])
    if required:
        raise ValueError(f"missing historical metadata field: {name}")
    return None


def _integer(value: str | None, name: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"invalid {name}") from exc
    if not number.is_finite() or number != number.to_integral_value() or number < 0:
        raise ValueError(f"invalid {name}")
    return int(number)


def _decimal(value: str | None, name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"invalid {name}") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"invalid {name}")
    return number


@dataclass(frozen=True)
class HistoricalMetadata:
    as_of_date: str
    code: str
    name: str
    market: str
    close_krw: int
    volume: int
    trading_value_krw: int
    market_cap_krw: int
    listed_shares: int
    float_ratio_pct: Decimal | None
    float_shares: int | None
    float_market_cap_krw: int | None
    source: str
    validation_status: str

    @classmethod
    def from_mapping(cls, record: Mapping[str, str]) -> "HistoricalMetadata":
        as_of = date.fromisoformat(_value(record, "as_of_date")).isoformat()
        code = _value(record, "code")
        if len(code) != 6 or not code.isalnum() or code.upper() != code:
            raise ValueError(f"invalid KRX short code: {code}")
        return cls(
            as_of_date=as_of,
            code=code,
            name=_value(record, "name"),
            market=_value(record, "market"),
            close_krw=_integer(_value(record, "close_krw"), "close_krw"),
            volume=_integer(_value(record, "volume"), "volume"),
            trading_value_krw=_integer(_value(record, "trading_value_krw"), "trading_value_krw"),
            market_cap_krw=_integer(_value(record, "market_cap_krw"), "market_cap_krw"),
            listed_shares=_integer(_value(record, "listed_shares"), "listed_shares"),
            float_ratio_pct=_decimal(_value(record, "float_ratio_pct", required=False), "float_ratio_pct"),
            float_shares=_integer(_value(record, "float_shares", required=False), "float_shares", nullable=True),
            float_market_cap_krw=_integer(
                _value(record, "float_market_cap_krw", required=False),
                "float_market_cap_krw",
                nullable=True,
            ),
            source=_value(record, "source"),
            validation_status=_value(record, "validation_status"),
        )


@dataclass(frozen=True)
class UniverseFilter:
    allowed_market: tuple[str, ...] = ()
    min_price: int | None = None
    max_price: int | None = None
    min_trading_value: int | None = None
    min_market_cap: int | None = None
    max_market_cap: int | None = None
    min_float_market_cap: int | None = None
    max_float_market_cap: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "min_price", "max_price", "min_trading_value", "min_market_cap",
            "max_market_cap", "min_float_market_cap", "max_float_market_cap",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise ValueError("min_price exceeds max_price")
        if self.min_market_cap is not None and self.max_market_cap is not None and self.min_market_cap > self.max_market_cap:
            raise ValueError("min_market_cap exceeds max_market_cap")


@dataclass(frozen=True)
class UniverseSelection:
    trade_date: str
    metadata_as_of_date: str
    universe_mode: str
    non_causal: bool
    screening_only: bool
    size_filter_basis: str
    rows: tuple[HistoricalMetadata, ...]


def read_metadata_csv(path: str | Path) -> tuple[HistoricalMetadata, ...]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        return tuple(HistoricalMetadata.from_mapping(record) for record in csv.DictReader(stream))


def select_historical_snapshot(
    rows: Iterable[HistoricalMetadata],
    *,
    trade_date: str,
    mode: str = "causal_preopen",
    posthoc_same_day_opt_in: bool = False,
) -> UniverseSelection:
    target = date.fromisoformat(trade_date)
    items = tuple(rows)
    if mode == "causal_preopen":
        eligible_dates = {date.fromisoformat(row.as_of_date) for row in items if date.fromisoformat(row.as_of_date) < target}
        non_causal = False
    elif mode == "posthoc_same_day":
        if not posthoc_same_day_opt_in:
            raise ValueError("posthoc_same_day requires explicit opt-in")
        eligible_dates = {date.fromisoformat(row.as_of_date) for row in items if date.fromisoformat(row.as_of_date) == target}
        non_causal = True
    else:
        raise ValueError("unsupported universe mode")
    if not eligible_dates:
        raise ValueError("no eligible historical metadata snapshot")
    selected_date = max(eligible_dates).isoformat()
    selected = tuple(sorted((row for row in items if row.as_of_date == selected_date), key=lambda row: row.code))
    seen: set[str] = set()
    duplicates: set[str] = set()
    for row in selected:
        if row.code in seen:
            duplicates.add(row.code)
        seen.add(row.code)
    if duplicates:
        raise ValueError(f"duplicate metadata code(s): {','.join(sorted(duplicates))}")
    return UniverseSelection(
        trade_date=target.isoformat(),
        metadata_as_of_date=selected_date,
        universe_mode=mode,
        non_causal=non_causal,
        screening_only=True,
        size_filter_basis="total_market_cap_proxy",
        rows=selected,
    )


def apply_cheap_filters(selection: UniverseSelection, filters: UniverseFilter) -> UniverseSelection:
    float_requested = filters.min_float_market_cap is not None or filters.max_float_market_cap is not None
    if float_requested and any(row.float_market_cap_krw is None for row in selection.rows):
        raise UnsupportedUniverseFilter(
            "historical float-market-cap unavailable; total market cap is not a silent fallback"
        )

    def keep(row: HistoricalMetadata) -> bool:
        return all((
            not filters.allowed_market or row.market in filters.allowed_market,
            filters.min_price is None or row.close_krw >= filters.min_price,
            filters.max_price is None or row.close_krw <= filters.max_price,
            filters.min_trading_value is None or row.trading_value_krw >= filters.min_trading_value,
            filters.min_market_cap is None or row.market_cap_krw >= filters.min_market_cap,
            filters.max_market_cap is None or row.market_cap_krw <= filters.max_market_cap,
            filters.min_float_market_cap is None or row.float_market_cap_krw >= filters.min_float_market_cap,
            filters.max_float_market_cap is None or row.float_market_cap_krw <= filters.max_float_market_cap,
        ))

    return replace(selection, rows=tuple(row for row in selection.rows if keep(row)))
