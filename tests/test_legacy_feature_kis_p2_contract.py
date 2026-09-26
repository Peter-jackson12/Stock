"""tests/test_legacy_feature_kis_p2_contract.py — PIPELINE_AUDIT 2026-09-26 legacy P2 회귀

legacy feature 저장(features/store.py, scripts/build_features.py)과 legacy KIS 일일 데몬
(collector/run_daily_daemon.py) 의 세 감사 발견을 tmp_path·합성 DataFrame·monkeypatch 로 고정한다.
실제 KIS API/websocket, Kiwoom, 실제 LOB/raw DB, 실제 Daily CSV 는 쓰지 않는다.
MWFD/Fast 경로를 검증하지 않는다.

A. manifest 갱신이 다른 날짜 coverage 를 지우지 않는다(병합·원자적 교체).
B. --strict 커버리지 실패는 게시 전에 멈춘다(validate before publish).
C. 데몬은 모든 필수 단계가 성공일 때만 완전 성공을 출력한다.

실행:
    uv run pytest tests/test_legacy_feature_kis_p2_contract.py -v
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

from collector import daily_daemon_result as r
from features.base import FeatureSet
from features.builders.microstructure import RollingBuyVolMean
from features.store import FeatureStore


def frame(value=1.0, codes=("005930",)):
    rows = [{"code": code, "time": f"09000{i}", "x": value} for code in codes for i in range(3)]
    return pd.DataFrame(rows)


@pytest.fixture
def feature_set():
    return FeatureSet(version="fs_v1", features=(RollingBuyVolMean(5),))


@pytest.fixture
def store(tmp_path):
    return FeatureStore(root=tmp_path / "features")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── A. manifest coverage 보존 ───────────────────────────────────────────────

def test_다음_날짜_write가_이전_날짜_coverage를_지우지_않는다(store, feature_set):
    """감사 발견: write() 가 feature_set.manifest() 로 매니페스트 전체를 덮어썼다."""
    store.write("20260910", frame(), feature_set)
    store.record_coverage("20260910", {"min_code_coverage_pct": 100.0})
    store.write("20260911", frame(), feature_set, coverage={"min_code_coverage_pct": 97.0})
    store.write("20260912", frame(), feature_set)
    coverage = store.read_manifest()["coverage"]
    assert coverage == {"20260910": {"min_code_coverage_pct": 100.0},
                        "20260911": {"min_code_coverage_pct": 97.0}}


def test_같은_날짜를_다시_쓰면_그_날짜_coverage만_교체된다(store, feature_set):
    store.write("20260910", frame(), feature_set, coverage={"v": "old-10"})
    store.write("20260911", frame(), feature_set, coverage={"v": "old-11"})
    store.write("20260910", frame(2.0), feature_set)          # coverage 없이 재기록
    assert store.read_manifest()["coverage"] == {"20260911": {"v": "old-11"}}   # 낡은 값은 남기지 않음
    store.write("20260910", frame(3.0), feature_set, coverage={"v": "new-10"})
    assert store.read_manifest()["coverage"] == {"20260910": {"v": "new-10"}, "20260911": {"v": "old-11"}}


def test_피처_선언_검증은_그대로다(store, feature_set):
    store.write("20260910", frame(), feature_set, coverage={"v": 1})
    other = FeatureSet(version="fs_v2", features=(RollingBuyVolMean(5),))
    with pytest.raises(ValueError, match="피처셋 버전"):
        store.write("20260911", frame(), other)
    manifest = store.read_manifest()
    assert manifest["feature_set_version"] == "fs_v1" and manifest["coverage"] == {"20260910": {"v": 1}}
    assert not store.path_for("20260911").exists()


def test_다른_피처셋으로_추가해도_기존_선언과_coverage를_보존한다(store, feature_set):
    store.write("20260910", frame(), feature_set, coverage={"v": 1})
    extra = FeatureSet(version="fs_v1", features=(RollingBuyVolMean(10),))
    store.write("20260911", frame(), extra)
    names = {entry["name"] for entry in store.read_manifest()["features"]}
    assert names == {RollingBuyVolMean(5).name, RollingBuyVolMean(10).name}
    assert store.read_manifest()["coverage"] == {"20260910": {"v": 1}}


def test_매니페스트_교체_중_실패해도_반쪽_JSON이_남지_않는다(store, feature_set, monkeypatch):
    store.write("20260910", frame(), feature_set, coverage={"v": 1})
    before = store.manifest_path.read_bytes()
    import features.store as module
    def broken_replace(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(module.os, "replace", broken_replace)
    with pytest.raises(OSError):
        store.record_coverage("20260911", {"v": 2})
    assert store.manifest_path.read_bytes() == before
    assert json.loads(before)                                        # 온전한 JSON
    assert not list(store.root.glob("*.tmp")) and not list(store.root.glob(".*.tmp"))


def test_parquet_교체_중_실패하면_기존_파일과_manifest가_남는다(store, feature_set, monkeypatch):
    store.write("20260910", frame(), feature_set, coverage={"v": 1})
    parquet, manifest = digest(store.path_for("20260910")), store.manifest_path.read_bytes()
    import features.store as module
    real = module.os.replace
    def fail_parquet(src, dst):
        if str(dst).endswith(".parquet"):
            raise OSError("interrupted")
        return real(src, dst)
    monkeypatch.setattr(module.os, "replace", fail_parquet)
    with pytest.raises(OSError):
        store.write("20260910", frame(9.0), feature_set, coverage={"v": 2})
    assert digest(store.path_for("20260910")) == parquet
    # 재기록 전 낡은 coverage 는 먼저 지웠다 — '모름' 이지 틀린 값이 아니다.
    assert "20260910" not in store.read_manifest().get("coverage", {})
    assert store.available_dates() == ["20260910"]                   # 임시 파일은 날짜로 안 보인다
    assert manifest != store.manifest_path.read_bytes()


# ── B. strict validate before publish ─────────────────────────────────────

LOW = {"total_codes": 1, "total_rows": 3, "macro_features": ["m"], "min_code_coverage_pct": 10.0,
       "per_feature": {"m": {"code_coverage_pct": 10.0, "row_coverage_pct": 10.0, "codes_with_value": 0}}}
HIGH = {"total_codes": 1, "total_rows": 3, "macro_features": ["m"], "min_code_coverage_pct": 100.0,
        "per_feature": {"m": {"code_coverage_pct": 100.0, "row_coverage_pct": 100.0, "codes_with_value": 1}}}


@pytest.fixture
def build(tmp_path, monkeypatch, feature_set):
    """build_day 의 LOB 읽기·피처 계산만 합성으로 바꾼다. 게시·판정 로직은 실제 코드."""
    import scripts.build_features as bf
    lob = tmp_path / "lob"
    lob.mkdir()
    monkeypatch.setattr(bf, "lob_path", lambda date: lob / f"{date}_LOB.db")
    monkeypatch.setattr(bf, "list_codes", lambda conn: ["005930"])
    monkeypatch.setattr(bf, "load_code", lambda conn, code: pd.DataFrame({"t": [1, 2, 3]}))
    monkeypatch.setattr(bf, "FeatureSet", lambda version, features: feature_set)
    monkeypatch.setattr(bf, "registry", types.SimpleNamespace(bootstrap=lambda: None, all_features=lambda: {}))
    state = {"value": 9.0, "coverage": LOW}
    monkeypatch.setattr(bf, "build_code_frame", lambda code, raw, date, ordered: frame(state["value"]))
    monkeypatch.setattr(bf, "measure_coverage", lambda day, ordered: state["coverage"])
    root = tmp_path / "features"

    def run(date, *, strict, coverage, value=9.0):
        (lob / f"{date}_LOB.db").touch()
        state.update(value=value, coverage=coverage)
        return bf.build_day(date, feature_root=str(root), strict=strict)
    return run, FeatureStore(root=root)


def _seed(store, feature_set):
    store.write("20260910", frame(1.0), feature_set, coverage={"seed": "10"})
    store.write("20260911", frame(1.0), feature_set, coverage={"seed": "11"})


def test_strict_FAIL은_기존_parquet_bytes를_바꾸지_않는다(build, feature_set):
    run, store = build
    _seed(store, feature_set)
    before = digest(store.path_for("20260911"))
    with pytest.raises(SystemExit, match="게시하지 않음"):
        run("20260911", strict=True, coverage=LOW)
    assert digest(store.path_for("20260911")) == before


def test_strict_FAIL은_manifest와_여러_날짜_coverage를_바꾸지_않는다(build, feature_set):
    run, store = build
    _seed(store, feature_set)
    manifest = store.manifest_path.read_bytes()
    with pytest.raises(SystemExit):
        run("20260911", strict=True, coverage=LOW)
    with pytest.raises(SystemExit):
        run("20260912", strict=True, coverage=LOW)                # 새 날짜도 게시 안 함
    assert store.manifest_path.read_bytes() == manifest
    assert store.available_dates() == ["20260910", "20260911"]
    assert not list(store.root.glob(".*.tmp"))


def test_strict_PASS는_새_parquet와_coverage를_게시하고_과거를_보존한다(build, feature_set):
    run, store = build
    _seed(store, feature_set)
    path = run("20260912", strict=True, coverage=HIGH, value=5.0)
    assert path == store.path_for("20260912")
    assert store.read("20260912")["x"].tolist() == [5.0, 5.0, 5.0]
    coverage = store.read_manifest()["coverage"]
    assert coverage["20260912"] == HIGH
    assert coverage["20260910"] == {"seed": "10"} and coverage["20260911"] == {"seed": "11"}


def test_non_strict_저커버리지는_기존처럼_경고하고_게시한다(build, feature_set, capsys):
    run, store = build
    _seed(store, feature_set)
    path = run("20260911", strict=False, coverage=LOW, value=7.0)
    assert path is not None and store.read("20260911")["x"].tolist() == [7.0, 7.0, 7.0]
    assert store.read_manifest()["coverage"]["20260911"] == LOW       # 경고 근거를 함께 기록
    assert store.read_manifest()["coverage"]["20260910"] == {"seed": "10"}
    assert "⚠️" in capsys.readouterr().out


# ── C. KIS 데몬 단계별 결과 ────────────────────────────────────────────────

LOB_OK = {"index_sec": 1.0, "resample_sec": 2.0, "codes": 3, "empty": ["000001"], "failures": [], "rows": 120}
DAILY_OK = {"status": "completed", "targets": 2, "priced": 2, "journal": "j.jsonl",
            "trading_dates": 5, "snapshots": 2}


def test_LOB_반환_계약을_그대로_판정한다():
    assert r.lob_step(LOB_OK).status == r.SUCCEEDED
    assert r.lob_step(None).status == r.FAILED                           # 원본 틱 파일 없음
    assert r.lob_step(LOB_OK | {"failures": [("005930", "tb")]}).status == r.FAILED
    assert r.lob_step(LOB_OK | {"codes": 0, "empty": [], "rows": 0}).status == r.NO_DATA
    assert r.lob_step(LOB_OK | {"codes": 1, "empty": ["000001"], "rows": 0}).status == r.NO_DATA
    assert r.lob_step({"rows": 1}).status == r.UNVERIFIED


def test_일봉_반환_계약을_그대로_판정한다():
    assert r.daily_step(DAILY_OK).status == r.SUCCEEDED
    assert r.daily_step(DAILY_OK | {"status": "no_data", "priced": 0}).status == r.NO_DATA
    assert r.daily_step(DAILY_OK | {"priced": 1}).status == r.PARTIAL
    assert r.daily_step(None).status == r.UNVERIFIED                     # 이전 revision 반환 없음
    assert r.daily_step(DAILY_OK | {"status": "weird"}).status == r.UNVERIFIED


def test_raw_단계_판정():
    assert r.raw_capture_step(approval_ok=False, tick_count=0).status == r.FAILED
    assert r.raw_capture_step(approval_ok=True, tick_count=0).status == r.NO_DATA
    assert r.raw_capture_step(approval_ok=True, tick_count=5).status == r.SUCCEEDED


def _outcome(*steps):
    return r.DaemonOutcome(list(steps))


def test_완전_성공_문구는_모든_필수_단계_성공일_때만_나온다():
    ok = _outcome(r.raw_capture_step(approval_ok=True, tick_count=5), r.lob_step(LOB_OK), r.daily_step(DAILY_OK))
    assert ok.complete and ok.status == "complete" and ok.exit_code == 0
    assert "완벽히 끝났습니다" in ok.lines()[-1]
    for broken in (r.lob_step(None), r.lob_step(LOB_OK | {"codes": 0, "empty": [], "rows": 0}),
                   r.daily_step(None), r.daily_step(DAILY_OK | {"priced": 1})):
        steps = [s if s.name != broken.name else broken for s in ok.steps]
        outcome = _outcome(*steps)
        assert not outcome.complete and outcome.exit_code == 1, broken
        assert outcome.status == "partial"
        assert not any("완벽히" in line for line in outcome.lines())


def test_단계가_빠지면_완전_성공이_아니다():
    outcome = _outcome(r.raw_capture_step(approval_ok=True, tick_count=5), r.lob_step(LOB_OK))
    assert not outcome.complete and outcome.status_of(r.DAILY_UPDATE) == r.NOT_RUN
    failed = _outcome(r.raw_capture_step(approval_ok=False, tick_count=0))
    assert failed.status == "failed" and failed.exit_code == 1
    assert "수집 실패" in failed.lines()[-1]


def test_post_process_예외는_실패로_남기고_전파하며_뒤_단계는_실행하지_않는다():
    outcome = _outcome(r.raw_capture_step(approval_ok=True, tick_count=5))
    lines, calls = [], []
    def resample(date):
        raise TypeError("numeric column polluted")
    class Daily:
        def collect(self, **kw):
            calls.append(kw)
            return DAILY_OK
    with pytest.raises(TypeError):
        r.post_process(outcome, "20260912", ["005930"], resample=resample, collector_factory=Daily,
                       daily_start="20220420", emit=lines.append)
    assert outcome.status_of(r.LOB_BUILD) == r.FAILED and "TypeError" in outcome.steps[-1].detail
    assert outcome.status_of(r.DAILY_UPDATE) == r.NOT_RUN and not calls
    assert any("미실행" in line for line in lines) and not any("완벽히" in line for line in lines)


def test_post_process는_일봉_실패를_성공으로_두갑시키지_않는다():
    outcome = _outcome(r.raw_capture_step(approval_ok=True, tick_count=5))
    class Daily:
        def collect(self, **kw):
            return {"status": "no_data", "targets": 1, "priced": 0}
    r.post_process(outcome, "20260912", ["005930"], resample=lambda d: LOB_OK, collector_factory=Daily,
                   daily_start="20220420", emit=lambda line: None)
    assert outcome.status_of(r.LOB_BUILD) == r.SUCCEEDED
    assert outcome.status_of(r.DAILY_UPDATE) == r.NO_DATA and not outcome.complete


# ── C. 데몬 배선 (KIS/websocket 없이) ─────────────────────────────────────

@pytest.fixture
def daemon_module(tmp_path, monkeypatch):
    if "websockets" not in sys.modules:
        monkeypatch.setitem(sys.modules, "websockets", types.ModuleType("websockets"))
    import collector.run_daily_daemon as daemon
    monkeypatch.setattr(daemon, "RAW_DIR", tmp_path / "raw")
    (tmp_path / "raw").mkdir()

    class FakeWS:
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def send(self, message): pass
        async def recv(self): raise asyncio.TimeoutError

    clock = iter(["090000"])
    class FakeNow:
        def strftime(self, fmt):
            return "20260912" if fmt == "%Y%m%d" else next(clock, "153500")
    monkeypatch.setattr(daemon, "datetime", types.SimpleNamespace(now=lambda: FakeNow()))
    monkeypatch.setattr(daemon, "websockets", types.SimpleNamespace(connect=lambda *a, **k: FakeWS()))
    return daemon


def test_데몬은_LOB_실패_뒤에_완료_문구를_출력하지_않고_비정상_종료값을_준다(daemon_module, monkeypatch, capsys):
    monkeypatch.setattr(daemon_module, "resample_raw_to_lob", lambda date: None)
    monkeypatch.setattr(daemon_module, "FastDailyCollector", lambda: types.SimpleNamespace(collect=lambda **kw: DAILY_OK))
    daemon = daemon_module.FullAutoCollector(["005930"])
    monkeypatch.setattr(daemon, "get_approval_key", lambda: "fixture-key")
    outcome = asyncio.run(daemon.run())
    out = capsys.readouterr().out
    assert outcome.exit_code == 1 and outcome.status_of(r.LOB_BUILD) == r.FAILED
    assert "완벽히 끝났습니다" not in out and "1초봉 LOB 변환: 실패" in out


def test_데몬은_승인키_실패면_후처리를_하지_않고_실패로_끝난다(daemon_module, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(daemon_module, "resample_raw_to_lob", lambda date: calls.append(date))
    daemon = daemon_module.FullAutoCollector(["005930"])
    monkeypatch.setattr(daemon, "get_approval_key", lambda: "")
    outcome = asyncio.run(daemon.run())
    assert outcome.status == "failed" and outcome.exit_code == 1 and not calls
    assert "수집 실패" in capsys.readouterr().out


def test_일봉_collect는_journal과_같은_종료_사실을_반환한다(tmp_path, monkeypatch):
    import collector.daily_collector as collector
    monkeypatch.setattr(collector, "observation_date", lambda: "20260916")
    monkeypatch.setattr(collector, "fetch_stock_meta_and_candles",
                        lambda code, count=4000, include_meta=True: ("x", pd.DataFrame(), {}))
    # 관측일이 요청 범위 밖이고 가격도 없으면 journal 은 no_data 로 끝난다.
    result = collector.FastDailyCollector(tmp_path).collect("20260901", "20260902", ["005930"])
    assert result["status"] == "no_data" and result["priced"] == 0 and result["targets"] == 1
    events = [json.loads(line)["event"] for line in Path(result["journal"]).read_text(encoding="utf-8").splitlines()]
    assert events[-1] == "no_data"
    assert r.daily_step(result).status == r.NO_DATA
    # 관측일 스냅샷만 있고 가격이 없으면 journal 은 completed 지만 가격 수신은 0/1 — 완전 성공이 아니다.
    result = collector.FastDailyCollector(tmp_path).collect("20260916", "20260916", ["005930"])
    events = [json.loads(line)["event"] for line in Path(result["journal"]).read_text(encoding="utf-8").splitlines()]
    assert result["status"] == events[-1] == "completed" and result["priced"] == 0
    assert r.daily_step(result).status == r.PARTIAL
