"""한 번만 계산하는 causal feature cache."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Iterable, Mapping

from engine.tick_ordering import OrderedTick, ReceiveOrderReplay
from execution.quote_validation import check_ordered_quote
from research.fast_backtest.plan import canonical_json


SCHEMA = "fast_backtest_feature_cache_v1"


@dataclass(frozen=True)
class FeatureConfig:
    recent_ticks: tuple[int, ...]
    breakout_window_seconds: tuple[int, ...]
    session_start_seconds: tuple[int, ...]
    max_quote_age_ns: int

    def __post_init__(self) -> None:
        for values, name in (
            (self.recent_ticks, "recent_ticks"),
            (self.breakout_window_seconds, "breakout_window_seconds"),
        ):
            if not values or any(type(value) is not int or value <= 0 for value in values):
                raise ValueError(f"{name} must contain positive integers")
        if not self.session_start_seconds or any(
            type(value) is not int or not 0 <= value < 86400 for value in self.session_start_seconds
        ):
            raise ValueError("session_start_seconds must contain valid market seconds")
        if type(self.max_quote_age_ns) is not int or self.max_quote_age_ns < 0:
            raise ValueError("max_quote_age_ns must be nonnegative")
        object.__setattr__(self, "recent_ticks", tuple(sorted(set(self.recent_ticks))))
        object.__setattr__(self, "breakout_window_seconds", tuple(sorted(set(self.breakout_window_seconds))))
        object.__setattr__(self, "session_start_seconds", tuple(sorted(set(self.session_start_seconds))))


@dataclass(frozen=True)
class FeatureRow:
    seq: int
    received_ns: int
    code: str
    venue: str
    kind: str
    market_second: int
    trade_price: float | None
    trade_volume: int | None
    is_buy: bool | None
    quote_seq: int | None
    quote_received_ns: int | None
    quote_age_ns: int | None
    quote_status: str
    quote_eligible: bool
    quote_reason: str
    bid: float | None
    ask: float | None
    bid_size: int | None
    ask_size: int | None
    bid3: int | None
    ask3: int | None
    spread_pct: float | None
    prior_high_by_window: dict[str, float]
    recent_volume_by_ticks: dict[str, int]
    buy_ratio_by_ticks: dict[str, float | None]
    unknown_direction_by_ticks: dict[str, int]
    open_price_by_session: dict[str, float | None]


@dataclass(frozen=True)
class CausalFeatureCache:
    schema: str
    input_event_digest: str
    feature_digest: str
    config: FeatureConfig
    rows: tuple[FeatureRow, ...]


def _valid_top3(quote: OrderedTick | None) -> tuple[int | None, int | None]:
    if quote is None:
        return None, None
    sizes = (quote.bid_sizes, quote.ask_sizes)
    if any(
        not isinstance(levels, tuple)
        or len(levels) < 3
        or any(type(value) is not int or value < 0 for value in levels[:3])
        for levels in sizes
    ):
        return None, None
    if quote.bid_sizes[0] != quote.bid_size or quote.ask_sizes[0] != quote.ask_size:
        return None, None
    return sum(quote.bid_sizes[:3]), sum(quote.ask_sizes[:3])


def build_causal_features(
    events: Iterable[OrderedTick],
    *,
    config: FeatureConfig,
    input_event_digest: str,
) -> CausalFeatureCache:
    if not isinstance(input_event_digest, str) or len(input_event_digest) != 64:
        raise ValueError("verified input event digest required")
    replay = None
    trade_history = defaultdict(deque)
    recent_history = defaultdict(deque)
    open_prices: dict[str, dict[int, float | None]] = defaultdict(
        lambda: {second: None for second in config.session_start_seconds}
    )
    rows: list[FeatureRow] = []
    digest = hashlib.sha256()
    maximum_window = max(config.breakout_window_seconds)
    maximum_recent = max(config.recent_ticks)

    for event in events:
        if type(event.market_second) is not int or not 0 <= event.market_second < 86400:
            raise ValueError("features require normalized market_second")
        if replay is None:
            replay = ReceiveOrderReplay(
                source=event.source,
                session_id=event.session_id,
                max_quote_age_ns=config.max_quote_age_ns,
            )
        view = replay.accept(event)
        checked = check_ordered_quote(view)
        book = checked.book
        bid3, ask3 = _valid_top3(view.quote)
        prior_high: dict[str, float] = {}
        volumes: dict[str, int] = {}
        ratios: dict[str, float | None] = {}
        unknowns: dict[str, int] = {}
        history = trade_history[event.code]
        recent = recent_history[event.code]

        if event.kind == "trade":
            if type(event.volume) is not int or event.volume <= 0 or event.price is None or float(event.price) <= 0:
                raise ValueError("features require positive normalized trade price/volume")
            second = event.market_second
            while history and second - history[0][0] > maximum_window:
                history.popleft()
            for window in config.breakout_window_seconds:
                candidates = [price for seen_second, price in history if second - seen_second <= window]
                prior_high[str(window)] = max(candidates, default=float(event.price))
            history.append((second, float(event.price)))
            recent.append((event.volume, event.is_buy))
            while len(recent) > maximum_recent:
                recent.popleft()
            for ticks in config.recent_ticks:
                items = tuple(recent)[-ticks:]
                total = sum(volume for volume, _ in items)
                unknown = sum(direction is None for _, direction in items)
                volumes[str(ticks)] = total
                unknowns[str(ticks)] = unknown
                ratios[str(ticks)] = (
                    None if unknown or total == 0
                    else sum(volume for volume, direction in items if direction) / total
                )
            for session_start in config.session_start_seconds:
                if second >= session_start and open_prices[event.code][session_start] is None:
                    open_prices[event.code][session_start] = float(event.price)

        row = FeatureRow(
            seq=event.seq,
            received_ns=event.received_ns,
            code=event.code,
            venue=event.venue,
            kind=event.kind,
            market_second=event.market_second,
            trade_price=float(event.price) if event.kind == "trade" else None,
            trade_volume=event.volume if event.kind == "trade" else None,
            is_buy=event.is_buy if event.kind == "trade" else None,
            quote_seq=view.quote.seq if view.quote else None,
            quote_received_ns=view.quote.received_ns if view.quote else None,
            quote_age_ns=view.quote_age_ns,
            quote_status=view.quote_status,
            quote_eligible=book is not None,
            quote_reason=checked.reason,
            bid=float(book.bid) if book else None,
            ask=float(book.ask) if book else None,
            bid_size=book.bid_size if book else None,
            ask_size=book.ask_size if book else None,
            bid3=bid3,
            ask3=ask3,
            spread_pct=(float((book.ask - book.bid) / book.bid) if book else None),
            prior_high_by_window=prior_high,
            recent_volume_by_ticks=volumes,
            buy_ratio_by_ticks=ratios,
            unknown_direction_by_ticks=unknowns,
            open_price_by_session={str(key): value for key, value in open_prices[event.code].items()},
        )
        encoded = (canonical_json(asdict(row)) + "\n").encode("utf-8")
        digest.update(encoded)
        rows.append(row)

    if replay is None:
        raise ValueError("empty feature input")
    return CausalFeatureCache(
        schema=SCHEMA,
        input_event_digest=input_event_digest,
        feature_digest=digest.hexdigest(),
        config=config,
        rows=tuple(rows),
    )


def write_feature_cache(cache: CausalFeatureCache, output_dir: str | Path) -> Path:
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    data_path = root / "features.jsonl"
    digest = hashlib.sha256()
    with data_path.open("xb") as stream:
        for row in cache.rows:
            encoded = (canonical_json(asdict(row)) + "\n").encode("utf-8")
            stream.write(encoded)
            digest.update(encoded)
    if digest.hexdigest() != cache.feature_digest:
        raise ValueError("feature cache digest changed during write")
    manifest = {
        "schema": cache.schema,
        "screening_only": True,
        "input_event_digest": cache.input_event_digest,
        "feature_digest": cache.feature_digest,
        "row_count": len(cache.rows),
        "config": asdict(cache.config),
        "features_file": data_path.name,
        "causality": "each row uses only the current or earlier events",
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return root / "manifest.json"


def _feature_row(value: Mapping) -> FeatureRow:
    return FeatureRow(**dict(value))


def load_feature_cache(
    cache_dir: str | Path,
    *,
    expected_input_event_digest: str | None = None,
) -> CausalFeatureCache:
    root = Path(cache_dir).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA or manifest.get("screening_only") is not True:
        raise ValueError("unsupported feature cache")
    if (
        expected_input_event_digest is not None
        and manifest.get("input_event_digest") != expected_input_event_digest
    ):
        raise ValueError("feature cache input digest mismatch")
    config = FeatureConfig(**manifest["config"])
    rows = []
    digest = hashlib.sha256()
    with (root / manifest["features_file"]).open("rb") as stream:
        for line in stream:
            digest.update(line)
            rows.append(_feature_row(json.loads(line.decode("utf-8"))))
    if len(rows) != manifest.get("row_count") or digest.hexdigest() != manifest.get("feature_digest"):
        raise ValueError("feature cache payload verification failed")
    return CausalFeatureCache(
        schema=SCHEMA,
        input_event_digest=manifest["input_event_digest"],
        feature_digest=digest.hexdigest(),
        config=config,
        rows=tuple(rows),
    )
