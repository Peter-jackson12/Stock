from dataclasses import replace
from types import SimpleNamespace
import sqlite3

import pytest

from control_tower.capture_health import CaptureHealth
from tests.test_managed_capture import managed


def monitor(facts, clock):
    class Process:
        def __init__(self, pid):
            self.facts = facts
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def require_alive(self):
            pass
    return CaptureHealth(clock=lambda: clock[0], process_factory=Process)


def test_old_heartbeat_cannot_revive_restarted_manager(managed):
    store, facts, launch, token = managed
    store.claim(launch, token, facts)
    clock = [0]
    health = monitor(facts, clock)
    assert health.observe(store, store.get())["state"] == "unknown"
    store.poll(launch, facts, heartbeat=True)
    assert health.observe(store, store.get())["state"] == "responsive"
    restarted = monitor(facts, clock)
    assert restarted.observe(store, store.get())["state"] == "unknown"
    assert restarted.observe(store, store.get())["state"] == "unknown"
    store.poll(launch, facts, heartbeat=True)
    assert restarted.observe(store, store.get())["state"] == "responsive"


def test_duplicates_and_wall_clock_updates_do_not_extend_liveness(managed):
    store, facts, launch, token = managed
    store.claim(launch, token, facts)
    clock = [0]
    health = monitor(facts, clock)
    health.observe(store, store.get())
    store.poll(launch, facts, heartbeat=True)
    assert health.observe(store, store.get())["state"] == "responsive"
    clock[0] = 14
    store.request_stop(launch)  # updated timestamp alone is not a peer heartbeat.
    assert health.observe(store, store.get())["age_seconds"] == 14
    clock[0] = 15
    assert health.observe(store, store.get())["state"] == "unknown"
    assert store.get()["stop_accepted"] == 0


def test_reused_pid_does_not_pass_health_check(managed):
    store, facts, launch, token = managed
    store.claim(launch, token, facts)
    health = monitor(replace(facts, started_at_utc="2026-09-17T00:00:00Z"), [0])
    assert health.observe(store, store.get())["state"] == "unknown"
    assert store.get()["challenge"] is None


def test_reversed_heartbeat_cannot_keep_peer_responsive(managed):
    store, facts, launch, token = managed
    store.claim(launch, token, facts)
    health = monitor(facts, [0])
    health.observe(store, store.get())
    store.poll(launch, facts, heartbeat=True)
    assert health.observe(store, store.get())["state"] == "responsive"
    old = store.get()
    old["heartbeat_seq"] = 0
    assert health.observe(store, old)["state"] == "unknown"


def test_v1_migration_keeps_rows_and_needs_new_challenge(tmp_path):
    from control_tower.managed_capture import ManagedCaptures
    store = ManagedCaptures(tmp_path)
    store.path.parent.mkdir()
    with sqlite3.connect(store.path) as conn:
        conn.execute("CREATE TABLE launches (id TEXT PRIMARY KEY, plan TEXT NOT NULL, token_hash TEXT NOT NULL, allowed_executables TEXT NOT NULL, state TEXT NOT NULL, owner TEXT, identity TEXT, revision INTEGER, stop_id TEXT, stop_accepted INTEGER NOT NULL DEFAULT 0, final_report TEXT, error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL)")
        conn.execute("PRAGMA user_version=1")
        conn.execute("INSERT INTO launches (id,plan,token_hash,allowed_executables,state,created,updated) VALUES ('old','{}','hash','[]','closed','then','then')")
    store.create(["005930"], 60, "mock", ["C:/fixture/python.exe"])
    assert store.get("old")["state"] == "closed"
    assert store.get("old")["heartbeat_seq"] == 0
