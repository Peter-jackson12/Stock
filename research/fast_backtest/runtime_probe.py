"""MWFD-03 다종목 Fast runtime probe의 고정 identity와 cache/checkpoint 도구."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator, Mapping
from uuid import uuid4

from engine.tick_ordering import OrderedTick, ReceiveOrderReplay
from research.fast_backtest.feasibility import EntryFeasibilityState
from research.fast_backtest.input_cache import event_line, event_record
from research.fast_backtest.plan import canonical_json
from research.fast_backtest.sweep import FastCandidate, deduplicate_candidates


EVENT_CACHE_SCHEMA = "fast_backtest_probe_event_cache_v1"
GATE_CACHE_SCHEMA = "fast_backtest_entry_feasibility_cache_v1"
RUN_SCHEMA = "mwfd_03_runtime_probe_v1"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def write_json_create(path: str | Path, value: Any) -> None:
    target = Path(path)
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def assert_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("nonfinite numeric result")
    if isinstance(value, Mapping):
        for item in value.values():
            assert_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_finite(item)


@dataclass(frozen=True)
class CandidateFamily:
    source_path: str
    source_sha256: str
    source_record_count: int
    source_identity_count: int
    candidates: tuple[FastCandidate, ...]
    source_identity_digest: str
    source_to_fast_identity_digest: str
    ordered_identity_digest: str
    canonical_family_digest: str

    def manifest(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "source_record_count": self.source_record_count,
            "source_identity_count": self.source_identity_count,
            "unique_candidate_count": len(self.candidates),
            "source_identity_digest": self.source_identity_digest,
            "source_to_fast_identity_digest": self.source_to_fast_identity_digest,
            "ordered_identity_digest": self.ordered_identity_digest,
            "canonical_family_digest": self.canonical_family_digest,
            "candidate_order": [candidate.parameter_identity for candidate in self.candidates],
        }


def load_candidate_family(path: str | Path, *, expected_unique: int = 663) -> CandidateFamily:
    source = Path(path).resolve(strict=True)
    records: list[dict[str, Any]] = []
    source_identities = set()
    source_identity_by_id = {}
    with source.open("r", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            record = {
                "candidate_id": str(row["run_index"]),
                "params": row["params"] | {"exit_rule": row["exit_rule"]},
            }
            records.append(record)
            source_identity = str(row["parameter_identity"])
            source_identities.add(source_identity)
            source_identity_by_id[record["candidate_id"]] = source_identity
    candidates = deduplicate_candidates(records)
    computed = {candidate.parameter_identity for candidate in candidates}
    if len(candidates) != expected_unique or len(source_identities) != expected_unique:
        raise ValueError("candidate family identity mismatch")
    mapping = []
    mapped_source = set()
    for candidate in candidates:
        aliases = {source_identity_by_id[alias] for alias in candidate.aliases}
        if len(aliases) != 1:
            raise ValueError("candidate source/Fast dedup partitions disagree")
        source_identity = next(iter(aliases))
        if source_identity in mapped_source:
            raise ValueError("candidate source identity maps to multiple Fast identities")
        mapped_source.add(source_identity)
        mapping.append({
            "source_parameter_identity": source_identity,
            "fast_parameter_identity": candidate.parameter_identity,
        })
    if mapped_source != source_identities or len(computed) != expected_unique:
        raise ValueError("candidate source/Fast identity mapping is not bijective")
    ordered = [candidate.parameter_identity for candidate in candidates]
    canonical = [
        {
            "candidate_id": candidate.candidate_id,
            "parameter_identity": candidate.parameter_identity,
            "source_parameter_identity": next(
                item["source_parameter_identity"]
                for item in mapping
                if item["fast_parameter_identity"] == candidate.parameter_identity
            ),
            "aliases": list(candidate.aliases),
            "params": candidate.params,
        }
        for candidate in candidates
    ]
    return CandidateFamily(
        source_path=str(source),
        source_sha256=sha256_file(source),
        source_record_count=len(records),
        source_identity_count=len(source_identities),
        candidates=candidates,
        source_identity_digest=json_digest(sorted(source_identities)),
        source_to_fast_identity_digest=json_digest(mapping),
        ordered_identity_digest=json_digest(ordered),
        canonical_family_digest=json_digest(canonical),
    )


@dataclass(frozen=True)
class ProbeCell:
    index: int
    cell_id: str
    code: str
    venue: str
    tier: str
    event_count: int
    selection_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProbeSample:
    cells: tuple[ProbeCell, ...]
    plan_sha256: str
    inventory_sha256: str
    ordered_cell_digest: str
    selection_rule: str
    eligible_cell_count: int

    def manifest(self) -> dict[str, Any]:
        return {
            "plan_sha256": self.plan_sha256,
            "inventory_sha256": self.inventory_sha256,
            "ordered_cell_digest": self.ordered_cell_digest,
            "selection_rule": self.selection_rule,
            "eligible_cell_count": self.eligible_cell_count,
            "sample_count": len(self.cells),
            "tier_counts": dict(sorted(Counter(cell.tier for cell in self.cells).items())),
            "cells": [cell.to_dict() for cell in self.cells],
            "selection_used_pnl": False,
        }


def _expected_probe_selection(inventory: Mapping[str, Any], session_id: str) -> list[dict[str, Any]]:
    candidates = [
        cell for cell in inventory["cells"]
        if cell["admission"] == "ELIGIBLE_FOR_FAST_PROBE"
    ]
    ordered = sorted(candidates, key=lambda cell: (cell["activity"]["event_count"], cell["cell_id"]))
    n = len(ordered)
    tiers = {
        "low": ordered[: n // 3],
        "medium": ordered[n // 3: 2 * n // 3],
        "high": ordered[2 * n // 3:],
    }
    selected = []
    for tier in ("high", "medium", "low"):
        stable = sorted(
            tiers[tier],
            key=lambda cell: hashlib.sha256(f"{session_id}|{cell['cell_id']}".encode()).hexdigest(),
        )[:15]
        selected.extend({
            "tier": tier,
            "cell_id": cell["cell_id"],
            "event_count": cell["activity"]["event_count"],
            "selection_hash": hashlib.sha256(
                f"{session_id}|{cell['cell_id']}".encode()
            ).hexdigest(),
        } for cell in stable)
    return selected


def load_probe_sample(
    plan_path: str | Path,
    inventory_path: str | Path,
    *,
    expected_count: int = 45,
) -> ProbeSample:
    plan_source = Path(plan_path).resolve(strict=True)
    inventory_source = Path(inventory_path).resolve(strict=True)
    plan = json.loads(plan_source.read_text(encoding="utf-8"))
    inventory = json.loads(inventory_source.read_text(encoding="utf-8"))
    if plan.get("status") != "ADMITTED" or len(plan.get("selected_cells", ())) != expected_count:
        raise ValueError("PROBE_ADMISSION_INVALID: sample is not admitted with 45 cells")
    if any(set(item) != {"tier", "cell_id", "event_count", "selection_hash"} for item in plan["selected_cells"]):
        raise ValueError("PROBE_ADMISSION_INVALID: unexpected sample selection fields")
    expected = _expected_probe_selection(inventory, inventory["session_id"])
    if plan["selected_cells"] != expected:
        raise ValueError("PROBE_ADMISSION_INVALID: deterministic sample mismatch")
    by_id = {cell["cell_id"]: cell for cell in inventory["cells"]}
    cells = []
    seen = set()
    for index, item in enumerate(plan["selected_cells"], start=1):
        cell_id = item["cell_id"]
        if cell_id in seen or cell_id not in by_id:
            raise ValueError("PROBE_ADMISSION_INVALID: duplicate or unknown cell")
        seen.add(cell_id)
        source = by_id[cell_id]
        if source["admission"] != "ELIGIBLE_FOR_FAST_PROBE":
            raise ValueError("PROBE_ADMISSION_INVALID: selected cell is not eligible")
        code, venue = cell_id.split("=", 1)
        cells.append(ProbeCell(
            index=index,
            cell_id=cell_id,
            code=code,
            venue=venue,
            tier=item["tier"],
            event_count=item["event_count"],
            selection_hash=item["selection_hash"],
        ))
    tier_counts = Counter(cell.tier for cell in cells)
    if tier_counts != {"high": 15, "medium": 15, "low": 15}:
        raise ValueError("PROBE_ADMISSION_INVALID: activity strata mismatch")
    return ProbeSample(
        cells=tuple(cells),
        plan_sha256=sha256_file(plan_source),
        inventory_sha256=sha256_file(inventory_source),
        ordered_cell_digest=json_digest([cell.to_dict() for cell in cells]),
        selection_rule=plan["selection_rule"],
        eligible_cell_count=plan["eligible_cells"],
    )


@dataclass(frozen=True)
class ProbeEventCacheManifest:
    cache_dir: Path
    database_path: Path
    manifest: Mapping[str, Any]


class ProbeEventCacheWriter:
    def __init__(
        self,
        target: str | Path,
        *,
        source_prefix_digest: str,
        sample: ProbeSample,
    ):
        self.target = Path(target).resolve()
        if self.target.exists():
            raise FileExistsError("probe event cache already exists")
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.building = self.target.parent / f".building-{uuid4().hex}"
        self.building.mkdir(exist_ok=False)
        self.path = self.building / "events.sqlite3"
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=OFF")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("""CREATE TABLE events(
            seq INTEGER PRIMARY KEY, received_ns INTEGER NOT NULL,
            cell_id TEXT NOT NULL, payload TEXT NOT NULL
        )""")
        self.source_prefix_digest = source_prefix_digest
        self.sample = sample
        self.allowed = {cell.cell_id for cell in sample.cells}
        self.counts = Counter()
        self.bytes = Counter()
        self.digests = {cell.cell_id: hashlib.sha256() for cell in sample.cells}
        self.overall_digest = hashlib.sha256()
        self.last_seq = 0
        self.source = self.session_id = None

    def append(self, event: OrderedTick) -> bool:
        cell_id = f"{event.code}={event.venue}"
        if cell_id not in self.allowed:
            return False
        if event.seq <= self.last_seq:
            raise ValueError("selected probe events must preserve global receive order")
        self.last_seq = event.seq
        if self.source is None:
            self.source, self.session_id = event.source, event.session_id
        elif (event.source, event.session_id) != (self.source, self.session_id):
            raise ValueError("probe event cache source/session mismatch")
        encoded = event_line(event)
        payload = encoded[:-1].decode("utf-8")
        self.conn.execute(
            "INSERT INTO events VALUES (?,?,?,?)",
            (event.seq, event.received_ns, cell_id, payload),
        )
        self.counts[cell_id] += 1
        self.bytes[cell_id] += len(encoded)
        self.digests[cell_id].update(encoded)
        self.overall_digest.update(encoded)
        if sum(self.counts.values()) % 10_000 == 0:
            self.conn.commit()
        return True

    def finish(self, *, observed_prefix_digest: str) -> ProbeEventCacheManifest:
        if observed_prefix_digest != self.source_prefix_digest:
            self.abort()
            raise ValueError("source prefix digest mismatch")
        expected = {cell.cell_id: cell.event_count for cell in self.sample.cells}
        if dict(self.counts) != expected:
            self.abort()
            raise ValueError("selected probe event counts do not match preregistered sample")
        self.conn.execute("CREATE INDEX events_cell_seq ON events(cell_id,seq)")
        cells = {
            cell.cell_id: {
                "event_count": self.counts[cell.cell_id],
                "event_bytes": self.bytes[cell.cell_id],
                "event_digest": self.digests[cell.cell_id].hexdigest(),
            }
            for cell in self.sample.cells
        }
        identity = {
            "schema": EVENT_CACHE_SCHEMA,
            "source_prefix_digest": self.source_prefix_digest,
            "sample_digest": self.sample.ordered_cell_digest,
            "source": self.source,
            "session_id": self.session_id,
            "event_count": sum(self.counts.values()),
            "event_digest": self.overall_digest.hexdigest(),
            "cells": cells,
        }
        manifest = identity | {
            "cache_id": json_digest(identity),
            "database_file": "events.sqlite3",
            "immutable": True,
        }
        self.conn.commit()
        self.conn.close()
        write_json_create(self.building / "manifest.json", manifest)
        self.building.rename(self.target)
        return ProbeEventCacheManifest(self.target, self.target / "events.sqlite3", manifest)

    def abort(self) -> None:
        try:
            self.conn.close()
        finally:
            if self.building.exists():
                import shutil
                shutil.rmtree(self.building)


def _event_from_payload(payload: str) -> OrderedTick:
    value = json.loads(payload)
    for name in ("bid_sizes", "ask_sizes"):
        if value.get(name) is not None:
            value[name] = tuple(value[name])
    return OrderedTick(**value)


def load_probe_event_cache(
    cache_dir: str | Path,
    *,
    expected_source_prefix_digest: str,
    expected_sample_digest: str,
    verify_payload: bool = True,
) -> ProbeEventCacheManifest:
    root = Path(cache_dir).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != EVENT_CACHE_SCHEMA or manifest.get("immutable") is not True:
        raise ValueError("unsupported probe event cache")
    if manifest.get("source_prefix_digest") != expected_source_prefix_digest:
        raise ValueError("probe event cache source digest mismatch")
    if manifest.get("sample_digest") != expected_sample_digest:
        raise ValueError("probe event cache sample digest mismatch")
    identity = {
        key: manifest[key]
        for key in (
            "schema", "source_prefix_digest", "sample_digest", "source", "session_id",
            "event_count", "event_digest", "cells",
        )
    }
    if manifest.get("cache_id") != json_digest(identity):
        raise ValueError("probe event cache identity mismatch")
    database = root / manifest["database_file"]
    conn = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        count = conn.execute("SELECT count(*) FROM events").fetchone()[0]
        if count != manifest["event_count"]:
            raise ValueError("probe event cache count mismatch")
        if verify_payload:
            overall = hashlib.sha256()
            counts = Counter()
            digests = {cell_id: hashlib.sha256() for cell_id in manifest["cells"]}
            replay = ReceiveOrderReplay(
                source=manifest["source"], session_id=manifest["session_id"], max_quote_age_ns=0
            )
            for seq, cell_id, payload in conn.execute("SELECT seq,cell_id,payload FROM events ORDER BY seq"):
                event = _event_from_payload(payload)
                if event.seq != seq or f"{event.code}={event.venue}" != cell_id:
                    raise ValueError("probe event payload identity mismatch")
                replay.accept(event)
                encoded = (payload + "\n").encode("utf-8")
                overall.update(encoded)
                counts[cell_id] += 1
                digests[cell_id].update(encoded)
            if overall.hexdigest() != manifest["event_digest"]:
                raise ValueError("probe event cache payload digest mismatch")
            for cell_id, expected in manifest["cells"].items():
                if counts[cell_id] != expected["event_count"] or digests[cell_id].hexdigest() != expected["event_digest"]:
                    raise ValueError("probe cell event payload mismatch")
    finally:
        conn.close()
    return ProbeEventCacheManifest(root, database, manifest)


def load_probe_cell_events(cache: ProbeEventCacheManifest, cell_id: str) -> tuple[OrderedTick, ...]:
    expected = cache.manifest["cells"].get(cell_id)
    if expected is None:
        raise ValueError("cell absent from probe event cache")
    conn = sqlite3.connect(cache.database_path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        rows = tuple(
            _event_from_payload(row[0])
            for row in conn.execute(
                "SELECT payload FROM events WHERE cell_id=? ORDER BY seq", (cell_id,)
            )
        )
    finally:
        conn.close()
    digest = hashlib.sha256()
    replay = None
    for event in rows:
        if replay is None:
            replay = ReceiveOrderReplay(
                source=event.source, session_id=event.session_id, max_quote_age_ns=0
            )
        replay.accept(event)
        digest.update(event_line(event))
    if len(rows) != expected["event_count"] or digest.hexdigest() != expected["event_digest"]:
        raise ValueError("probe cell event verification failed")
    return rows


def write_gate_cache(
    path: str | Path,
    *,
    states: Mapping[int, EntryFeasibilityState],
    event_digest: str,
    depth_cache_id: str,
) -> dict[str, Any]:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=False)
    data = target / "states.jsonl"
    digest = hashlib.sha256()
    with data.open("x", encoding="utf-8", newline="\n") as stream:
        for seq in sorted(states):
            line = canonical_json(states[seq].to_dict()) + "\n"
            stream.write(line)
            digest.update(line.encode("utf-8"))
    manifest = {
        "schema": GATE_CACHE_SCHEMA,
        "event_digest": event_digest,
        "depth_cache_id": depth_cache_id,
        "state_count": len(states),
        "state_digest": digest.hexdigest(),
        "states_file": data.name,
        "causality": "each trade uses only the latest quote observed at or before its seq",
    }
    write_json_create(target / "manifest.json", manifest)
    return manifest


def load_gate_cache(
    path: str | Path,
    *,
    expected_event_digest: str,
    expected_depth_cache_id: str,
) -> dict[int, EntryFeasibilityState]:
    root = Path(path)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != GATE_CACHE_SCHEMA:
        raise ValueError("unsupported gate cache")
    if manifest.get("event_digest") != expected_event_digest:
        raise ValueError("gate cache event digest mismatch")
    if manifest.get("depth_cache_id") != expected_depth_cache_id:
        raise ValueError("gate cache depth identity mismatch")
    states = {}
    digest = hashlib.sha256()
    with (root / manifest["states_file"]).open("rb") as stream:
        for line in stream:
            digest.update(line)
            state = EntryFeasibilityState(**json.loads(line.decode("utf-8")))
            if state.seq in states:
                raise ValueError("duplicate gate state")
            states[state.seq] = state
    if len(states) != manifest["state_count"] or digest.hexdigest() != manifest["state_digest"]:
        raise ValueError("gate cache payload verification failed")
    return states


def directory_bytes(path: str | Path) -> int:
    return sum(item.stat().st_size for item in Path(path).rglob("*") if item.is_file())
