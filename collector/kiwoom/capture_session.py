"""Synchronous, injectable capture-session prototype; NOT an OCX callback adapter.

All stamps must be taken by the caller at callback entry. This class performs
SQLite I/O and must not be dropped into a live Qt callback. QueuedCapture owns it
on a dedicated worker; actual OCX wiring and latency tests remain prerequisites.
"""
from collector.raw_v2 import RawV2Writer, CaptureControl
from collector.kiwoom.tick_normalizer import normalize_tick


class CaptureSession:
    def __init__(self, path, *, source, session_id, market_date, feed_scope,
                 price_policy, direction_policy, started_ns, started_at_utc):
        if price_policy not in ("positive_only", "signed_magnitude") or direction_policy not in ("unknown", "signed_volume"):
            raise ValueError("supported policies required")
        self.source, self.session_id = source, session_id
        self.price_policy, self.direction_policy = price_policy, direction_policy
        self.writer = RawV2Writer(path, source=source, session_id=session_id,
                                  market_date=market_date, feed_scope=feed_scope)
        self.state = "running"
        try:
            self.control("session_start", started_ns, started_at_utc,
                         dict(price_policy=price_policy, direction_policy=direction_policy))
            self.writer.commit()
        except BaseException:
            self.writer.__exit__(None, None, None)
            raise

    def control(self, kind, received_ns, received_at_utc, details):
        if self.state not in ("running", "interrupted"):
            raise ValueError("capture is not writable")
        event = CaptureControl(self.source, self.session_id, self.writer.count + 1, received_ns, kind, details)
        try:
            self.writer.append(event, received_at_utc=received_at_utc, raw_fields=dict(control=kind, details=details))
        except BaseException:
            self.state = "failed"
            raise
        if kind in ("disconnect", "reconnect", "queue_overflow", "callback_error"):
            self.state = "interrupted"

    def on_tick(self, *, code, venue, real_type, fids, received_ns, received_at_utc):
        if self.state != "running":
            raise ValueError("new session/file required after interruption")
        try:
            packet = normalize_tick(source=self.source, session_id=self.session_id,
                                    seq=self.writer.count + 1, received_ns=received_ns,
                                    received_at_utc=received_at_utc, code=code, venue=venue,
                                    real_type=real_type, fids=fids, price_policy=self.price_policy,
                                    direction_policy=self.direction_policy)
        except (ValueError, TypeError, AttributeError) as exc:
            self.control("callback_error", received_ns, received_at_utc,
                         dict(code=code, real_type=real_type, fids=fids, error=f"{type(exc).__name__}: {exc}"))
            self.writer.commit()
            return None
        try:
            self.writer.append(packet.event, received_at_utc=packet.received_at_utc,
                               raw_fields=packet.raw_fields, exchange_ts_raw=packet.exchange_ts_raw,
                               source_time_precision=packet.source_time_precision)
            if packet.issues:
                self.control("parse_error", received_ns, received_at_utc,
                             dict(code=code, issues=list(packet.issues)))
        except BaseException:
            self.state = "failed"
            raise
        return packet

    def commit(self):
        if self.state not in ("running", "interrupted"):
            raise ValueError("capture is not writable")
        try:
            self.writer.commit()
        except BaseException:
            self.state = "failed"
            raise

    def finish(self, close_ns):
        if self.state not in ("running", "interrupted"):
            raise ValueError("cannot finalize failed/closed capture")
        try:
            self.writer.finish(close_ns=close_ns)
            self.state = "closed"
        except BaseException:
            self.state = "failed"
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.writer.__exit__(*exc)
        if self.state in ("running", "interrupted"):
            self.state = "incomplete"
