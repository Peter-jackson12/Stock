"""Qt 비의존 opt10001 배치 상태 머신. 원응답/재개/호출 간격을 SQLite에 보존."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from collector.kiwoom.stock_meta import KST, normalize_opt10001


def validate_job(job):
    if job.get("version") != 1 or job.get("server") not in ("mock", "live"):
        raise ValueError("unsupported job version/server")
    day = job["date"]
    if datetime.strptime(day, "%Y%m%d").strftime("%Y%m%d") != day:
        raise ValueError("invalid date")
    codes = job["codes"]
    if not 1 <= len(codes) <= 50 or len(set(codes)) != len(codes):
        raise ValueError("pilot requires 1..50 unique codes")
    if any(not isinstance(c, str) or not re.fullmatch(r"[0-9]{6}", c) for c in codes):
        raise ValueError("invalid code")
    if job.get("shares_multiplier") not in (1, 1000):
        raise ValueError("explicit shares_multiplier required")
    return job


class BatchStore:
    def __init__(self, path, job):
        self.job = validate_job(job)
        payload = json.dumps(job, ensure_ascii=False, sort_keys=True)
        self.job_id = hashlib.sha256(payload.encode()).hexdigest()[:20]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(
                id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, code TEXT NOT NULL,
                requested_at REAL NOT NULL, status TEXT NOT NULL,
                observation TEXT, error TEXT);
            CREATE INDEX IF NOT EXISTS by_job ON attempts(job_id, code, status);
        """)
        self.conn.execute("INSERT OR IGNORE INTO jobs VALUES (?,?)", (self.job_id, payload))
        self.conn.commit()

    def complete_codes(self):
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT code FROM attempts WHERE job_id=? AND status='complete'", (self.job_id,))}

    def last_request(self):
        return self.conn.execute("SELECT MAX(requested_at) FROM attempts").fetchone()[0]

    def begin(self, code, now):
        cursor = self.conn.execute(
            "INSERT INTO attempts(job_id,code,requested_at,status) VALUES (?,?,?,'requested')",
            (self.job_id, code, now))
        self.conn.commit()  # 송신 전에 기록해 재시작에도 호출 간격을 유지한다.
        return cursor.lastrowid

    def finish(self, attempt, status, observation=None, error=None):
        self.conn.execute("UPDATE attempts SET status=?,observation=?,error=? WHERE id=? AND job_id=?",
                          (status, json.dumps(observation, ensure_ascii=False) if observation else None,
                           error, attempt, self.job_id))
        self.conn.commit()

    def close(self):
        self.conn.close()


class BatchController:
    def __init__(self, store, send, *, clock=time.time, interval=4.0, timeout=20.0, tries=2):
        if interval < 4 or timeout <= 0 or not 1 <= tries <= 3:
            raise ValueError("interval >=4, timeout >0, tries 1..3 required")
        self.store, self.send, self.clock = store, send, clock
        self.interval, self.timeout, self.tries = interval, timeout, tries
        self.used = Counter()
        self.active = None
        self.stopped = None

    def today(self):
        return datetime.fromtimestamp(self.clock(), KST).strftime("%Y%m%d")

    def pump(self):
        if self.stopped:
            return
        if self.today() != self.store.job["date"]:
            self.stop("date_changed")
            return
        now = self.clock()
        if self.active:
            if now - self.active[2] >= self.timeout:
                self.store.finish(self.active[0], "failed", error="timeout")
                self.active = None
            else:
                return
        completed = self.store.complete_codes()
        remaining = [c for c in self.store.job["codes"] if c not in completed and self.used[c] < self.tries]
        if not remaining:
            self.stopped = "complete" if len(completed) == len(self.store.job["codes"]) else "incomplete"
            return
        last = self.store.last_request()
        if last is not None and now - last < self.interval:
            return
        code = remaining[0]
        attempt = self.store.begin(code, now)
        self.used[code] += 1
        self.active = (attempt, code, now)
        try:
            rc = self.send(f"meta_{attempt}", code)
        except Exception as exc:
            self.stop(f"send_exception: {exc}")
            return
        if rc != 0:
            # -200 등의 제한 오류에서 반복 재요청하지 않는다.
            self.stop(f"request_rejected: {rc}")

    def receive(self, rqname, raw):
        if not self.active or rqname != f"meta_{self.active[0]}":
            return False  # 타임아웃 뒤 늦게 온 응답을 다음 종목에 붙이지 않는다.
        attempt, code, _ = self.active
        at = datetime.fromtimestamp(self.clock(), KST)
        envelope = {"raw": dict(raw), "received_at": at.isoformat(), "server": self.store.job["server"]}
        if self.clock() - self.active[2] >= self.timeout:
            self.store.finish(attempt, "failed", envelope, "late_response")
        elif raw.get("종목코드", "").strip() != code or self.today() != self.store.job["date"]:
            self.store.finish(attempt, "failed", envelope, "code_or_date_mismatch")
        else:
            try:
                observation = normalize_opt10001(raw, received_at=at,
                    shares_multiplier=self.store.job["shares_multiplier"], mkt_to_eok=1)
                observation["server"] = self.store.job["server"]
                ready = all(observation["snapshot"][k] is not None for k in ("shares", "mkt", "float"))
                self.store.finish(attempt, "complete" if ready else "missing", observation)
            except ValueError as exc:
                self.store.finish(attempt, "failed", envelope, str(exc))
        self.active = None
        return True

    def stop(self, reason):
        if self.active:
            self.store.finish(self.active[0], "failed", error=reason)
            self.active = None
        self.stopped = reason
