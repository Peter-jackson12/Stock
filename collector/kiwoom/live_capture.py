"""Raw-v2 backend used by the operational Qt collector, with no OCX ownership.

Qt reads raw FIDs; the queue owns normalization/storage. Direction and venue stay
unknown until independently verified. All output paths are new per-session paths.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from collector.kiwoom.queued_capture import QueuedCapture
from collector.kiwoom.queue_control import QueueStopReports
from control_tower.lifecycle import CaptureReport, ProcessIdentity
from control_tower.report_journal import ReportJournal
from control_tower.windows_process import WindowsProcess
from control_tower.storage_guard import require_disk_space

TRADE_FIDS = (20, 10, 15, 14, 27, 28)
QUOTE_FIDS = (21, *range(41, 81))


class LiveRawCapture:
    def __init__(self, root, *, server, code_revision, facts=None, capacity=8192):
        if server not in ("mock", "live"):
            raise ValueError("observed mock/live server required")
        if facts is None:
            with WindowsProcess(os.getpid()) as process:
                facts = process.facts
        if facts.python_bits != 32:
            raise ValueError("operational OCX collector requires 32-bit Python")
        root = Path(root).resolve()
        require_disk_space(root)
        self.latest_status_path = root / "operations_state" / "capture_status.json"
        session_id = uuid4().hex
        now = datetime.now()
        self.path = root / "sampledata" / "raw_ticks_v2" / now.strftime("%Y%m%d") / f"{session_id}.db"
        self.directory = root / "operations_state" / "capture_sessions" / session_id
        self.identity = ProcessIdentity(session_id, facts.pid, facts.started_at_utc, facts.executable,
            code_revision, facts.python_bits, server, "kiwoom_universe_venue_unverified", str(self.path))
        self.journal = ReportJournal(self.directory, self.identity)
        self.report = CaptureReport(self.identity, 1, "starting")
        self.journal.append(self.report)
        self.received_trades = self.received_quotes = 0
        self._error = None
        self._last_status = 0
        self._finished = False
        self.queue = QueuedCapture(self.path, capacity=capacity, batch_size=512,
            source="kiwoom", session_id=session_id, market_date=now.strftime("%Y-%m-%d"),
            feed_scope=self.identity.feed_scope, price_policy="signed_magnitude", direction_policy="unknown",
            started_ns=time.perf_counter_ns(), started_at_utc=datetime.now(timezone.utc).isoformat()).start()
        try:
            if not self.queue.ready.wait(5) or self.queue.snapshot()["state"] != "running":
                raise RuntimeError("raw-v2 writer startup failed")
            self.write_status(force=True)
        except BaseException:
            self.queue.abort("backend startup failed")
            self.queue.wait(5)
            raise

    @property
    def error(self):
        snapshot = self.queue.snapshot()
        return self._error or snapshot["error"] or (
            "raw-v2 capture interrupted" if snapshot["state"] in ("interrupted", "failed") else None)

    @property
    def pending(self):
        return self.queue.snapshot()["pending_callbacks"]

    def on_tick(self, code, real_type, read_fid, *, received_ns, received_at_utc):
        if real_type not in ("주식체결", "주식호가잔량"):
            return False
        fids = {}
        event_type = real_type
        try:
            for fid in TRADE_FIDS if real_type == "주식체결" else QUOTE_FIDS:
                fids[str(fid)] = read_fid(fid)  # Preserve source strings, including signs/whitespace.
        except Exception as exc:
            self._error = f"FID read failed: {type(exc).__name__}: {exc}"
            fids["_read_error"] = self._error
            event_type = "callback_error"  # Worker preserves partial input as an error record.
        accepted = self.queue.submit_tick(code=code, venue="unknown", real_type=event_type,
            fids=fids, received_ns=received_ns, received_at_utc=received_at_utc)
        if accepted:
            if real_type == "주식체결":
                self.received_trades += 1
            else:
                self.received_quotes += 1
        return accepted

    def write_status(self, *, force=False, reason=None):
        now = time.monotonic()
        if not force and now - self._last_status < 5:
            return
        payload = dict(identity=asdict(self.identity), observed_at_utc=datetime.now(timezone.utc).isoformat(),
            snapshot=self.queue.snapshot(), received_trade_callbacks=self.received_trades,
            received_quote_callbacks=self.received_quotes, error=self.error, reason=reason,
            status_schema="raw_capture_status_v1", control_heartbeat=False)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        for path in (self.directory / "status.json", self.latest_status_path):
            temporary = path.with_suffix(".tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
        self._last_status = now

    def finish(self, reason, command=None):
        if self._finished:
            return self.queue.snapshot()["state"] == "closed" and not self.error
        self._finished = True
        reports = None
        try:
            if self.error:
                raise RuntimeError(self.error)
            reports = QueueStopReports(self.queue, self.identity).stop(
                command, self.report, close_ns=time.perf_counter_ns())
            for report in reports:
                self.journal.append(report)
                self.report = report
        except Exception as exc:
            self._error = f"shutdown incomplete: {type(exc).__name__}: {exc}"
            self.queue.abort(self._error)
        finally:
            if reports is not None:
                reports.close()
            self.queue.wait(5)
            try:
                self.write_status(force=True, reason=reason)
            except Exception as exc:
                self._error = self._error or f"status write failed: {exc}"
        return self.queue.snapshot()["state"] == "closed" and not self.error
