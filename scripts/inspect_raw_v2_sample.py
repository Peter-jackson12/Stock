"""Bounded, read-only raw-v2 sample comparison; never certifies a whole file.

Exit 0: sampled records consistent; 2: sample issues/empty; 3: invalid input.
No replay, database discovery, full checksum, COUNT, or source writes.
"""
import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.raw_v2 import (
    MAX_MANIFEST_BYTES, SCHEMA, SUPPORTED_SCHEMAS, CaptureControl,
    _common, _envelope_fields, _identity, _load_json, _json,
)
from collector.kiwoom.tick_normalizer import normalize_tick
from engine.tick_ordering import OrderedTick, ReceiveOrderReplay

MAX_ROWS = 1000
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 2


def inspect_sample(path, *, start_seq, limit):
    if type(start_seq) is not int or not 1 <= start_seq <= 2**63 - 1:
        raise ValueError("start_seq must be a positive SQLite integer")
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise ValueError("limit must be between 1 and 1000")
    path = Path(path).resolve(strict=True)
    deadline = time.monotonic() + MAX_SECONDS
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        rows = conn.execute("SELECT substr(CAST(value AS BLOB),1,?) FROM metadata LIMIT 2",
                            (MAX_MANIFEST_BYTES + 1,)).fetchall()
        if len(rows) != 1 or rows[0][0] is None or len(rows[0][0]) > MAX_MANIFEST_BYTES:
            raise ValueError("one bounded manifest required")
        meta = _load_json(rows[0][0])
        _identity(meta)
        if meta.get("schema") not in SUPPORTED_SCHEMAS or meta.get("state") != "closed":
            raise ValueError("unsupported or incomplete raw dataset")
        if type(meta.get("close_ns")) is not int or meta["close_ns"] <= 0:
            raise ValueError("invalid close boundary")
        if type(meta.get("event_count")) is not int or meta["event_count"] < 0:
            raise ValueError("invalid event count")
        # Require the writer's rowid lookup; never silently fall back to a scan.
        query = "SELECT seq,substr(CAST(payload AS BLOB),1,?) FROM events WHERE seq>=? ORDER BY seq LIMIT ?"
        args = (MAX_PAYLOAD_BYTES + 1, start_seq, limit)
        plan = conn.execute("EXPLAIN QUERY PLAN " + query, args).fetchall()
        if len(plan) != 1 or "SEARCH events USING INTEGER PRIMARY KEY" not in plan[0][3]:
            raise ValueError("sample requires an INTEGER PRIMARY KEY range lookup")
        order = ReceiveOrderReplay(source=meta["source"], session_id=meta["session_id"], max_quote_age_ns=0)
        counts, issues, mismatches = Counter(), Counter(), []
        count = total_bytes = compared = 0
        last_ns = 0
        first_seq = last_seq = None
        for seq, payload in conn.execute(query, args):
            if time.monotonic() > deadline:
                raise ValueError("sample time budget exceeded")
            if payload is None or len(payload) > MAX_PAYLOAD_BYTES:
                raise ValueError("sample payload exceeds 64 KiB or is null")
            total_bytes += len(payload)
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("sample exceeds 8 MiB payload budget")
            envelope = _load_json(payload)
            if not isinstance(envelope, dict) or not isinstance(envelope.get("event"), dict):
                raise ValueError("record envelope/event object required")
            _envelope_fields(envelope.get("received_at_utc"), envelope.get("raw_fields"),
                             envelope.get("exchange_ts_raw"), envelope.get("source_time_precision"))
            fields = dict(envelope["event"])
            for name in ("bid_sizes", "ask_sizes"):
                if fields.get(name) is not None:
                    if not isinstance(fields[name], list):
                        raise ValueError("depth quantities must be arrays or null")
                    fields[name] = tuple(fields[name])
            if "control_type" in fields:
                if meta["schema"] != SCHEMA:
                    raise ValueError("control records require prototype 2")
                event = CaptureControl(**fields)
            else:
                event = OrderedTick(**fields)
            _common(event, meta, start_seq + count, last_ns)
            if event.seq != seq or event.received_ns >= meta["close_ns"] or seq > meta["event_count"]:
                raise ValueError("sample sequence/manifest boundary mismatch")
            count += 1
            first_seq = seq if first_seq is None else first_seq
            last_seq, last_ns = seq, event.received_ns
            if isinstance(event, CaptureControl):
                counts[event.control_type] += 1
                if event.control_type not in ("session_start", "session_note"):
                    issues["control:" + event.control_type] += 1
                continue
            order.accept(event)
            counts[event.kind] += 1
            raw = envelope["raw_fields"]
            if raw.get("normalization") != "kiwoom_fids_prototype_1":
                issues["unsupported_normalization"] += 1
                continue
            normalized = normalize_tick(
                source=event.source, session_id=event.session_id, seq=seq, received_ns=event.received_ns,
                received_at_utc=envelope["received_at_utc"], code=event.code, venue=event.venue,
                real_type=raw.get("real_type"), fids=raw.get("fids"),
                price_policy=raw.get("price_policy"), direction_policy=raw.get("direction_policy"))
            compared += 1
            stored = asdict(event)
            changed = [name for name, value in asdict(normalized.event).items()
                       if _json(value) != _json(stored[name])]
            for name in ("exchange_ts_raw", "source_time_precision"):
                if getattr(normalized, name) != envelope.get(name):
                    changed.append(name)
            if list(normalized.issues) != raw.get("issues"):
                changed.append("raw_fields.issues")
            if changed:
                mismatches.append(dict(seq=seq, fields=changed))
            issues.update(normalized.issues)
        if count < limit and start_seq + count <= meta["event_count"]:
            raise ValueError("sample ends before manifest event count")
        return dict(
            schema="raw_v2_sample_inspection_v1", inspected_at_utc=datetime.now(timezone.utc).isoformat(),
            path=str(path), manifest_claim=meta,
            requested_start_seq=start_seq, requested_limit=limit, sampled_records=count,
            first_seq=first_seq, last_seq=last_seq, payload_bytes=total_bytes,
            compared_ticks=compared, record_counts=dict(counts), quality_issues=dict(issues),
            mismatches=mismatches,
            status="empty_sample" if not count else "sample_issues" if issues or mismatches else "sample_consistent",
            full_integrity_verified=False, feed_accuracy_verified=False, replay_performed=False,
            limitations=["sample_only", "outside_sample_unchecked", "checksum_not_verified",
                         "stored_policies_not_certified", "identity_and_venue_not_certified",
                         "not_strategy_or_fill_validation"],
            code_sha256={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                         for name in ("scripts/inspect_raw_v2_sample.py", "collector/raw_v2.py",
                                      "collector/kiwoom/tick_normalizer.py", "engine/tick_ordering.py")})
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--start-seq", required=True, type=int)
    parser.add_argument("--limit", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        result = inspect_sample(args.db, start_seq=args.start_seq, limit=args.limit)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(f"Cannot inspect sample: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["status"] == "sample_consistent" else 2


if __name__ == "__main__":
    raise SystemExit(main())
