"""검증된 normalized OrderedTick의 immutable/create-only cache."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil
from typing import Iterable, Iterator, Mapping
from uuid import uuid4

from engine.tick_ordering import OrderedTick, ReceiveOrderReplay
from research.fast_backtest.plan import canonical_json


SCHEMA = "fast_backtest_input_cache_v1"


@dataclass(frozen=True)
class EventCacheSpec:
    source_dataset_identity: str
    trade_date: str
    instrument: str
    cutoff_market_second_exclusive: int
    policy: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_dataset_identity, str) or not self.source_dataset_identity:
            raise ValueError("source dataset identity required")
        date.fromisoformat(self.trade_date)
        if not isinstance(self.instrument, str) or not self.instrument:
            raise ValueError("instrument required")
        if type(self.cutoff_market_second_exclusive) is not int or not 0 < self.cutoff_market_second_exclusive <= 86400:
            raise ValueError("valid exclusive cutoff required")
        if not isinstance(self.policy, str) or not self.policy:
            raise ValueError("input policy required")


@dataclass(frozen=True)
class EventCacheManifest:
    cache_dir: Path
    cache_id: str
    event_count: int
    event_digest: str
    manifest_digest: str
    source: str
    session_id: str
    spec: EventCacheSpec


def event_record(event: OrderedTick) -> dict:
    if type(event) is not OrderedTick:
        raise ValueError("OrderedTick input required")
    return asdict(event)


def event_line(event: OrderedTick) -> bytes:
    return (canonical_json(event_record(event)) + "\n").encode("utf-8")


def _event_from_record(record: Mapping) -> OrderedTick:
    value = dict(record)
    for name in ("bid_sizes", "ask_sizes"):
        if value.get(name) is not None:
            value[name] = tuple(value[name])
    return OrderedTick(**value)


def _read_manifest(cache_dir: Path) -> tuple[dict, bytes]:
    path = cache_dir / "manifest.json"
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if value.get("schema") != SCHEMA:
        raise ValueError("unsupported input cache schema")
    return value, raw


def build_event_cache(
    events: Iterable[OrderedTick],
    *,
    spec: EventCacheSpec,
    cache_root: str | Path,
) -> EventCacheManifest:
    root = Path(cache_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    building = root / f".building-{uuid4().hex}"
    building.mkdir(exist_ok=False)
    data_path = building / "events.jsonl"
    digest = hashlib.sha256()
    replay = None
    count = 0
    source = session_id = None
    try:
        with data_path.open("xb") as stream:
            for event in events:
                if event.code != spec.instrument:
                    raise ValueError("event instrument conflicts with cache spec")
                if type(event.market_second) is not int or not 0 <= event.market_second < spec.cutoff_market_second_exclusive:
                    raise ValueError("event lacks valid pre-cutoff market_second")
                if replay is None:
                    source, session_id = event.source, event.session_id
                    replay = ReceiveOrderReplay(source=source, session_id=session_id, max_quote_age_ns=0)
                replay.accept(event)
                encoded = event_line(event)
                stream.write(encoded)
                digest.update(encoded)
                count += 1
        if count == 0:
            raise ValueError("empty verified event cache is not supported")
        identity = {
            "schema": SCHEMA,
            "source_dataset_identity": spec.source_dataset_identity,
            "trade_date": spec.trade_date,
            "instrument": spec.instrument,
            "cutoff_market_second_exclusive": spec.cutoff_market_second_exclusive,
            "policy": spec.policy,
            "source": source,
            "session_id": session_id,
            "event_count": count,
            "event_digest": digest.hexdigest(),
        }
        cache_id = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
        manifest = identity | {
            "cache_id": cache_id,
            "events_file": "events.jsonl",
            "immutable": True,
        }
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        (building / "manifest.json").write_bytes(manifest_bytes)
        target = root / cache_id
        if target.exists():
            existing = load_event_cache(target)
            if existing.event_digest != digest.hexdigest() or existing.event_count != count:
                raise ValueError("cache identity collision")
            shutil.rmtree(building)
            return existing
        building.rename(target)
        return EventCacheManifest(
            cache_dir=target,
            cache_id=cache_id,
            event_count=count,
            event_digest=digest.hexdigest(),
            manifest_digest=hashlib.sha256(manifest_bytes).hexdigest(),
            source=source,
            session_id=session_id,
            spec=spec,
        )
    except Exception:
        if building.exists():
            shutil.rmtree(building)
        raise


def load_event_cache(cache_dir: str | Path) -> EventCacheManifest:
    root = Path(cache_dir).resolve()
    manifest, raw = _read_manifest(root)
    spec = EventCacheSpec(
        source_dataset_identity=manifest["source_dataset_identity"],
        trade_date=manifest["trade_date"],
        instrument=manifest["instrument"],
        cutoff_market_second_exclusive=manifest["cutoff_market_second_exclusive"],
        policy=manifest["policy"],
    )
    expected_id = hashlib.sha256(canonical_json({
        key: manifest[key]
        for key in (
            "schema", "source_dataset_identity", "trade_date", "instrument",
            "cutoff_market_second_exclusive", "policy", "source", "session_id",
            "event_count", "event_digest",
        )
    }).encode("utf-8")).hexdigest()
    if expected_id != manifest.get("cache_id") or root.name != expected_id:
        raise ValueError("input cache identity mismatch")
    count = 0
    digest = hashlib.sha256()
    replay = ReceiveOrderReplay(
        source=manifest["source"],
        session_id=manifest["session_id"],
        max_quote_age_ns=0,
    )
    with (root / manifest["events_file"]).open("rb") as stream:
        for line in stream:
            event = _event_from_record(json.loads(line.decode("utf-8")))
            replay.accept(event)
            digest.update(line)
            count += 1
    if count != manifest["event_count"] or digest.hexdigest() != manifest["event_digest"]:
        raise ValueError("input cache payload verification failed")
    return EventCacheManifest(
        cache_dir=root,
        cache_id=manifest["cache_id"],
        event_count=count,
        event_digest=digest.hexdigest(),
        manifest_digest=hashlib.sha256(raw).hexdigest(),
        source=manifest["source"],
        session_id=manifest["session_id"],
        spec=spec,
    )


def iter_cached_events(manifest: EventCacheManifest) -> Iterator[OrderedTick]:
    verified = load_event_cache(manifest.cache_dir)
    if verified.cache_id != manifest.cache_id:
        raise ValueError("input cache manifest changed")
    with (verified.cache_dir / "events.jsonl").open("r", encoding="utf-8") as stream:
        for line in stream:
            yield _event_from_record(json.loads(line))
