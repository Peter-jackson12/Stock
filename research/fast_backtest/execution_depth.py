"""최소 execution-depth companion cache와 multi-code gate inventory.

Fast OrderedTick cache를 바꾸지 않고 raw quote FID 41..80의 10단계 가격/잔량만
source/session/seq에 결합한다. 모든 판정은 receive order 기준이며 invalid 새 quote는
이전 정상 quote를 대체한다.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any, Iterable, Mapping
from uuid import uuid4

from collector.raw_v2 import CaptureControl
from collector.selected_instrument_smoke_policy import (
    SAFE_CONTROL_TYPES,
    _exact_mirrored_pair,
    _selected_direction_quarantine_candidate,
    _tick_issues,
)
from collector.zero_quote_policy_experiment import classify_one_sided_zero_quote
from engine.tick_ordering import OrderedTick
from research.fast_backtest.plan import canonical_json


SCHEMA = "fast_backtest_execution_depth_v1"
INVENTORY_SCHEMA = "fast_backtest_market_inventory_v1"
MAX_QUOTE_AGE_NS = 2_000_000_000
THRESHOLD_KRW = 100_000_000
INTEGER = re.compile(r"[+-]?[0-9]+\Z")


def _vector_text(values: tuple[int | None, ...]) -> str:
    return json.dumps(values, separators=(",", ":"))


def _vector(text: str) -> tuple[int | None, ...]:
    value = json.loads(text)
    if not isinstance(value, list) or len(value) != 10:
        raise ValueError("depth vector must contain ten levels")
    return tuple(value)


def _numeric(value, *, integer=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite() or number <= 0:
        return None
    if integer and number != number.to_integral_value():
        return None
    return int(number) if integer else number


def _source_integer(
    fids: Mapping[str, Any], fid: int, *, magnitude: bool, allow_zero: bool
) -> tuple[int | None, str, str | None]:
    raw = fids.get(str(fid))
    if raw is None:
        return None, "PARTIAL", f"missing_fid_{fid}"
    if not isinstance(raw, str) or not INTEGER.fullmatch(raw.strip()):
        return None, "INVALID", f"invalid_fid_{fid}"
    value = int(raw)
    if magnitude:
        value = abs(value)
    if value < 0:
        return None, "INVALID", f"negative_fid_{fid}"
    if value == 0 and not allow_zero:
        return None, "PARTIAL", f"zero_fid_{fid}"
    return value, "COMPLETE", None


def _side(
    fids: Mapping[str, Any], *, price_start: int, size_start: int,
    magnitude: bool, ascending: bool,
) -> tuple[tuple[int | None, ...], tuple[int | None, ...], str, str | None, int | None]:
    prices, sizes = [], []
    states, reasons = [], []
    for fid in range(price_start, price_start + 10):
        value, state, reason = _source_integer(fids, fid, magnitude=magnitude, allow_zero=False)
        prices.append(value)
        states.append(state)
        if reason:
            reasons.append(reason)
    for fid in range(size_start, size_start + 10):
        value, state, reason = _source_integer(fids, fid, magnitude=False, allow_zero=True)
        sizes.append(value)
        states.append(state)
        if reason:
            reasons.append(reason)
    status = "INVALID" if "INVALID" in states else "PARTIAL" if "PARTIAL" in states else "COMPLETE"
    reason = reasons[0] if reasons else None
    if status == "COMPLETE":
        ordered = all(
            (prices[index] < prices[index + 1]) if ascending else (prices[index] > prices[index + 1])
            for index in range(9)
        )
        if not ordered:
            status = "INVALID"
            reason = "invalid_price_ladder"
    notional = (
        sum(price * size for price, size in zip(prices, sizes))
        if status == "COMPLETE" else None
    )
    return tuple(prices), tuple(sizes), status, reason, notional


@dataclass(frozen=True)
class ExecutionDepthRecord:
    source: str
    session_id: str
    seq: int
    received_ns: int
    code: str
    venue: str
    ask_prices: tuple[int | None, ...]
    ask_sizes: tuple[int | None, ...]
    bid_prices: tuple[int | None, ...]
    bid_sizes: tuple[int | None, ...]
    ask_status: str
    ask_reason: str | None
    bid_status: str
    bid_reason: str | None
    ask_depth_notional_10: int | None
    bid_depth_notional_10: int | None
    top_status: str
    top_reason: str | None

    @property
    def completeness(self) -> str:
        states = (self.ask_status, self.bid_status)
        return "INVALID" if "INVALID" in states else "PARTIAL" if "PARTIAL" in states else "COMPLETE"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"completeness": self.completeness}


def execution_depth_record(envelope: Mapping[str, Any]) -> ExecutionDepthRecord:
    event = envelope.get("event") if isinstance(envelope, Mapping) else None
    raw = envelope.get("raw_fields") if isinstance(envelope, Mapping) else None
    if not isinstance(event, OrderedTick) or event.kind != "quote":
        raise ValueError("raw quote OrderedTick envelope required")
    fids = raw.get("fids") if isinstance(raw, Mapping) else None
    if not isinstance(fids, Mapping):
        fids = {}
    magnitude = isinstance(raw, Mapping) and raw.get("price_policy") == "signed_magnitude"
    ask_prices, ask_sizes, ask_status, ask_reason, ask_notional = _side(
        fids, price_start=41, size_start=61, magnitude=magnitude, ascending=True
    )
    bid_prices, bid_sizes, bid_status, bid_reason, bid_notional = _side(
        fids, price_start=51, size_start=71, magnitude=magnitude, ascending=False
    )
    top_values = (
        _numeric(event.bid), _numeric(event.ask),
        _numeric(event.bid_size, integer=True), _numeric(event.ask_size, integer=True),
    )
    top_reason = None
    for name, value in zip(("bid", "ask", "bid_size", "ask_size"), top_values):
        if value is None:
            top_reason = f"invalid_{name}"
            break
    if top_reason is None and top_values[0] >= top_values[1]:
        top_reason = "locked_or_crossed"
    return ExecutionDepthRecord(
        source=event.source,
        session_id=event.session_id,
        seq=event.seq,
        received_ns=event.received_ns,
        code=event.code,
        venue=event.venue,
        ask_prices=ask_prices,
        ask_sizes=ask_sizes,
        bid_prices=bid_prices,
        bid_sizes=bid_sizes,
        ask_status=ask_status,
        ask_reason=ask_reason,
        bid_status=bid_status,
        bid_reason=bid_reason,
        ask_depth_notional_10=ask_notional,
        bid_depth_notional_10=bid_notional,
        top_status="VALID" if top_reason is None else "INVALID",
        top_reason=top_reason,
    )


@dataclass(frozen=True)
class ExecutionDepthSpec:
    source: str
    session_id: str
    market_date: str
    cutoff_market_second_exclusive: int
    source_prefix_digest: str
    source_path_identity: Mapping[str, Any]
    code_provenance: Mapping[str, str]

    @property
    def cache_id(self) -> str:
        identity = {"schema": SCHEMA, **asdict(self)}
        return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionDepthManifest:
    cache_dir: Path
    cache_id: str
    quote_count: int
    logical_digest: str
    spec: ExecutionDepthSpec


class ExecutionDepthWriter:
    def __init__(self, cache_root: str | Path, spec: ExecutionDepthSpec):
        self.spec = spec
        self.root = Path(cache_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.target = self.root / spec.cache_id
        if self.target.exists():
            raise FileExistsError("execution-depth cache already exists")
        self.building = self.root / f".building-{uuid4().hex}"
        self.building.mkdir(exist_ok=False)
        self.path = self.building / "depth.sqlite3"
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=OFF")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("CREATE TABLE metadata(value TEXT NOT NULL)")
        self.conn.execute("""CREATE TABLE quotes(
            seq INTEGER PRIMARY KEY, received_ns INTEGER NOT NULL,
            code TEXT NOT NULL, venue TEXT NOT NULL,
            ask_prices TEXT NOT NULL, ask_sizes TEXT NOT NULL,
            bid_prices TEXT NOT NULL, bid_sizes TEXT NOT NULL,
            ask_status TEXT NOT NULL, ask_reason TEXT,
            bid_status TEXT NOT NULL, bid_reason TEXT,
            ask_notional INTEGER, bid_notional INTEGER,
            top_status TEXT NOT NULL, top_reason TEXT
        )""")
        self.count = 0
        self.digest = hashlib.sha256()
        self.last_seq = 0

    def append(self, record: ExecutionDepthRecord) -> None:
        if record.seq <= self.last_seq:
            raise ValueError("depth records must preserve receive sequence")
        if (record.source, record.session_id) != (self.spec.source, self.spec.session_id):
            raise ValueError("depth source/session mismatch")
        self.conn.execute(
            "INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.seq, record.received_ns, record.code, record.venue,
                _vector_text(record.ask_prices), _vector_text(record.ask_sizes),
                _vector_text(record.bid_prices), _vector_text(record.bid_sizes),
                record.ask_status, record.ask_reason, record.bid_status, record.bid_reason,
                record.ask_depth_notional_10, record.bid_depth_notional_10,
                record.top_status, record.top_reason,
            ),
        )
        self.digest.update((canonical_json(record.to_dict()) + "\n").encode("utf-8"))
        self.count += 1
        self.last_seq = record.seq
        if self.count % 10_000 == 0:
            self.conn.commit()

    def finish(self, *, observed_prefix_digest: str) -> ExecutionDepthManifest:
        if observed_prefix_digest != self.spec.source_prefix_digest:
            self.abort()
            raise ValueError("source prefix digest mismatch")
        self.conn.execute("CREATE INDEX quotes_code_venue_seq ON quotes(code,venue,seq)")
        manifest = {
            "schema": SCHEMA,
            "cache_id": self.spec.cache_id,
            "immutable": True,
            **asdict(self.spec),
            "quote_count": self.count,
            "logical_digest": self.digest.hexdigest(),
            "database_file": "depth.sqlite3",
        }
        self.conn.execute("INSERT INTO metadata VALUES (?)", (canonical_json(manifest),))
        self.conn.commit()
        self.conn.close()
        (self.building / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        self.building.rename(self.target)
        return ExecutionDepthManifest(self.target, self.spec.cache_id, self.count, self.digest.hexdigest(), self.spec)

    def abort(self) -> None:
        try:
            self.conn.close()
        finally:
            if self.building.exists():
                shutil.rmtree(self.building)


def _record_from_row(spec: ExecutionDepthSpec, row) -> ExecutionDepthRecord:
    return ExecutionDepthRecord(
        source=spec.source, session_id=spec.session_id,
        seq=row[0], received_ns=row[1], code=row[2], venue=row[3],
        ask_prices=_vector(row[4]), ask_sizes=_vector(row[5]),
        bid_prices=_vector(row[6]), bid_sizes=_vector(row[7]),
        ask_status=row[8], ask_reason=row[9], bid_status=row[10], bid_reason=row[11],
        ask_depth_notional_10=row[12], bid_depth_notional_10=row[13],
        top_status=row[14], top_reason=row[15],
    )


def load_execution_depth_cache(
    cache_dir: str | Path, *, expected_source_prefix_digest: str,
    verify_payload: bool = True,
) -> ExecutionDepthManifest:
    root = Path(cache_dir).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA or not manifest.get("immutable"):
        raise ValueError("unsupported or mutable execution-depth cache")
    if manifest.get("source_prefix_digest") != expected_source_prefix_digest:
        raise ValueError("source prefix digest mismatch")
    spec = ExecutionDepthSpec(**{
        key: manifest[key] for key in (
            "source", "session_id", "market_date", "cutoff_market_second_exclusive",
            "source_prefix_digest", "source_path_identity", "code_provenance",
        )
    })
    if manifest.get("cache_id") != spec.cache_id or root.name != spec.cache_id:
        raise ValueError("execution-depth cache identity mismatch")
    conn = sqlite3.connect((root / manifest["database_file"]).as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        count = conn.execute("SELECT count(*) FROM quotes").fetchone()[0]
        if count != manifest["quote_count"]:
            raise ValueError("execution-depth count mismatch")
        if verify_payload:
            digest = hashlib.sha256()
            rows = conn.execute("SELECT * FROM quotes ORDER BY seq")
            for row in rows:
                digest.update((canonical_json(_record_from_row(spec, row).to_dict()) + "\n").encode("utf-8"))
            if digest.hexdigest() != manifest["logical_digest"]:
                raise ValueError("execution-depth payload digest mismatch")
    finally:
        conn.close()
    return ExecutionDepthManifest(root, spec.cache_id, count, manifest["logical_digest"], spec)


def load_cell_execution_depth(
    cache_dir: str | Path,
    *,
    expected_source_prefix_digest: str,
    code: str,
    venue: str,
) -> tuple[ExecutionDepthRecord, ...]:
    """검증된 companion cache에서 한 cell의 quote depth를 receive order로 읽는다."""
    manifest = load_execution_depth_cache(
        cache_dir,
        expected_source_prefix_digest=expected_source_prefix_digest,
        verify_payload=False,
    )
    return load_cell_execution_depth_from_manifest(manifest, code=code, venue=venue)


def load_cell_execution_depth_from_manifest(
    manifest: ExecutionDepthManifest,
    *,
    code: str,
    venue: str,
) -> tuple[ExecutionDepthRecord, ...]:
    metadata = json.loads((manifest.cache_dir / "manifest.json").read_text(encoding="utf-8"))
    database = manifest.cache_dir / metadata["database_file"]
    conn = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        rows = tuple(
            _record_from_row(manifest.spec, row)
            for row in conn.execute(
                "SELECT * FROM quotes WHERE code=? AND venue=? ORDER BY seq",
                (code, venue),
            )
        )
    finally:
        conn.close()
    if any(row.code != code or row.venue != venue for row in rows):
        raise ValueError("execution-depth cell identity mismatch")
    return rows


@dataclass
class Distribution:
    count: int = 0
    total: int = 0
    minimum: int | None = None
    maximum: int | None = None
    bins: Counter = field(default_factory=Counter)

    def add(self, value: int) -> None:
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        key = "lt_10m" if value < 10_000_000 else "10m_to_lt_100m" if value < THRESHOLD_KRW else "100m_to_lt_1b" if value < 1_000_000_000 else "gte_1b"
        self.bins[key] += 1

    def result(self) -> dict[str, Any]:
        return {
            "count": self.count, "min": self.minimum, "max": self.maximum,
            "mean": self.total / self.count if self.count else None,
            "bins": dict(sorted(self.bins.items())),
        }


@dataclass
class CellStats:
    code: str
    venue: str
    events: int = 0
    trades: int = 0
    quotes: int = 0
    first_received_ns: int | None = None
    last_received_ns: int | None = None
    first_received_at_utc: str | None = None
    last_received_at_utc: str | None = None
    direction: Counter = field(default_factory=Counter)
    quote_quality: Counter = field(default_factory=Counter)
    depth_quality: Counter = field(default_factory=Counter)
    gate: Counter = field(default_factory=Counter)
    gate_reasons: Counter = field(default_factory=Counter)
    clock_ns: Counter = field(default_factory=Counter)
    quality: Counter = field(default_factory=Counter)
    ask_distribution: Distribution = field(default_factory=Distribution)
    bid_distribution: Distribution = field(default_factory=Distribution)
    latest_depth: ExecutionDepthRecord | None = None
    timeline_ns: int | None = None

    def _base_gate(self) -> tuple[str, str]:
        quote = self.latest_depth
        if quote is None:
            return "UNKNOWN", "missing_quote"
        if quote.top_status != "VALID":
            return "UNKNOWN", quote.top_reason or "invalid_top"
        if quote.ask_status != "COMPLETE":
            return "UNKNOWN", quote.ask_reason or "incomplete_ask_depth"
        if quote.ask_depth_notional_10 >= THRESHOLD_KRW:
            return "PASS", "ask10_notional_gte_threshold"
        return "FAIL", "ask10_notional_below_threshold"

    def _integrate(self, end_ns: int) -> None:
        if self.timeline_ns is None or end_ns <= self.timeline_ns:
            return
        start = self.timeline_ns
        status, _ = self._base_gate()
        quote = self.latest_depth
        if quote is not None and status in ("PASS", "FAIL"):
            fresh_end = min(end_ns, quote.received_ns + MAX_QUOTE_AGE_NS)
            if fresh_end > start:
                self.clock_ns[status] += fresh_end - start
                start = fresh_end
        if end_ns > start:
            self.clock_ns["UNKNOWN"] += end_ns - start

    def accept_tick(self, envelope: Mapping[str, Any], depth: ExecutionDepthRecord | None) -> None:
        event = envelope["event"]
        self._integrate(event.received_ns)
        self.timeline_ns = event.received_ns
        self.events += 1
        self.first_received_ns = event.received_ns if self.first_received_ns is None else self.first_received_ns
        self.last_received_ns = event.received_ns
        self.first_received_at_utc = envelope["received_at_utc"] if self.first_received_at_utc is None else self.first_received_at_utc
        self.last_received_at_utc = envelope["received_at_utc"]
        if event.kind == "quote":
            self.quotes += 1
            self.latest_depth = depth
            self.quote_quality["VALID" if depth.top_status == "VALID" else "INVALID"] += 1
            self.depth_quality[depth.completeness] += 1
            if depth.ask_depth_notional_10 is not None:
                self.ask_distribution.add(depth.ask_depth_notional_10)
            if depth.bid_depth_notional_10 is not None:
                self.bid_distribution.add(depth.bid_depth_notional_10)
            return
        self.trades += 1
        direction = "unknown" if event.is_buy is None else "buy" if event.is_buy else "sell"
        self.direction[direction] += 1
        if self.latest_depth is None:
            status, reason = "UNKNOWN", "missing_quote"
        elif event.received_ns - self.latest_depth.received_ns > MAX_QUOTE_AGE_NS:
            status, reason = "UNKNOWN", "stale_quote"
        else:
            status, reason = self._base_gate()
        self.gate[status] += 1
        self.gate_reasons[reason] += 1

    def result(self, *, global_quality_ok: bool) -> dict[str, Any]:
        evaluated = sum(self.gate.values())
        active = (self.last_received_ns - self.first_received_ns) if self.events and self.events > 1 else 0
        quality_ok = global_quality_ok and not self.quality.get("disqualifying")
        if not quality_ok:
            admission = "QUALITY_DISQUALIFIED"
        elif self.depth_quality.get("COMPLETE", 0) == 0:
            admission = "INSUFFICIENT_DEPTH_DATA"
        elif self.gate.get("PASS", 0) > 0:
            admission = "ELIGIBLE_FOR_FAST_PROBE"
        elif evaluated == 0:
            admission = "NOT_ASSESSED"
        elif self.gate.get("UNKNOWN", 0) == evaluated:
            admission = "INSUFFICIENT_DEPTH_DATA"
        else:
            admission = "NO_EXECUTABLE_OPPORTUNITY"
        clock_total = sum(self.clock_ns.values())
        return {
            "cell_id": f"{self.code}={self.venue}", "code": self.code, "venue": self.venue,
            "activity": {
                "event_count": self.events, "trade_count": self.trades, "quote_count": self.quotes,
                "first_received_ns": self.first_received_ns, "last_received_ns": self.last_received_ns,
                "first_received_at_utc": self.first_received_at_utc,
                "last_received_at_utc": self.last_received_at_utc,
                "active_duration_ns": active,
            },
            "quality": {
                "selected_strategy_research_eligible": quality_ok,
                "counts": dict(sorted(self.quality.items())),
                "direction": dict(sorted(self.direction.items())),
                "top_quote": dict(sorted(self.quote_quality.items())),
                "depth": dict(sorted(self.depth_quality.items())),
            },
            "execution_depth": {
                "ask_complete_count": self.ask_distribution.count,
                "bid_complete_count": self.bid_distribution.count,
                "ask_notional_distribution": self.ask_distribution.result(),
                "bid_notional_distribution": self.bid_distribution.result(),
            },
            "event_weighted_gate": {
                "evaluated": evaluated,
                "PASS": self.gate.get("PASS", 0), "FAIL": self.gate.get("FAIL", 0),
                "UNKNOWN": self.gate.get("UNKNOWN", 0),
                "pass_rate": self.gate.get("PASS", 0) / evaluated if evaluated else None,
                "reasons": dict(sorted(self.gate_reasons.items())),
            },
            "clock_time_gate": {
                "contract": "between consecutive selected tick receipt times; freshness expires at quote_received_ns+2s",
                "total_duration_ns": clock_total,
                "PASS": self.clock_ns.get("PASS", 0), "FAIL": self.clock_ns.get("FAIL", 0),
                "UNKNOWN": self.clock_ns.get("UNKNOWN", 0),
                "pass_ratio": self.clock_ns.get("PASS", 0) / clock_total if clock_total else None,
            },
            "admission": admission,
        }


class MarketInventoryAccumulator:
    def __init__(self):
        self.cells: dict[tuple[str, str], CellStats] = {}
        self.pending: Mapping[str, Any] | None = None
        self.global_quality = Counter()
        self.tick_count = 0
        self.control_count = 0

    def _cell(self, event: OrderedTick) -> CellStats:
        return self.cells.setdefault((event.code, event.venue), CellStats(event.code, event.venue))

    def _reject_pending(self, reason: str) -> None:
        if self.pending is None:
            return
        event = self.pending["event"]
        self._cell(event).quality["disqualifying"] += 1
        self._cell(event).quality[reason] += 1
        self.global_quality[reason] += 1
        self.pending = None

    def _consume_pair(self, control: CaptureControl) -> bool:
        if self.pending is None or not _exact_mirrored_pair(self.pending, control):
            return False
        event = self.pending["event"]
        cell = self._cell(event)
        zero = classify_one_sided_zero_quote(self.pending)
        if zero.accepted:
            cell.quality["quarantined_zero_quote"] += 1
        elif _selected_direction_quarantine_candidate(self.pending):
            cell.quality["quarantined_unknown_direction"] += 1
        else:
            cell.quality["disqualifying"] += 1
            cell.quality["selected_issue_pair_disqualifying"] += 1
        self.pending = None
        return True

    def accept(self, envelope: Mapping[str, Any]) -> ExecutionDepthRecord | None:
        event = envelope.get("event")
        if not isinstance(event, (OrderedTick, CaptureControl)):
            raise TypeError("OrderedTick or CaptureControl required")
        if self.pending is not None:
            if isinstance(event, CaptureControl) and self._consume_pair(event):
                self.control_count += 1
                return None
            self._reject_pending("issue_tick_without_exact_mirrored_parse_error")
        if isinstance(event, CaptureControl):
            self.control_count += 1
            if event.control_type not in SAFE_CONTROL_TYPES:
                self.global_quality[f"unsafe_control:{event.control_type}"] += 1
            return None
        self.tick_count += 1
        depth = execution_depth_record(envelope) if event.kind == "quote" else None
        self._cell(event).accept_tick(envelope, depth)
        issues = _tick_issues(envelope)
        if issues is None:
            self._cell(event).quality["disqualifying"] += 1
            self.global_quality["malformed_normalized_issues"] += 1
        elif issues:
            self.pending = envelope
        return depth

    def result(self) -> dict[str, Any]:
        self._reject_pending("issue_tick_without_exact_mirrored_parse_error")
        global_ok = not self.global_quality
        cells = [self.cells[key].result(global_quality_ok=global_ok) for key in sorted(self.cells)]
        admissions = Counter(cell["admission"] for cell in cells)
        return {
            "schema": INVENTORY_SCHEMA,
            "source_relevant_counts": {
                "tick_records": self.tick_count,
                "control_records": self.control_count,
                "per_code_event_sum": sum(cell["activity"]["event_count"] for cell in cells),
                "reconciled": sum(cell["activity"]["event_count"] for cell in cells) == self.tick_count,
            },
            "global_quality": {"eligible": global_ok, "counts": dict(sorted(self.global_quality.items()))},
            "admission_counts": dict(sorted(admissions.items())),
            "cell_count": len(cells),
            "cells": cells,
        }
