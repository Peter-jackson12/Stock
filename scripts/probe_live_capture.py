"""Run the operational backend with synthetic callbacks in 32-bit Python; no OCX."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.kiwoom.live_capture import LiveRawCapture
from collector.raw_v2 import read_raw_v2


def main():
    root = ROOT / "operations_state" / "live_backend_probes" / uuid4().hex
    capture = LiveRawCapture(root, server="mock", code_revision="synthetic-backend-probe")
    try:
        for _ in range(20):
            values = {20: "090000", 10: " -10000 ", 15: "+2", 14: "12345", 27: "10001", 28: "10000"}
            assert capture.on_tick("005930", "주식체결", values.__getitem__,
                received_ns=time.perf_counter_ns(), received_at_utc=datetime.now(timezone.utc).isoformat())
        assert capture.finish("synthetic probe")
        with read_raw_v2(capture.path) as (manifest, rows):
            count = sum(1 for _ in rows)
        assert count == 41 and manifest["payload_sha256"] == capture.report.finalization.payload_sha256
        result = dict(checked_at_utc=datetime.now(timezone.utc).isoformat(), python_bits=capture.identity.python_bits,
            ocx_used=False, simulated_server=True, production_load_validated=False, callbacks=20,
            verified_raw_records=count, snapshot=capture.queue.snapshot(), manifest=manifest)
        path = root / "result.json"
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(str(path))
        print(json.dumps(result, indent=2))
    finally:
        capture.queue.abort("probe cleanup")
        assert capture.queue.wait(6)


if __name__ == "__main__":
    main()
