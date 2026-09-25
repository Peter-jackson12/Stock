"""MWFD-04 1,286-cell full Fast run: 전체 모집단, 재개 가능한 materialization/출력 도구.

MWFD-03 probe 계약(event cache schema, checkpoint 레코드)을 그대로 재사용하고,
시간 예산 단위로 끊어 실행해도 결과가 동일하도록 상태를 create-only/atomic으로 남긴다.
"""
from __future__ import annotations

from collections import Counter
import ctypes
import ctypes.util
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from engine.tick_ordering import OrderedTick
from research.fast_backtest.input_cache import event_line
from research.fast_backtest.runtime_probe import (
    EVENT_CACHE_SCHEMA,
    ProbeCell,
    json_digest,
    sha256_file,
)


RUN_SCHEMA = "mwfd_04_full_run_v1"
EXPECTED_CELLS = 1286
EXPECTED_CANDIDATES = 663
ELIGIBLE = "ELIGIBLE_FOR_FAST_PROBE"
SELECTION_RULE = (
    "all MWFD-02 ELIGIBLE_FOR_FAST_PROBE cells in inventory order; "
    "activity tier = event-count terciles on (event_count, cell_id)"
)


def write_json_atomic(path: str | Path, value: Any) -> None:
    """진행 상태처럼 갱신이 필요한 파일만 tmp+replace로 쓴다."""
    target = Path(path)
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, target)


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class FullPopulation:
    cells: tuple[ProbeCell, ...]
    inventory_sha256: str
    ordered_cell_digest: str
    session_id: str
    selection_rule: str = SELECTION_RULE

    @property
    def eligible_cell_count(self) -> int:
        return len(self.cells)

    def manifest(self) -> dict[str, Any]:
        return {
            "inventory_sha256": self.inventory_sha256,
            "ordered_cell_digest": self.ordered_cell_digest,
            "session_id": self.session_id,
            "selection_rule": self.selection_rule,
            "cell_count": len(self.cells),
            "event_count": sum(cell.event_count for cell in self.cells),
            "tier_counts": dict(sorted(Counter(cell.tier for cell in self.cells).items())),
            "selection_used_pnl": False,
            "cells": [cell.to_dict() for cell in self.cells],
        }


