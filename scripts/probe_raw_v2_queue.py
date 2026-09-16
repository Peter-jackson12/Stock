"""Small synthetic queue/SQLite smoke probe for either Python bitness; no OCX."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.kiwoom.queued_capture import QueuedCapture
from collector.raw_v2 import read_raw_v2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--callbacks", type=int, default=500)
    args = parser.parse_args()
    if not 1 <= args.callbacks <= 10000:
        parser.error("callbacks must be 1..10000")
    directory = ROOT / "operations_state" / "queue_probes" / uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / "synthetic.db"
    began = time.perf_counter()
    capture = QueuedCapture(path, capacity=args.callbacks, source="synthetic", session_id=directory.name,
        market_date="2026-09-16", feed_scope="synthetic_only", price_policy="signed_magnitude",
        direction_policy="signed_volume", started_ns=0, started_at_utc="2026-09-16T00:00:00Z").start()
    if not capture.ready.wait(10) or capture.snapshot()["state"] != "running":
        capture.abort("probe startup failed or timed out")
        raise RuntimeError(f"worker not ready: {capture.snapshot()}")
    try:
        for ns in range(1, args.callbacks + 1):
            if not capture.submit_tick(code="005930", venue="unknown", real_type="주식체결",
                    fids={"20": "090000", "10": "10000", "15": "+2"},
                    received_ns=ns, received_at_utc="2026-09-16T00:00:00Z"):
                raise RuntimeError(f"input rejected: {capture.snapshot()}")
        capture.request_stop(args.callbacks + 1)
    except BaseException:
        capture.abort("probe input interrupted")
        raise
    if not capture.wait(10) or capture.snapshot()["state"] != "closed":
        capture.abort("probe drain failed or timed out")
        raise RuntimeError(f"worker not closed: {capture.snapshot()}")
    with read_raw_v2(path) as (meta, rows):
        count = sum(1 for _ in rows)  # exhaust checksum validation
    if count != args.callbacks + 1:
        raise RuntimeError("synthetic record count mismatch")
    report = dict(checked_at_utc=datetime.now(timezone.utc).isoformat(),
                  python_bits=struct.calcsize("P") * 8, synthetic_callbacks=args.callbacks,
                  verified_records=count, manifest=meta, snapshot=capture.snapshot(),
                  elapsed_seconds=round(time.perf_counter() - began, 3),
                  ocx_used=False, production_load_validated=False)
    output = directory / "result.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"Report: {output}")


if __name__ == "__main__":
    main()
