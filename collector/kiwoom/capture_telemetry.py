"""추가 OCX 호출 없는 선택적 관측. 수집/저장/연구 적격성 판정에는 쓰지 않는다.

Qt producer는 폴링 상태를 immutable tuple로 교체하고, 종류당 5초에 한 번만
작은 표본을 보관한다. JSON/시각 해석/디스크 쓰기는 stats의 flush에서 한다.
관측 실패·표본 누락은 원본 콜백 실패나 시장 데이터 drop으로 바꾸지 않는다.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time

KST = timezone(timedelta(hours=9))
KINDS = {"주식체결": "20", "주식호가잔량": "21"}


def observe(probe, method, *args, **kwargs):
    """기록기 대역/런타임의 오류도 수집 제어 흐름 밖으로 격리한다."""
    if probe is None:
        return None
    try:
        return getattr(probe, method)(*args, **kwargs)
    except Exception as exc:
        try:
            probe.disable(f"{method}:{type(exc).__name__}")
        except Exception:
            pass
        return None


def _text(value, limit):
    if value is None:
        return None, False, "NoneType"
    if not isinstance(value, str):
        return None, False, type(value).__name__[:40]
    return value[:limit], len(value) > limit, "str"


def clock_difference(raw_clock, received_utc):
    """날짜 보정이 아니라 명시적 동일 KST 날짜 가정의 signed clock 차이다."""
    result = {"same_day_kst_assumption": True, "not_network_latency": True,
              "difference_seconds": None, "status": "invalid_source_clock"}
    if not isinstance(raw_clock, str):
        return result
    value = raw_clock.strip()
    if len(value) != 6 or not value.isascii() or not value.isdigit():
        return result
    hour, minute, second = int(value[:2]), int(value[2:4]), int(value[4:])
    if hour > 23 or minute > 59 or second > 59:
        return result
    try:
        received = datetime.fromisoformat(received_utc.replace("Z", "+00:00"))
        if received.utcoffset() is None:
            raise ValueError("timezone required")
        local = received.astimezone(KST)
    except (AttributeError, TypeError, ValueError):
        result["status"] = "invalid_receive_clock"
        return result
    seconds = local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6
    result.update(status="unverified_clock_difference",
                  difference_seconds=seconds - (hour * 3600 + minute * 60 + second))
    # 음수/자정 경계도 감추거나 +/-24시간으로 자동 보정하지 않는다.
    return result


class CaptureTelemetry:
    SAMPLE_NS = 5_000_000_000
    FLUSH_NS = 60_000_000_000
    MAX_PENDING = 64
    MAX_BYTES = 8 * 1024 * 1024
    MAX_BATCHES = 1024
    MAX_RECORD_BYTES = 64 * 1024

    def __init__(self, *, clock_ns=None):
        self.clock_ns = clock_ns or time.perf_counter_ns
        self.owner_thread = threading.get_ident()
        self.path = self.stream = None
        self.session_id = None
        self.error = None
        self.closed = False
        self.bytes_written = self.batches_written = self.samples_dropped = 0
        self._next_sample = {kind: 0 for kind in KINDS}
        self._samples = deque()
        self._samples_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._last_flush_ns = None
        # entries, returns, inflight, last_enter_ns, last_return_ns,
        # last_duration_ns, last_return_id, outcome, connection_observation
        self._poll = (0, 0, 0, None, None, None, None, None, None)

    def disable(self, reason):
        self.error = self.error or str(reason)[:128]

    def bind(self, directory, *, session_id, code_revision):
        """새 세션의 새 진단 파일만 생성. 충돌 시 기존 파일을 보존한다."""
        if self.path is not None or self.error or self.closed:
            return False
        self.path = Path(directory) / "capture_telemetry.jsonl"
        self.session_id = session_id
        try:
            self.stream = self.path.open("xb")
            header = dict(schema="capture_telemetry_v1", kind="header", session_id=session_id,
                          code_revision=code_revision, sample_interval_ns=self.SAMPLE_NS,
                          flush_interval_ns=self.FLUSH_NS, pending_limit=self.MAX_PENDING,
                          byte_limit=self.MAX_BYTES, batch_limit=self.MAX_BATCHES,
                          feed_or_storage_certification=False)
            self._write(header)
        except Exception as exc:
            self.disable(f"bind:{type(exc).__name__}")
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            return False
        return True

    def poll_enter(self):
        if self.error or self.closed or threading.get_ident() != self.owner_thread:
            return None
        now = self.clock_ns()
        old = self._poll
        number = old[0] + 1
        self._poll = (number, old[1], old[2] + 1, now, *old[4:])
        return number, now

    def connection_observed(self, token, value):
        if (token is None or self.error or self.closed
                or threading.get_ident() != self.owner_thread):
            return
        if type(value) is int:
            saved = value if -(2 ** 63) <= value < 2 ** 63 else None
        elif isinstance(value, str):
            saved = value[:64]
        elif value is None or type(value) is bool:
            saved = value
        else:
            saved = None
        observation = (token[0], self.clock_ns(), type(value).__name__[:40], saved,
                       isinstance(value, str) and len(value) > 64)
        self._poll = (*self._poll[:8], observation)

    def poll_return(self, token, *, outcome):
        if (token is None or self.error or self.closed
                or threading.get_ident() != self.owner_thread):
            return
        now = self.clock_ns()
        old = self._poll
        duration = now - token[1]
        self._poll = (old[0], old[1] + 1, max(0, old[2] - 1), old[3], now,
                      duration if duration >= 0 else None, token[0], str(outcome)[:40], old[8])

    def reserve_sample(self, real_type, received_ns):
        if (self.error or self.closed or self.stream is None
                or threading.get_ident() != self.owner_thread or real_type not in KINDS):
            return False
        if type(received_ns) is not int or received_ns < self._next_sample[real_type]:
            return False
        self._next_sample[real_type] = received_ns + self.SAMPLE_NS
        return True

    def callback_sample(self, *, code, real_type, fids, received_ns, received_at_utc,
                        accepted, finished_ns=None):
        if (self.error or self.closed or self.stream is None
                or threading.get_ident() != self.owner_thread):
            return
        if finished_ns is None:
            finished_ns = self.clock_ns()  # 선택된 표본에만 추가 시계 호출
        if not self._samples_lock.acquire(blocking=False):
            self.samples_dropped += 1
            return
        try:
            if len(self._samples) >= self.MAX_PENDING:
                self.samples_dropped += 1
                return
            fid = KINDS[real_type]
            raw_clock, truncated, raw_type = _text(fids.get(fid), 32)
            receive_clock, receive_truncated, _ = _text(received_at_utc, 64)
            safe_code, code_truncated, _ = _text(code, 32)
            duration = finished_ns - received_ns
            self._samples.append(dict(code=safe_code, code_truncated=code_truncated,
                real_type=real_type, source_fid=fid, source_clock_raw=raw_clock,
                source_clock_type=raw_type, source_clock_truncated=truncated,
                received_at_utc=receive_clock, receive_clock_truncated=receive_truncated,
                received_ns=received_ns, processing_ns=duration if duration >= 0 else None,
                processing_scope="callback_entry_to_queue_submit_return",
                accepted=accepted if type(accepted) is bool else None))
        finally:
            self._samples_lock.release()

    def poll_snapshot(self, now_ns):
        state = self._poll
        connection = state[8]
        return dict(entries=state[0], returns=state[1], in_flight=state[2],
            last_entry_ns=state[3], last_return_ns=state[4], last_duration_ns=state[5],
            last_return_id=state[6], last_outcome=state[7],
            entry_age_ns=None if state[3] is None else now_ns - state[3],
            return_age_ns=None if state[4] is None else now_ns - state[4],
            connection=None if connection is None else dict(poll_id=connection[0],
                observed_ns=connection[1], value_type=connection[2], value=connection[3],
                truncated=connection[4]),
            note="Qt polling observation, not feed health or native stack")

    def _write(self, record):
        data = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(data) > self.MAX_RECORD_BYTES or self.bytes_written + len(data) > self.MAX_BYTES:
            raise ValueError("telemetry byte budget exhausted")
        written = self.stream.write(data)
        if written != len(data):
            raise OSError("short telemetry write")
        self.stream.flush()
        self.bytes_written += written

    def flush(self, *, force=False):
        """stats/finalization 전용. 콜백에서 호출하지 않는다. I/O 시간 상한은 아니다."""
        if self.error or self.closed or self.stream is None:
            return False
        if not self._flush_lock.acquire(blocking=False):
            return False
        try:
            now = self.clock_ns()
            if (not force and self._last_flush_ns is not None
                    and now - self._last_flush_ns < self.FLUSH_NS):
                return False
            if self.batches_written >= self.MAX_BATCHES:
                self.disable("batch_budget_exhausted")
                return False
            self._last_flush_ns = now
            with self._samples_lock:
                samples = list(self._samples)
                self._samples.clear()
            for sample in samples:
                if sample["source_clock_truncated"] or sample["receive_clock_truncated"]:
                    sample["clock_comparison"] = {"status": "truncated", "difference_seconds": None}
                else:
                    sample["clock_comparison"] = clock_difference(
                        sample["source_clock_raw"], sample["received_at_utc"])
            self._write(dict(schema="capture_telemetry_v1", kind="sample_batch",
                session_id=self.session_id, observed_at_utc=datetime.now(timezone.utc).isoformat(),
                observed_ns=now, poll=self.poll_snapshot(now), samples=samples,
                diagnostic_samples_dropped=self.samples_dropped, force=bool(force)))
            self.batches_written += 1
            return True
        except Exception as exc:
            self.disable(f"flush:{type(exc).__name__}")
            return False
        finally:
            self._flush_lock.release()

    def close(self):
        if self.closed:
            return True
        try:
            self.flush(force=True)
        except Exception as exc:
            # 最終 flush의 예외도 파일 닫기를 건너뛰게 해서는 안 된다.
            # 앞서 기록된 진단 오류는 disable의 first-error 정책으로 보존한다.
            self.disable(f"close_flush:{type(exc).__name__}")
        if not self._flush_lock.acquire(blocking=False):
            return False
        try:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self.closed = True
            return True
        except Exception as exc:
            self.disable(f"close:{type(exc).__name__}")
            return False
        finally:
            self._flush_lock.release()