def activity_tiers(eligible: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    ordered = sorted(eligible, key=lambda cell: (cell["activity"]["event_count"], cell["cell_id"]))
    n = len(ordered)
    tiers = {}
    for position, cell in enumerate(ordered):
        tiers[cell["cell_id"]] = "low" if position < n // 3 else "medium" if position < 2 * n // 3 else "high"
    return tiers


def load_full_population(
    inventory_path: str | Path,
    *,
    expected_sha256: str | None = None,
    expected_count: int = EXPECTED_CELLS,
) -> FullPopulation:
    source = Path(inventory_path).resolve(strict=True)
    digest = sha256_file(source)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("POPULATION_INVALID: MWFD-02 inventory digest mismatch")
    inventory = json.loads(source.read_text(encoding="utf-8"))
    session_id = inventory["session_id"]
    eligible = [cell for cell in inventory["cells"] if cell["admission"] == ELIGIBLE]
    if len(eligible) != expected_count or inventory["admission_counts"].get(ELIGIBLE) != expected_count:
        raise ValueError("POPULATION_INVALID: eligible cell count mismatch")
    tiers = activity_tiers(eligible)
    cells, seen = [], set()
    for index, item in enumerate(eligible, start=1):
        cell_id = item["cell_id"]
        if cell_id in seen:
            raise ValueError("POPULATION_INVALID: duplicate cell")
        seen.add(cell_id)
        code, venue = cell_id.split("=", 1)
        if (code, venue) != (item["code"], item["venue"]) or venue != "unknown":
            raise ValueError("POPULATION_INVALID: cell identity/venue mismatch")
        cells.append(ProbeCell(
            index=index,
            cell_id=cell_id,
            code=code,
            venue=venue,
            tier=tiers[cell_id],
            event_count=item["activity"]["event_count"],
            selection_hash=hashlib.sha256(f"{session_id}|{cell_id}".encode()).hexdigest(),
        ))
    return FullPopulation(
        cells=tuple(cells),
        inventory_sha256=digest,
        ordered_cell_digest=json_digest([cell.to_dict() for cell in cells]),
        session_id=session_id,
    )


def checkpoint_name(cell: ProbeCell) -> str:
    return f"{cell.index:04d}-{cell.code}-{cell.venue}"


# ---------------------------------------------------------------------------
# 프로세스 경계를 넘어 이어지는 SHA-256 (OpenSSL SHA256_CTX 상태 직렬화)
# ---------------------------------------------------------------------------

_CTX_BYTES = 112
_LIB = None


def _libcrypto():
    global _LIB
    if _LIB is not None:
        return _LIB
    names = ["libcrypto.so.3", "libcrypto-3-x64.dll", "libcrypto-3.dll", ctypes.util.find_library("crypto")]
    last = None
    for name in names:
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
            lib.SHA256_Init.argtypes = [ctypes.c_void_p]
            lib.SHA256_Update.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
            lib.SHA256_Final.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
            break
        except (OSError, AttributeError) as exc:
            last = exc
    else:
        raise OSError(f"resumable SHA-256 requires libcrypto: {last}")
    _LIB = lib
    # self-test: 상태 저장/복원 후 hashlib과 동일해야 한다.
    sample = [bytes([i % 251]) * (i * 7 % 300) for i in range(400)]
    reference = hashlib.sha256()
    probe = ResumableSha256()
    for position, block in enumerate(sample):
        reference.update(block)
        probe.update(block)
        if position == 200:
            probe = ResumableSha256(probe.state())
    if probe.hexdigest() != reference.hexdigest():
        _LIB = None
        raise OSError("resumable SHA-256 self-test failed")
    return _LIB


class ResumableSha256:
    def __init__(self, state: bytes | None = None):
        lib = _LIB if _LIB is not None else _libcrypto()
        if state is None:
            self._ctx = ctypes.create_string_buffer(_CTX_BYTES)
            if lib.SHA256_Init(self._ctx) != 1:
                raise OSError("SHA256_Init failed")
        else:
            if len(state) != _CTX_BYTES:
                raise ValueError("invalid SHA-256 state")
            self._ctx = ctypes.create_string_buffer(bytes(state), _CTX_BYTES)
        self._pending: list[bytes] = []
        self._pending_bytes = 0

    def update(self, data: bytes) -> None:
        self._pending.append(data)
        self._pending_bytes += len(data)
        if self._pending_bytes >= 1 << 20:
            self._flush()

    def _flush(self) -> None:
        if self._pending:
            block = b"".join(self._pending)
            if _LIB.SHA256_Update(self._ctx, block, len(block)) != 1:
                raise OSError("SHA256_Update failed")
            self._pending, self._pending_bytes = [], 0

    def state(self) -> bytes:
        self._flush()
        return bytes(self._ctx.raw)

    def hexdigest(self) -> str:
        self._flush()
        copy = ctypes.create_string_buffer(bytes(self._ctx.raw), _CTX_BYTES)
        out = ctypes.create_string_buffer(32)
        if _LIB.SHA256_Final(out, copy) != 1:
            raise OSError("SHA256_Final failed")
        return out.raw.hex()


# ---------------------------------------------------------------------------
# 재개 가능한 1,286-cell event cache builder (MWFD-03 cache schema와 동일)
# ---------------------------------------------------------------------------

class ResumableEventCacheBuilder:
    """raw prefix를 seq 순서로 여러 번에 나눠 읽되 각 레코드를 정확히 한 번만 소비한다.

    상태 파일은 DB commit 뒤에만 갱신한다. commit 후 상태 기록 전에 중단되면
    재개 시 상태의 next_seq 이상 행을 지우고 다시 쓴다(idempotent).
    """

    STATE_SCHEMA = "mwfd_04_event_build_state_v1"

    def __init__(self, build_dir: str | Path, *, population: FullPopulation, source_prefix_digest: str,
                 expected_prefix_records: int):
        self.root = Path(build_dir)
        self.population = population
        self.source_prefix_digest = source_prefix_digest
        self.expected_prefix_records = expected_prefix_records
        self.db_path = self.root / "events.sqlite3"
        self.state_path = self.root / "build_state.json"
        self.allowed = {cell.cell_id for cell in population.cells}

    def _initial_state(self) -> dict[str, Any]:
        return {
            "schema": self.STATE_SCHEMA,
            "population_digest": self.population.ordered_cell_digest,
            "source_prefix_digest": self.source_prefix_digest,
            "status": "BUILDING",
            "next_seq": 1,
            "prefix_records": 0,
            "selected_records": 0,
            "last_ns": 0,
            "last_utc": None,
            "last_selected_seq": 0,
            "source": None,
            "session_id": None,
            "sentinel": None,
            "counts": {},
            "bytes": {},
            "prefix_sha_state": ResumableSha256().state().hex(),
            "overall_sha_state": ResumableSha256().state().hex(),
            "cell_sha_state": {},
            "chunks": [],
        }

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            if self.db_path.exists():
                raise FileExistsError("event build DB exists without state")
            conn = sqlite3.connect(self.db_path)
            conn.execute("""CREATE TABLE events(
                seq INTEGER PRIMARY KEY, received_ns INTEGER NOT NULL,
                cell_id TEXT NOT NULL, payload TEXT NOT NULL
            )""")
            conn.commit()
            conn.close()
            state = self._initial_state()
            write_json_atomic(self.state_path, state)
            return state
        state = read_json(self.state_path)
        if state.get("schema") != self.STATE_SCHEMA:
            raise ValueError("event build state schema mismatch")
        if state["population_digest"] != self.population.ordered_cell_digest:
            raise ValueError("event build population drift")
        if state["source_prefix_digest"] != self.source_prefix_digest:
            raise ValueError("event build source drift")
        return state

    def run_chunk(self, raw_conn, *, manifest, cutoff_utc, decode_record, deadline, clock) -> dict[str, Any]:
        state = self.load_state()
        if state["status"] != "BUILDING":
            return state
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        # 상태 이후 행(중단된 commit 잔여)을 제거한다.
        conn.execute("DELETE FROM events WHERE seq >= ?", (state["next_seq"],))
        conn.commit()
        prefix_sha = ResumableSha256(bytes.fromhex(state["prefix_sha_state"]))
        overall_sha = ResumableSha256(bytes.fromhex(state["overall_sha_state"]))
        cell_sha = {key: ResumableSha256(bytes.fromhex(value)) for key, value in state["cell_sha_state"].items()}
        counts, sizes = Counter(state["counts"]), Counter(state["bytes"])
        expected_seq, last_ns = state["next_seq"], state["last_ns"]
        last_utc = datetime.fromisoformat(state["last_utc"]) if state["last_utc"] else None
        last_selected = state["last_selected_seq"]
        source, session_id = state["source"], state["session_id"]
        prefix_records, selected = state["prefix_records"], state["selected_records"]
        start_seq = expected_seq
        sentinel = None
        batch = []
        cursor = raw_conn.execute("SELECT seq,payload FROM events WHERE seq >= ? ORDER BY seq", (start_seq,))
        try:
            for seq, payload in cursor:
                envelope, event, received_utc = decode_record(seq, payload, manifest, expected_seq, last_ns)
                if last_utc is not None and received_utc < last_utc:
                    raise ValueError("UTC receipt clock moved backwards")
                last_utc = received_utc
                expected_seq += 1
                last_ns = event.received_ns
                if received_utc >= cutoff_utc:
                    sentinel = {"seq": seq, "received_ns": event.received_ns,
                                "received_at_utc": envelope["received_at_utc"]}
                    break
                prefix_records += 1
                prefix_sha.update(payload.encode("utf-8") + b"\n")
                if isinstance(event, OrderedTick):
                    cell_id = f"{event.code}={event.venue}"
                    if cell_id in self.allowed:
                        if event.seq <= last_selected:
                            raise ValueError("selected events must preserve global receive order")
                        last_selected = event.seq
                        if source is None:
                            source, session_id = event.source, event.session_id
                        elif (event.source, event.session_id) != (source, session_id):
                            raise ValueError("event cache source/session mismatch")
                        encoded = event_line(event)
                        batch.append((event.seq, event.received_ns, cell_id, encoded[:-1].decode("utf-8")))
                        counts[cell_id] += 1
                        sizes[cell_id] += len(encoded)
                        digest = cell_sha.get(cell_id)
                        if digest is None:
                            digest = cell_sha[cell_id] = ResumableSha256()
                        digest.update(encoded)
                        overall_sha.update(encoded)
                        selected += 1
                if len(batch) >= 20000:
                    conn.executemany("INSERT INTO events VALUES (?,?,?,?)", batch)
                    batch.clear()
                if prefix_records % 50000 == 0 and clock() >= deadline:
                    break
        finally:
            cursor.close()
        if batch:
            conn.executemany("INSERT INTO events VALUES (?,?,?,?)", batch)
        conn.commit()
        conn.close()
        state.update({
            "next_seq": expected_seq if sentinel is None else sentinel["seq"],
            "prefix_records": prefix_records,
            "selected_records": selected,
            "last_ns": last_ns,
            "last_utc": last_utc.isoformat() if last_utc else None,
            "last_selected_seq": last_selected,
            "source": source,
            "session_id": session_id,
            "counts": dict(counts),
            "bytes": dict(sizes),
            "prefix_sha_state": prefix_sha.state().hex(),
            "overall_sha_state": overall_sha.state().hex(),
            "cell_sha_state": {key: value.state().hex() for key, value in cell_sha.items()},
        })
        state["chunks"].append({"start_seq": start_seq, "next_seq": state["next_seq"],
                                "prefix_records": prefix_records})
        if sentinel is not None:
            if prefix_records != self.expected_prefix_records:
                raise ValueError("prefix record count mismatch")
            observed = prefix_sha.hexdigest()
            if observed != self.source_prefix_digest:
                raise ValueError("prefix digest mismatch")
            state["sentinel"] = sentinel
            state["observed_prefix_digest"] = observed
            state["status"] = "SOURCE_PASS_COMPLETE"
        write_json_atomic(self.state_path, state)
        return state

    def finish(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """index 생성과 manifest 작성. 결과 manifest는 MWFD-03 probe cache와 같은 schema다."""
        if state["status"] not in {"SOURCE_PASS_COMPLETE", "FINISHED"}:
            raise ValueError("source pass is not complete")
        expected = {cell.cell_id: cell.event_count for cell in self.population.cells}
        if dict(state["counts"]) != expected:
            raise ValueError("selected event counts do not match MWFD-02 inventory")
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT count(*) FROM events").fetchone()[0]
            if count != state["selected_records"]:
                raise ValueError("event build row count mismatch")
            conn.execute("CREATE INDEX IF NOT EXISTS events_cell_seq ON events(cell_id,seq)")
            conn.commit()
            conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()
        for suffix in ("-wal", "-shm", "-journal"):
            if Path(str(self.db_path) + suffix).exists():
                raise ValueError("event build DB sidecar remains")
        cells = {
            cell.cell_id: {
                "event_count": state["counts"][cell.cell_id],
                "event_bytes": state["bytes"][cell.cell_id],
                "event_digest": ResumableSha256(bytes.fromhex(state["cell_sha_state"][cell.cell_id])).hexdigest(),
            }
            for cell in self.population.cells
        }
        identity = {
            "schema": EVENT_CACHE_SCHEMA,
            "source_prefix_digest": self.source_prefix_digest,
            "sample_digest": self.population.ordered_cell_digest,
            "source": state["source"],
            "session_id": state["session_id"],
            "event_count": state["selected_records"],
            "event_digest": ResumableSha256(bytes.fromhex(state["overall_sha_state"])).hexdigest(),
            "cells": cells,
        }
        return identity | {
            "cache_id": json_digest(identity),
            "database_file": "events.sqlite3",
            "immutable": True,
        }


# ---------------------------------------------------------------------------
# 재개 가능한 단일 파일 결합 writer
# ---------------------------------------------------------------------------

class ResumableConcatWriter:
    """큰 canonical 출력 파일을 셀 단위로 이어 쓴다. 완료 전까지 숨김 partial 이름을 쓴다."""

    def __init__(self, final_path: str | Path):
        self.final = Path(final_path)
        self.partial = self.final.with_name(f".{self.final.name}.partial")
        self.state_path = self.final.with_name(f".{self.final.name}.state.json")

    def open(self) -> dict[str, Any]:
        if self.final.exists():
            raise FileExistsError(f"{self.final.name} already finalized")
        if self.state_path.exists():
            state = read_json(self.state_path)
            with self.partial.open("r+b") as stream:
                stream.truncate(state["bytes"])
        else:
            if self.partial.exists():
                raise FileExistsError("partial output without state")
            self.partial.touch(exist_ok=False)
            state = {"next_cell": 0, "bytes": 0, "rows": 0, "sha_state": ResumableSha256().state().hex()}
            write_json_atomic(self.state_path, state)
        self.state = state
        self.digest = ResumableSha256(bytes.fromhex(state["sha_state"]))
        self.stream = self.partial.open("ab")
        return state

    def write_lines(self, lines: Iterable[bytes]) -> int:
        count = 0
        for line in lines:
            self.stream.write(line)
            self.digest.update(line)
            self.state["bytes"] += len(line)
            count += 1
        self.state["rows"] += count
        return count

    def commit_cell(self, next_cell: int) -> None:
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.state["next_cell"] = next_cell
        self.state["sha_state"] = self.digest.state().hex()
        write_json_atomic(self.state_path, self.state)

    def close(self) -> None:
        self.stream.close()

    def finalize(self) -> dict[str, Any]:
        self.stream.close()
        if self.partial.stat().st_size != self.state["bytes"]:
            raise ValueError("partial output size mismatch")
        digest = self.digest.hexdigest()
        if self.final.exists():
            raise FileExistsError(f"{self.final.name} already finalized")
        os.rename(self.partial, self.final)
        result = {"path": self.final.name, "rows": self.state["rows"], "bytes": self.state["bytes"], "sha256": digest}
        self.state_path.unlink()
        return result
