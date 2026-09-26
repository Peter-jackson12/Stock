"""tests/test_operator_p2_contract.py — PIPELINE_AUDIT 2026-09-26 Operator/managed capture P2 회귀

실제 collector/OCX/로그인/프로세스 kill 없이 tmp_path 와 합성 프로세스 대역만 쓴다.

A. 작업 제어 화면: 미완료 작업은 최근 30개 이력 창과 무관하게 조회된다.
B. managed capture: Popen 성공만으로 launch 성공이 아니다. claim 전에 종료한 자식은
   launching 에 고착되지 않고 failed 로 수렴하며, 다음 launch 를 막지 않는다.

실행:
    uv run pytest tests/test_operator_p2_contract.py -v
"""
from __future__ import annotations

from dataclasses import replace
import sqlite3
import subprocess

import pytest

from control_tower.jobs import ACTIVE_STATUSES, JobStore
from control_tower.managed_capture import ManagedCaptures, start_managed_capture
from control_tower.windows_process import ProcessFacts


# ── A. active jobs 와 recent history 분리 ───────────────────────────────────

def _age(store, job_id, created_at, status=None):
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (created_at, job_id))
        if status is not None:
            conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))


@pytest.fixture
def crowded(tmp_path):
    """오래된 미완료 작업 3개 뒤에 더 새로운 종료 작업 35개."""
    store = JobStore(tmp_path)
    old = {}
    for index, status in enumerate(("queued", "planned", "running")):
        kind = "inspect_result" if status != "planned" else "replay_raw_v2"
        job = store.submit(kind, {"path": f"old-{status}.json"})
        _age(store, job, f"2000-01-0{index + 1}T00:00:00+00:00", status)
        old[status] = job
    terminal = ("succeeded", "failed", "cancelled")
    for index in range(35):
        job = store.submit("inspect_result", {"path": f"new-{index}.json"})
        _age(store, job, f"2026-09-26T00:{index:02d}:00+00:00", terminal[index % 3])
    return store, old


def test_오래된_미완료_작업은_newer_완료_31개_이상에도_조회된다(crowded):
    store, old = crowded
    recent_ids = {job["id"] for job in store.recent()}
    assert not recent_ids & set(old.values())              # 감사 발견: 최근 30개 창 밖
    active = store.active()
    assert [job["id"] for job in active] == [old["queued"], old["planned"], old["running"]]
    assert {job["status"] for job in active} <= set(ACTIVE_STATUSES)


def test_dedup이_돌려준_오래된_작업을_찾을_수_있다(crowded):
    store, old = crowded
    again = store.submit("inspect_result", {"path": "old-queued.json"})
    assert again == old["queued"]
    assert store.get(again)["status"] == "queued"
    assert again in {job["id"] for job in store.active()}


def test_종료_이력은_기존_상한을_유지하고_미완료를_섞지_않는다(crowded):
    store, old = crowded
    finished = store.finished()
    assert len(finished) == 30
    assert all(job["status"] not in ACTIVE_STATUSES for job in finished)
    assert finished[0]["created_at"] > finished[-1]["created_at"]      # 최신순
    assert len(store.recent()) == 30                                    # 기존 API 불변
    for limit in (0, 101, True, "30"):
        for method in (store.active, store.finished, store.recent):
            with pytest.raises(ValueError):
                method(limit)


def test_취소와_완료는_active로_표시하지_않는다(tmp_path):
    store = JobStore(tmp_path)
    cancelled = store.submit("replay_raw_v2", {"db": "a.db"})
    assert store.cancel(cancelled)
    done = store.submit("inspect_result", {"path": "done.json"})
    job = store.claim_inspection("worker")
    store.complete(job["id"], "worker", result={"ok": True})
    assert store.active() == []
    assert {j["id"] for j in store.finished()} == {cancelled, done}


def test_빈_DB는_읽기만으로_파일을_만들지_않는다(tmp_path):
    store = JobStore(tmp_path)
    assert store.active() == [] and store.finished() == [] and store.get("missing") is None
    assert not store.path.exists()


def test_조회는_읽기_전용이다(crowded):
    store, _ = crowded
    before = store.path.read_bytes()
    store.active(); store.finished(); store.get("x")
    assert store.path.read_bytes() == before


def test_손상된_payload는_조회를_숨기지_않고_오류로_드러난다(tmp_path):
    store = JobStore(tmp_path)
    job = store.submit("inspect_result", {"path": "p.json"})
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE jobs SET payload='{broken' WHERE id=?", (job,))
    for call in (store.active, store.recent, lambda: store.get(job)):
        with pytest.raises(ValueError):                    # 기존 recent()와 같은 계약
            call()


