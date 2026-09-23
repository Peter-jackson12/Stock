"""Raw-v2 backend used by the operational Qt collector, with no OCX ownership.

Qt reads raw FIDs; the queue owns normalization/storage. Explicit FID 15 signs
determine direction; venue remains unknown. Output paths are new per session.
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
from collector.kiwoom.capture_telemetry import observe
from control_tower.lifecycle import CaptureReport, ProcessIdentity
from control_tower.report_journal import ReportJournal
from control_tower.windows_process import WindowsProcess
from control_tower.storage_guard import require_disk_space

TRADE_FIDS = (20, 10, 15, 14, 27, 28)
QUOTE_FIDS = (21, *range(41, 81))


class LiveRawCapture:
    # Class default so __new__-only test doubles keep diagnostic OFF / full FID path.
    fid_read_ab = None

    def __init__(self, root, *, server, code_revision, facts=None, capacity=8192,
                 feed_scope=None, fid_read_ab=None):
        if server not in ("mock", "live"):
            raise ValueError("observed mock/live server required")
        if facts is None:
            with WindowsProcess(os.getpid()) as process:
                facts = process.facts
        if facts.python_bits != 32:
            raise ValueError("operational OCX collector requires 32-bit Python")
        if feed_scope is None:
            feed_scope = "kiwoom_universe_venue_unverified"
        if type(feed_scope) is not str or not feed_scope:
            raise ValueError("feed_scope must be a non-empty str")
        root = Path(root).resolve()
        require_disk_space(root)
        self.latest_status_path = root / "operations_state" / "capture_status.json"
        session_id = uuid4().hex
        now = datetime.now()
        self.path = root / "sampledata" / "raw_ticks_v2" / now.strftime("%Y%m%d") / f"{session_id}.db"
        self.directory = root / "operations_state" / "capture_sessions" / session_id
        self.fid_read_ab = fid_read_ab
        self.identity = ProcessIdentity(session_id, facts.pid, facts.started_at_utc, facts.executable,
            code_revision, facts.python_bits, server, feed_scope, str(self.path))
        self.journal = ReportJournal(self.directory, self.identity)
        self.report = CaptureReport(self.identity, 1, "starting")
        self.journal.append(self.report)
        self.received_trades = self.received_quotes = 0
        self.telemetry = None  # logger가 명시적으로 활성화한 경우에만 연결한다.
        self._error = None
        self._last_status = 0
        self._finished = False
        self.queue = QueuedCapture(self.path, capacity=capacity, batch_size=512,
            source="kiwoom", session_id=session_id, market_date=now.strftime("%Y-%m-%d"),
            feed_scope=self.identity.feed_scope, price_policy="signed_magnitude", direction_policy="signed_volume",
            started_ns=time.perf_counter_ns(), started_at_utc=datetime.now(timezone.utc).isoformat()).start()
        try:
            ready = self.queue.ready.wait(5)
            startup = self.queue.snapshot()
            if not ready or startup["state"] != "running":
                raise RuntimeError(
                    "raw-v2 writer startup failed: "
                    f"ready={ready}, state={startup['state']}, "
                    f"error={startup['error']!r}, writer_done={self.queue.done.is_set()}")
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
        probe = self.telemetry
        sampled = (observe(probe, "reserve_sample", real_type, received_ns)
                   if probe is not None else False)
        fids = {}
        event_type = real_type
        controller = getattr(self, "fid_read_ab", None)
        fid_calls = None
        diagnostic_phase = None
        fid_read_ns = None
        queue_submit_ns = None
        try:
            if controller is not None:
                from collector.kiwoom.fid_read_ab_diagnostic import read_fids_for_phase
                diagnostic_phase = controller.current_phase()
                fids, fid_calls = read_fids_for_phase(real_type, diagnostic_phase, read_fid)
                controller.note_callback(real_type, diagnostic_phase, fid_calls)
                if sampled and probe is not None:
                    # Extra clocks only on the already-selected low-frequency sample.
                    mid = probe.clock_ns()
                    fid_read_ns = mid - received_ns if mid >= received_ns else None
            else:
                for fid in TRADE_FIDS if real_type == "주식체결" else QUOTE_FIDS:
                    fids[str(fid)] = read_fid(fid)  # Preserve source strings, including signs/whitespace.
        except Exception as exc:
            self._error = f"FID read failed: {type(exc).__name__}: {exc}"
            fids["_read_error"] = self._error
            event_type = "callback_error"  # Worker preserves partial input as an error record.
        accepted = None
        try:
            accepted = self.queue.submit_tick(code=code, venue="unknown", real_type=event_type,
                fids=fids, received_ns=received_ns, received_at_utc=received_at_utc)
        finally:
            if sampled:
                # 추가 FID 조회/JSON/파일 쓰기 없이 작은 진단 표본만 보관한다.
                kwargs = dict(code=code, real_type=real_type, fids=fids,
                              received_ns=received_ns, received_at_utc=received_at_utc,
                              accepted=accepted)
                if controller is not None and probe is not None:
                    finished_ns = probe.clock_ns()
                    kwargs["finished_ns"] = finished_ns
                    if fid_read_ns is not None:
                        q = finished_ns - received_ns - fid_read_ns
                        queue_submit_ns = q if q >= 0 else None
                    kwargs["diagnostic_phase"] = diagnostic_phase
                    kwargs["fid_call_count"] = fid_calls
                    kwargs["fid_read_ns"] = fid_read_ns
                    kwargs["queue_submit_ns"] = queue_submit_ns
                observe(probe, "callback_sample", **kwargs)
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
