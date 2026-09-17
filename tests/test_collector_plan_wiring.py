"""tests/test_collector_plan_wiring.py — 수집기가 구독 계획을 받아 등록하는 경로

로그인 없이 가짜 Qt/OCX 로 확인한다. 실제 수신·저장·종료는 별도 실행 단계로 남는다.

확인하는 것:
  1. 명시적 NXT 모드에서만 계획을 받고 `_NX` 를 등록하는가
  2. 목록 근거·시장 프로필·구독 원문이 세션에 남는가
  3. 체결 전용 시계로 경고와 종료 판단이 모두 이루어지는가
  4. 등록 실패를 삼키지 않는가, 그리고 기존 정규장 경로가 그대로인가

실행:
    uv run pytest tests/test_collector_plan_wiring.py -v
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime

import pytest

from collector.kiwoom.market_sessions import PROFILE_NXT_AFTERMARKET
from collector.kiwoom.subscription_plan import (
    MODE_KRX_REGULAR,
    SymbolListSource,
    build_plan,
)
from tests.test_tick_collector_shutdown import collector  # 오프라인 Qt/OCX 대역

SOURCE = SymbolListSource(origin="사용자 입력", verified_at="2026-09-17T16:30:00+09:00")


def _plan(codes="005930_NX,000660_NX", profile="nxt_aftermarket", source=SOURCE):
    return build_plan("nxt", codes, source=source, market_profile=profile)


def _logger(collector, **kwargs):
    old, messages, _ = collector
    if kwargs.get("plan") is not None:
        kwargs.setdefault("duration_seconds", 60)
    return type(old)(code_revision="fixture", **kwargs), messages


@pytest.mark.parametrize("duration", [None, 0, -1, 301, True, float("inf")])
def test_unbounded_plan_is_rejected_before_ocx(collector, duration):
    with pytest.raises(ValueError, match="제한 시간"):
        _logger(collector, plan=_plan(), duration_seconds=duration)


@pytest.mark.parametrize("extra", [dict(storage="raw-v1"), dict(managed=object())])
def test_plan_requires_isolated_raw_v2(collector, extra):
    with pytest.raises(ValueError, match="독립 raw-v2"):
        _logger(collector, plan=_plan(), **extra)


def test_plan_duration_requests_shutdown_without_waiting_for_market_close(collector):
    import time
    from types import SimpleNamespace
    logger, _ = _logger(collector, plan=_plan(), duration_seconds=60)
    logger.writer = SimpleNamespace(pending=0)
    logger.monitor = SimpleNamespace(tick=lambda: None, sample_queue_depth=lambda _: None,
                                     silence_stop_reason=lambda: None)
    logger._subscribed_at = time.monotonic() - 61
    logger.is_running = True
    logger._stats_worker()
    assert logger._shutdown_requested == "제한 수집 시간 종료"


def _registrations(ocx):
    return [(args[0], args[1], args[2], args[3])
            for _, method, args in ocx.calls if method.startswith("SetRealReg")]


# ── 1. 계획을 받아 _NX 를 등록한다 ──────────────────────────────────────────

def test_계획의_코드만_등록하고_유니버스_조회를_하지_않는다(collector):
    logger, _ = _logger(collector, plan=_plan())
    logger.ocx.dynamicCall = lambda method, *a: ("0" if method.startswith("SetRealReg") else "1")
    logger.ocx.calls = []
    calls = []
    logger.ocx.dynamicCall = lambda method, *a: (calls.append((method, a)) or
                                                 ("0" if method.startswith("SetRealReg") else "1"))
    logger._register_all_universe()
    methods = [m for m, _ in calls]
    assert not any(m.startswith("GetCodeListByMarket") for m in methods)   # 조회 목록을 쓰지 않는다
    assert not any(m.startswith("GetMasterCodeName") for m in methods)
    registered = [a for m, a in calls if m.startswith("SetRealReg")]
    assert len(registered) == 1
    screen, codes, fids, opt = registered[0]
    assert codes == "005930_NX;000660_NX"          # 접미사 그대로 등록
    assert screen == "1000" and opt == "0"
    assert fids.startswith("20;10;15;14;27;28;21;") and fids.endswith(";80")


def test_전_종목_경로와_같은_FID_목록을_쓴다(collector):
    from collector.kiwoom.kiwoom_universe_logger import REAL_FIDS
    logger, _ = _logger(collector, plan=_plan())
    calls = []
    logger.ocx.dynamicCall = lambda method, *a: (calls.append((method, a)) or "0")
    logger._register_all_universe()
    assert [a for m, a in calls if m.startswith("SetRealReg")][0][2] == REAL_FIDS


def test_계획은_NXT_모드에서만_받는다(collector):
    krx = build_plan(MODE_KRX_REGULAR, "005930", source=SOURCE, market_profile="krx_regular")
    with pytest.raises(ValueError, match="NXT 모드에서만"):
        _logger(collector, plan=krx)


def test_계획과_codes를_함께_주면_거부한다(collector):
    with pytest.raises(ValueError, match="함께 줄 수 없다"):
        _logger(collector, plan=_plan(), codes=["005930"])


# ── 2. 목록 근거가 세션에 남는다 ────────────────────────────────────────────

def test_목록_근거와_시장_프로필과_구독_원문이_기록된다(collector, tmp_path):
    logger, messages = _logger(collector, plan=_plan())
    logger.ocx.dynamicCall = lambda method, *a: "0"
    logger._register_all_universe()
    text = "\n".join(messages)
    assert "사용자 입력" in text and "2026-09-17T16:30:00+09:00" in text
    assert "미확인 — 사용자 입력일 뿐이다" in text        # 입력 사실과 대상 확인을 구분
    assert "005930_NX" in text
    payload = _plan().describe()
    assert payload["source"]["origin"] == "사용자 입력"
    assert payload["market_profile"] == "nxt_aftermarket"
    assert payload["codes"] == ["005930_NX", "000660_NX"]
    assert payload["venue_resolution"] == "unverified"     # 접미사로 올리지 않는다
    assert payload["nxt_coverage"] == "unconfirmed"
    assert json.loads(json.dumps(payload, ensure_ascii=False))   # 그대로 직렬화된다


def test_감시기가_계획의_시장_프로필을_쓴다(collector):
    logger, _ = _logger(collector, plan=_plan(profile="nxt_aftermarket"))
    assert logger.monitor._sessions == PROFILE_NXT_AFTERMARKET
    plain, _ = _logger(collector)
    assert plain.monitor._sessions is None                 # 기본은 기존 단일 창


# ── 3. 체결 전용 시계 ───────────────────────────────────────────────────────

def _at(hour, minute=0, second=0):
    return datetime.combine(date.today(), dtime(hour, minute, second)).timestamp()


def test_종료_판단도_체결_전용_시계를_쓴다(collector):
    # 호가가 계속 들어와도 체결이 끊기면 종료 권고가 나와야 한다.
    logger, _ = _logger(collector, plan=_plan())
    now = [_at(16, 0)]
    logger.monitor._clock = lambda: now[0]
    logger.monitor.start()
    logger.monitor.mark_reception_expected()
    logger.monitor.on_trade()
    for _ in range(14):
        now[0] += 50
        logger.monitor.on_quote()                          # 호가만 계속 수신
    assert logger.monitor.silence_stop_reason() is not None
    assert now[0] - logger.monitor.last_trade_ts >= 600


def test_체결이_이어지면_종료_권고가_없다(collector):
    logger, _ = _logger(collector, plan=_plan())
    now = [_at(16, 0)]
    logger.monitor._clock = lambda: now[0]
    logger.monitor.start(); logger.monitor.mark_reception_expected()
    for _ in range(14):
        now[0] += 50
        logger.monitor.on_trade()
    assert logger.monitor.silence_stop_reason() is None


# ── 4. 실패 처리와 정규장 호환성 ────────────────────────────────────────────

def test_등록_거부를_삼키지_않는다(collector):
    logger, _ = _logger(collector, plan=_plan())
    logger.ocx.dynamicCall = lambda method, *a: "-300"      # 등록 거부
    with pytest.raises(RuntimeError, match="SetRealReg rejected"):
        logger._register_all_universe()


def test_계획_모드는_15시35분_정규장_종료를_쓰지_않는다(collector):
    logger, _ = _logger(collector, plan=_plan())
    assert logger.plan is not None
    plain, _ = _logger(collector)
    assert plain.plan is None                              # 정규장은 기존 종료 유지


def test_계획이_없으면_기존_전_종목_경로_그대로다(collector):
    logger, messages = _logger(collector)
    seen = []
    def call(method, *a):
        seen.append(method)
        if method.startswith("GetCodeListByMarket"):
            return "005930;000660;123456"
        if method.startswith("GetMasterCodeName"):
            return "테스트종목"
        return "0"
    logger.ocx.dynamicCall = call
    logger._register_all_universe()
    assert any(m.startswith("GetCodeListByMarket") for m in seen)   # 조회 목록을 다시 쓴다
    assert "구독 계획 등록" not in "\n".join(messages)


# ── 5. 계획 파일·콜백 저장 통합 (가짜 OCX, 로그인부터 저장까지) ─────────────

@pytest.fixture
def plan_live(collector, monkeypatch, tmp_path):
    """계획 모드로 로그인~저장 경로를 태운다. 실제 OCX·로그인은 없다."""
    from collector.kiwoom.live_capture import LiveRawCapture
    from control_tower.windows_process import ProcessFacts
    old, messages, _ = collector
    facts = ProcessFacts(123, "2026-09-17T00:00:00Z", "C:/fixture/python.exe", 32)
    monkeypatch.setitem(old._on_login.__globals__, "LiveRawCapture",
                        lambda root, **kw: LiveRawCapture(tmp_path, facts=facts, **kw))
    logger = type(old)(code_revision="fixture", plan=_plan(), duration_seconds=60)
    monkeypatch.setattr(logger, "_stats_worker", lambda: None)
    values = {20: "090000", 10: " -10000 ", 15: "+2", 14: "100000",
              27: "10001", 28: "10000", 21: "090001"}
    values.update({i: "10001" for i in range(41, 51)})
    values.update({i: "10000" for i in range(51, 61)})
    values.update({i: "3" for i in range(61, 81)})
    def call(method, *args):
        if method.startswith("GetCommRealData"):
            return values[args[1]]
        if method.startswith("GetConnectState"):
            return 1
        return "0"
    logger.ocx.dynamicCall = call
    yield logger, messages
    if not logger._shutdown_done:
        logger._shutdown("test cleanup")


def test_계획_파일이_세션_폴더에_실제로_쓰인다(plan_live):
    logger, _ = plan_live
    logger._on_login(0)
    record = logger.raw_capture.directory / "subscription_plan.json"
    assert record.is_file()
    saved = json.loads(record.read_text(encoding="utf-8"))
    assert saved["mode"] == "nxt"
    assert saved["codes"] == ["005930_NX", "000660_NX"]          # 구독 원문
    assert saved["market_profile"] == "nxt_aftermarket"
    assert saved["source"]["origin"] == "사용자 입력"
    assert saved["source"]["verified_at"] == "2026-09-17T16:30:00+09:00"
    assert saved["source"]["nxt_eligibility_confirmed"] is False  # 입력과 확인은 다르다
    assert saved["venue_resolution"] == "unverified"
    assert saved["nxt_coverage"] == "unconfirmed"


def test_콜백_코드의_접미사가_저장까지_보존된다(plan_live):
    from collector.raw_v2 import read_raw_v2
    logger, _ = plan_live
    logger._on_login(0)
    logger._on_receive_real_data("005930_NX", "주식체결", "")
    logger._on_receive_real_data("000660_NX", "주식호가잔량", "")
    logger._shutdown("test stop")
    with read_raw_v2(logger.db_path) as (_, rows):
        records = list(rows)
    trade = records[1]
    assert trade["event"].code == "005930_NX"       # 접미사가 원문 그대로 남는다
    assert trade["event"].venue == "unknown"        # 접미사가 붙어도 거래소는 확정하지 않는다
    quote = records[2]
    assert quote["event"].code == "000660_NX" and quote["event"].venue == "unknown"


def test_계획_모드도_저장을_정상_종료한다(plan_live):
    logger, _ = plan_live
    logger._on_login(0)
    logger._on_receive_real_data("005930_NX", "주식체결", "")
    logger._shutdown("test stop")
    assert logger.exit_code == 0
    assert logger.raw_capture.queue.snapshot()["state"] == "closed"


def test_계획_모드에서_구독하지_않은_코드가_와도_원문을_남긴다(plan_live):
    # 예상 밖 코드를 조용히 버리지 않는다. 대조는 기록을 보고 사람이 한다.
    from collector.raw_v2 import read_raw_v2
    logger, _ = plan_live
    logger._on_login(0)
    logger._on_receive_real_data("005930", "주식체결", "")     # 접미사 없는 코드가 도착
    logger._shutdown("test stop")
    with read_raw_v2(logger.db_path) as (_, rows):
        records = list(rows)
    assert records[1]["event"].code == "005930"
    assert records[1]["event"].venue == "unknown"