def test_작업_이력_화면이_active와_종료_이력을_분리한다(crowded, monkeypatch):
    from tests.test_control_tower_ui import screen
    store, old = crowded
    app = screen(store.path.parents[1])
    assert not app.exception
    labels = [item.label for item in app.expander]
    assert any(old["queued"][:8] in label for label in labels)
    assert any(old["running"][:8] in label for label in labels)
    markdown = " ".join(item.value for item in app.markdown)
    assert "**진행 중·대기 작업** · 3건" in markdown and "**최근 종료 이력** · 30건" in markdown


# ── B. managed capture 초기화 실패 전달 ─────────────────────────────────────

SPAWNED = ProcessFacts(4242, "2026-09-26T00:00:00.000000Z", "C:/fixture/.venv32/Scripts/python.exe", 32)
CHILD = ProcessFacts(4343, "2026-09-26T00:00:01.000000Z", "C:/fixture/base/python.exe", 32)


def _environment(root):
    for relative in (".venv32/Scripts/python.exe", "collector/kiwoom/kiwoom_universe_logger.py", "base/python.exe"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (root / ".venv32/pyvenv.cfg").write_text(f"home = {root / 'base'}")


class FakeChild:
    def __init__(self, pid=SPAWNED.pid, code=None):
        self.pid, self.code = pid, code
    def poll(self):
        return self.code


class FakeProcess:
    """WindowsProcess 대역. alive 에 있는 PID만 그 facts 로 열린다."""
    def __init__(self, alive, pid):
        if pid not in alive:
            exc = OSError("The parameter is incorrect")
            exc.winerror = 87
            raise exc
        self.facts = alive[pid]
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def factory(alive):
    return lambda pid: FakeProcess(alive, pid)


def _launch(root, child, alive, popen_log=None):
    _environment(root)
    def popen(args, **kw):
        if popen_log is not None:
            kw["stdout"].write(popen_log)
        return child
    return start_managed_capture(root, ["005930"], 60, "mock", popen=popen, process_factory=factory(alive))


def test_spawn_예외는_기존대로_unknown이며_명시적_취소로만_풀린다(tmp_path):
    _environment(tmp_path)
    def broken(*args, **kw):
        raise OSError("CreateProcess failed")
    with pytest.raises(OSError):
        start_managed_capture(tmp_path, ["005930"], 60, "mock", popen=broken, process_factory=factory({}))
    store = ManagedCaptures(tmp_path)
    current = store.get()
    assert current["state"] == "unknown" and "CreateProcess failed" in current["error"]
    assert current["spawn"] is None
    store.request_stop(current["id"])
    assert store.get()["state"] == "cancelled"
    store.create(["005930"], 60, "mock", [str(tmp_path / "x.exe")])     # 다음 launch 가능


def test_spawn_직후_이미_종료한_자식은_즉시_failed로_수렴한다(tmp_path):
    launch = _launch(tmp_path, FakeChild(code=1), {}, popen_log=b"ImportError: No module named PyQt5\n")
    current = ManagedCaptures(tmp_path).get(launch)
    assert current["state"] == "failed"
    assert "exit code 1" in current["error"] and "PyQt5" in current["error"]
    assert len(current["error"]) <= 1024


def test_claim_전에_종료한_자식은_reconcile로_failed가_되고_capability가_무효화된다(tmp_path):
    alive = {SPAWNED.pid: SPAWNED}
    launch = _launch(tmp_path, FakeChild(), alive)
    store = ManagedCaptures(tmp_path)
    assert store.get(launch)["state"] == "launching" and store.get(launch)["spawn"]["pid"] == SPAWNED.pid
    assert store.reconcile_unclaimed(launch, process_factory=factory(alive)) is None   # 아직 살아 있음
    assert store.get(launch)["state"] == "launching"
    with pytest.raises(ValueError, match="still alive"):
        store.reconcile(launch, process_factory=factory(alive))

    del alive[SPAWNED.pid]                                               # 초기화 전 종료
    assert store.reconcile(launch, process_factory=factory(alive)) == "failed"
    current = store.get(launch)
    assert current["state"] == "failed" and "before claiming" in current["error"]
    with pytest.raises(ValueError):                                      # 늦은 자식은 claim 불가
        store.claim(launch, "token-unused", CHILD)
    store.create(["005930"], 60, "mock", [CHILD.executable])             # 영구 차단 없음


def test_PID_재사용은_정상_자식으로_오인하지_않는다(tmp_path):
    alive = {SPAWNED.pid: SPAWNED}
    launch = _launch(tmp_path, FakeChild(), alive)
    reused = replace(SPAWNED, started_at_utc="2026-09-26T05:00:00.000000Z", executable="C:/Windows/notepad.exe")
    alive[SPAWNED.pid] = reused
    store = ManagedCaptures(tmp_path)
    assert store.reconcile_unclaimed(launch, process_factory=factory(alive)) == "failed"
    assert "different process" in store.get(launch)["error"]


def test_접근_거부는_종료_근거가_아니다(tmp_path):
    alive = {SPAWNED.pid: SPAWNED}
    launch = _launch(tmp_path, FakeChild(), alive)
    def denied(pid):
        exc = OSError("access denied")
        exc.winerror = 5
        raise exc
    store = ManagedCaptures(tmp_path)
    with pytest.raises(OSError):
        store.reconcile_unclaimed(launch, process_factory=denied)
    assert store.get(launch)["state"] == "launching"


def test_정상_claim_뒤_종료는_기존_소유권_재대조를_따른다(tmp_path):
    alive = {SPAWNED.pid: SPAWNED, CHILD.pid: CHILD}
    _environment(tmp_path)
    store = ManagedCaptures(tmp_path)
    launch, token = store.create(["005930"], 60, "mock", [SPAWNED.executable, CHILD.executable])
    store.record_spawn(launch, SPAWNED)
    store.claim(launch, token, CHILD)                                    # 실제 claim 은 base python
    del alive[SPAWNED.pid]                                               # launcher 종료도
    assert store.startup_failed(launch, "late parent observation") is False
    with pytest.raises(ValueError):
        store.reconcile_unclaimed(launch, process_factory=factory(alive))
    with pytest.raises(ValueError, match="still alive"):                  # claim 한 자식 기준으로 판정
        store.reconcile(launch, process_factory=factory(alive))
    del alive[CHILD.pid]
    assert store.reconcile(launch, process_factory=factory(alive)) == "failed"
    assert "complete durable close report" in store.get(launch)["error"]


def test_spawn_identity가_없으면_추측하지_않는다(tmp_path):
    store = ManagedCaptures(tmp_path)
    launch, _ = store.create(["005930"], 60, "mock", [SPAWNED.executable])
    with pytest.raises(ValueError, match="not recorded"):
        store.reconcile_unclaimed(launch, process_factory=factory({}))
    assert store.get(launch)["state"] == "launching"


def test_duplicate_launch_보호는_유지된다(tmp_path):
    alive = {SPAWNED.pid: SPAWNED}
    _launch(tmp_path, FakeChild(), alive)
    with pytest.raises(sqlite3.IntegrityError):
        _launch(tmp_path, FakeChild(pid=5555), alive)
    assert [row["state"] for row in _launches(tmp_path)] == ["launching"]


def _launches(root):
    with sqlite3.connect(ManagedCaptures(root).path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT state FROM launches ORDER BY rowid").fetchall()


def test_이전_revision_DB도_읽고_spawn_없이_열린다(tmp_path):
    store = ManagedCaptures(tmp_path)
    launch, _ = store.create(["005930"], 60, "mock", [SPAWNED.executable])
    with sqlite3.connect(store.path) as conn:
        conn.execute("DROP TABLE spawns")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2    # 스키마 버전 불변
    assert store.get(launch)["spawn"] is None


def test_관리_화면은_claim_전_종료를_failed로_보여주고_다시_시작할_수_있다(tmp_path, monkeypatch):
    from tests.test_control_tower_ui import button, screen
    alive = {SPAWNED.pid: SPAWNED}
    launch = _launch(tmp_path, FakeChild(), alive)
    original = ManagedCaptures.reconcile_unclaimed
    monkeypatch.setattr(ManagedCaptures, "reconcile_unclaimed",
                        lambda self, launch_id: original(self, launch_id, process_factory=factory(alive)))
    app = screen(tmp_path)
    assert not app.exception
    assert button(app, "소규모 수집 시작 · 로그인").disabled                # 살아 있으면 대기
    assert any("초기화 대기" in item.value for item in app.caption)
    del alive[SPAWNED.pid]
    button(app, "상태 새로고침").click().run()
    assert not app.exception
    assert ManagedCaptures(tmp_path).get(launch)["state"] == "failed"
    assert any("before claiming" in item.value for item in app.error)
    assert not button(app, "소규모 수집 시작 · 로그인").disabled
