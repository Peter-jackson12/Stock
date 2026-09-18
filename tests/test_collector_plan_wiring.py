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


# ── 6. 애프터마켓 전환 (수집기 수준, 가짜 OCX) ─────────────────────────────

@pytest.fixture
def transition_live(collector, monkeypatch, tmp_path):
    """정규장으로 시작해 애프터마켓 전환 계획을 들고 있는 수집기."""
    from collector.kiwoom.live_capture import LiveRawCapture
    from control_tower.windows_process import ProcessFacts
    old, messages, _ = collector
    facts = ProcessFacts(123, "2026-09-17T00:00:00Z", "C:/fixture/python.exe", 32)
    monkeypatch.setitem(old._on_login.__globals__, "LiveRawCapture",
                        lambda root, **kw: LiveRawCapture(tmp_path, facts=facts, **kw))
    monkeypatch.setitem(old._on_login.__globals__, "PROJECT_ROOT", tmp_path)
    logger = type(old)(code_revision="fixture", aftermarket_plan=_plan(), aftermarket_duration_seconds=60)
    monkeypatch.setattr(logger, "_stats_worker", lambda: None)
    monkeypatch.setattr(logger, "_register_all_universe", lambda: None)
    values = {20: "090000", 10: " -10000 ", 15: "+2", 14: "100000",
              27: "10001", 28: "10000", 21: "090001"}
    values.update({i: "10001" for i in range(41, 51)})
    values.update({i: "10000" for i in range(51, 61)})
    values.update({i: "3" for i in range(61, 81)})
    logger.ocx.dynamicCall = lambda method, *a: (
        values[a[1]] if method.startswith("GetCommRealData")
        else 1 if method.startswith("GetConnectState") else "0")
    yield logger, messages
    if not logger._shutdown_done:
        logger._shutdown("test cleanup")


def test_전환_계획은_명시할_때만_준비된다(collector):
    plain, _ = _logger(collector)
    assert plain.aftermarket_plan is None and plain.transition is None
    with pytest.raises(ValueError, match="함께 줄 수 없다"):
        _logger(collector, plan=_plan(), aftermarket_plan=_plan())


def test_정규장_저장을_닫은_뒤에_새_파일을_연다(transition_live):
    logger, messages = transition_live
    logger._on_login(0)
    logger._on_receive_real_data("005930", "주식체결", "")
    regular = logger.raw_capture
    regular_path = regular.path
    record = logger.run_aftermarket_transition()
    assert record.succeeded, record.error
    # 정규장 저장은 닫혔고 마무리 정보가 있다
    snapshot = regular.queue.snapshot()
    assert snapshot["state"] == "closed" and snapshot["finalization"]
    # 새 파일은 다른 경로다 — 닫힌 파일을 다시 열지 않는다
    assert logger.raw_capture is not regular and logger.db_path != regular_path
    assert (logger.raw_capture.directory / "subscription_plan.json").is_file()
    assert logger._transition_record_path.is_file()
    assert "애프터마켓 전환 완료" in "\n".join(messages)


def test_전환_뒤_수신은_새_파일에_쌓인다(transition_live):
    from collector.raw_v2 import read_raw_v2
    logger, _ = transition_live
    logger._on_login(0)
    logger._on_receive_real_data("005930", "주식체결", "")
    regular_path = logger.raw_capture.path
    logger.run_aftermarket_transition()
    logger._on_receive_real_data("005930_NX", "주식체결", "")
    after_path = logger.raw_capture.path
    logger._shutdown("test stop")
    with read_raw_v2(regular_path) as (_, rows):
        before = [r for r in rows if r["event"].__class__.__name__ == "OrderedTick"]
    with read_raw_v2(after_path) as (_, rows):
        after = [r for r in rows if getattr(r["event"], "code", None)]
    assert [r["event"].code for r in before] == ["005930"]
    assert [r["event"].code for r in after] == ["005930_NX"]


def test_마무리가_확인되지_않으면_새_파일을_열지_않는다(transition_live, monkeypatch):
    logger, messages = transition_live
    logger._on_login(0)
    regular = logger.raw_capture
    monkeypatch.setattr(regular, "finish", lambda *a, **k: False)   # 마무리 미확인
    record = logger.run_aftermarket_transition()
    assert not record.succeeded
    assert record.failed_step == "finalize_regular"
    assert logger.raw_capture is regular          # 새 세션을 열지 않았다
    assert logger.exit_code == 2
    assert "애프터마켓 전환 실패" in "\n".join(messages)


def test_전환_중_도착한_콜백은_진단_기록에_남는다(transition_live, monkeypatch):
    logger, _ = transition_live
    logger._on_login(0)
    regular = logger.raw_capture
    original = regular.finish
    def finish_with_callback(*a, **k):
        logger._on_receive_real_data("005930", "주식체결", "")   # 전환 도중 도착
        return original(*a, **k)
    monkeypatch.setattr(regular, "finish", finish_with_callback)
    record = logger.run_aftermarket_transition()
    assert len(record.callbacks_during) == 1
    assert record.callbacks_during[0]["code"] == "005930"
    saved = json.loads(logger._transition_record_path
                       .read_text(encoding="utf-8"))
    assert saved["callbacks_during_transition"] == 1
    assert saved["callbacks_preserved"][0]["code"] == "005930"
    assert "무누락을 보장하지 않는다" in saved["note"]


def test_전환_중_콜백에_전환_단계와_FID_원문이_남는다(transition_live, monkeypatch):
    logger, _ = transition_live
    logger._on_login(0)
    regular = logger.raw_capture
    original = regular.finish
    def finish_with_callback(*a, **k):
        logger._on_receive_real_data("005930", "주식체결", "")
        return original(*a, **k)
    monkeypatch.setattr(regular, "finish", finish_with_callback)
    record = logger.run_aftermarket_transition()
    preserved = record.callbacks_during[0]
    assert preserved["transition_step"] == "finalize_regular"
    assert preserved["fids"]["15"] == "+2"                      # 등록된 FID 원문
    assert preserved["subscription_context"]["aftermarket_plan_codes"] == ["005930_NX", "000660_NX"]


# ── 7. 전환 기록의 식별자·리비전과 애프터마켓 종료 상한 ─────────────────────

def test_애프터마켓_상한_없이는_전환_계획을_받지_않는다(collector):
    for bad in (None, 0, -1, 301, True, float("inf")):
        with pytest.raises(ValueError, match="1~300초 제한 시간"):
            _logger(collector, aftermarket_plan=_plan(), aftermarket_duration_seconds=bad)


def test_전환_기록에_세션_식별자와_코드_리비전이_남는다(transition_live):
    logger, _ = transition_live
    logger._on_login(0)
    regular = logger.raw_capture
    record = logger.run_aftermarket_transition()
    assert record.prev_session_id == regular.identity.session_id
    assert record.next_session_id == logger.raw_capture.identity.session_id
    assert record.prev_session_id != record.next_session_id
    assert record.code_revision == "fixture"
    assert record.plan_revision["codes"] == ["005930_NX", "000660_NX"]
    saved = json.loads(logger._transition_record_path
                       .read_text(encoding="utf-8"))
    assert saved["prev_session_id"] == regular.identity.session_id
    assert saved["next_session_id"] == logger.raw_capture.identity.session_id
    assert len(saved["step_timing"]) == 4


def test_전환_뒤_감시_상태는_새_인스턴스다(transition_live):
    logger, _ = transition_live
    logger._on_login(0)
    logger._on_receive_real_data("005930", "주식체결", "")   # 정규장 수신 — 옛 감시기에 남는다
    old_monitor = logger.monitor
    logger.run_aftermarket_transition()
    assert logger.monitor is not old_monitor
    assert logger.monitor.last_event_ts is None            # 정규장 수신 이력을 이어받지 않는다


def test_애프터마켓_구간도_명시_시간_제한으로_종료를_요청한다(transition_live):
    import time
    from types import SimpleNamespace
    logger, _ = transition_live
    logger._on_login(0)
    logger.run_aftermarket_transition()
    logger.writer = SimpleNamespace(pending=0)
    logger.monitor = SimpleNamespace(
        tick=lambda: None, sample_queue_depth=lambda _: None, silence_stop_reason=lambda: None,
        finish=lambda reason: SimpleNamespace(extra=[], headline="test", lines=lambda: []))
    logger._aftermarket_subscribed_at = time.monotonic() - (logger.aftermarket_duration_seconds + 1)
    logger.is_running = True
    type(logger)._stats_worker(logger)               # 픽스처가 인스턴스 메서드를 대체해 둔 것을 우회
    assert logger._shutdown_requested == "애프터마켓 제한 시간 종료"


def test_전환_뒤_첫_수신_시각과_공백이_기록_파일에_반영된다(transition_live):
    logger, _ = transition_live
    logger._on_login(0)
    logger._on_receive_real_data("005930", "주식체결", "")   # 정규장 마지막 수신
    logger.run_aftermarket_transition()
    logger._on_receive_real_data("005930_NX", "주식체결", "")
    saved = json.loads(logger._transition_record_path
                       .read_text(encoding="utf-8"))
    assert saved["first_event_after"] is not None
    assert saved["reception_gap_sec"] is not None


def test_수신_공백은_콜백_진입의_단조_시계로_정확히_계산한다(transition_live, monkeypatch):
    logger, _ = transition_live
    now = [100.0]
    logger._monotonic = lambda: now[0]
    logger._on_login(0)
    call = logger.ocx.dynamicCall
    def slow_fid(method, *args):
        if method.startswith("GetCommRealData"):
            now[0] += 0.1  # 추출이 늦어져도 콜백 진입 시각을 사용해야 한다.
        return call(method, *args)
    monkeypatch.setattr(logger.ocx, "dynamicCall", slow_fid)
    logger._on_receive_real_data("005930", "주식체결", "")
    now[0] = 102.0
    record = logger.run_aftermarket_transition()
    assert record.reception_gap_sec is None
    now[0] = 107.25
    logger._on_receive_real_data("005930_NX", "주식체결", "")
    assert record.reception_gap_sec == 7.25
    assert record.reception_gap_sec >= 0
    saved = json.loads(logger._transition_record_path.read_text(encoding="utf-8"))
    assert saved["reception_gap_sec"] == 7.25 and saved["reception_clock"] == "monotonic"
    assert saved["last_event_before_utc"] and saved["first_event_after_kst"]


def test_정규장_만료가_애프터_타이머를_조기_종료하지_않는다(transition_live, monkeypatch):
    from types import SimpleNamespace
    import time
    logger, _ = transition_live
    logger._on_login(0)
    logger.run_aftermarket_transition()
    now = [1000.0]
    logger._monotonic = lambda: now[0]
    logger.duration_seconds, logger._subscribed_at = 1, 0
    logger._aftermarket_subscribed_at = now[0]
    logger.resources = None
    logger.log.status = lambda _: None
    original_monitor = logger.monitor
    logger.monitor = SimpleNamespace(tick=lambda: None, sample_queue_depth=lambda _: None,
                                     silence_stop_reason=lambda: None)
    sleeps = []
    def advance(_):
        assert logger._shutdown_requested is None
        sleeps.append(now[0])
        now[0] += 30
        assert len(sleeps) <= 2
    fake_time = SimpleNamespace(monotonic=lambda: now[0], sleep=advance)
    monkeypatch.setitem(type(logger)._stats_worker.__globals__, "time", fake_time)
    try:
        type(logger)._stats_worker(logger)
        assert sleeps == [1000.0, 1030.0]
        assert logger._shutdown_requested == "애프터마켓 제한 시간 종료"
    finally:
        logger.monitor = original_monitor
        monkeypatch.setitem(type(logger)._stats_worker.__globals__, "time", time)


def test_필수_진단_기록_실패_전에_새_세션을_열지_않는다(transition_live, monkeypatch):
    logger, _ = transition_live
    logger._on_login(0)
    regular = logger.raw_capture
    def fail(*args, **kwargs):
        raise OSError("진단 디스크 실패")
    monkeypatch.setattr(logger, "_write_transition_record", fail)
    record = logger.run_aftermarket_transition()
    assert not record.succeeded
    assert logger.raw_capture is regular
    assert not logger.accepting_events
    assert regular.queue.done.wait(1)


@pytest.mark.parametrize("failure_write", range(1, 11))
def test_단계별_기록_실패는_수신을_막고_열린_writer를_정리한다(
        transition_live, monkeypatch, failure_write):
    logger, _ = transition_live
    logger._on_login(0)
    regular = logger.raw_capture
    writes, registrations, closed_bytes = [], [], []
    original_write = logger._write_transition_record
    original_call = logger.ocx.dynamicCall
    def write():
        writes.append(logger.transition.record.phase)
        if regular.queue.snapshot()["writer_closed"] and not closed_bytes:
            closed_bytes.append(regular.path.read_bytes())  # 임시 디렉터리의 작은 합성 파일만
        if len(writes) >= failure_write:
            raise OSError("지속 기록 실패")
        original_write()
    def call(method, *args):
        if method.startswith("SetRealReg"):
            registrations.append(args)
            assert not logger.accepting_events
        return original_call(method, *args)
    monkeypatch.setattr(logger, "_write_transition_record", write)
    monkeypatch.setattr(logger.ocx, "dynamicCall", call)
    record = logger.run_aftermarket_transition()
    assert not record.succeeded and record.failed_step == "diagnostic_write"
    assert not logger.accepting_events and not logger._transition_active
    assert logger._shutdown_requested and logger.exit_code == 2
    assert bool(registrations) == (failure_write >= 9)
    assert (logger.raw_capture is not regular) == (failure_write >= 7)
    assert regular.queue.done.wait(1) and logger.raw_capture.queue.done.wait(1)
    assert regular.queue.snapshot()["writer_closed"]
    assert logger.raw_capture.queue.snapshot()["writer_closed"]
    if closed_bytes:
        assert regular.path.read_bytes() == closed_bytes[0]
    with pytest.raises(ValueError, match="한 번만"):
        logger.run_aftermarket_transition()


@pytest.mark.parametrize("stage", ["unsubscribe_regular", "finalize_regular", "open_aftermarket", "subscribe_nxt"])
@pytest.mark.parametrize("fault", ["cancel", "timeout", "exception"])
def test_단계별_중단은_후속_실행_없이_자원을_정리한다(transition_live, monkeypatch, stage, fault):
    logger, _ = transition_live
    logger._on_login(0)
    regular = logger.raw_capture
    now, injected, registrations = [100.0], [], []
    logger._monotonic = lambda: now[0]
    original_write = logger._write_transition_record
    original_call = logger.ocx.dynamicCall
    def write():
        record = logger.transition.record
        original_write()
        if (record.phase == "step_started" and record.step_timing[-1].step == stage
                and not injected):
            injected.append(stage)
            if fault == "cancel":
                logger._shutdown("합성 전환 취소")  # Qt 재진입에서도 실제 shutdown을 중첩하지 않는다.
            elif fault == "timeout":
                now[0] += 30
            else:
                logger.transition._steps[stage] = lambda: (_ for _ in ()).throw(RuntimeError("합성 단계 오류"))
    def call(method, *args):
        if method.startswith("SetRealReg"):
            registrations.append(args)
        return original_call(method, *args)
    monkeypatch.setattr(logger, "_write_transition_record", write)
    monkeypatch.setattr(logger.ocx, "dynamicCall", call)
    record = logger.run_aftermarket_transition()
    assert not record.succeeded and record.failed_step == stage
    assert not registrations and not logger.accepting_events
    assert regular.queue.done.wait(1) and logger.raw_capture.queue.done.wait(1)
    saved = json.loads(logger._transition_record_path.read_text(encoding="utf-8"))
    assert saved["phase"] == "failed" and saved["cleanup"]
    assert not logger._shutdown_done  # 종료 요청만 남기며 큐 정리는 이미 끝났다.


def test_마지막_구독_중_콜백도_완료_기록_전에는_진단에만_남는다(transition_live, monkeypatch):
    logger, _ = transition_live
    logger._on_login(0)
    call = logger.ocx.dynamicCall
    def receive(method, *args):
        if method.startswith("SetRealReg"):
            logger._on_receive_real_data("005930_NX", "주식체결", "")
        return call(method, *args)
    monkeypatch.setattr(logger.ocx, "dynamicCall", receive)
    record = logger.run_aftermarket_transition()
    assert record.succeeded and record.first_event_after is None
    assert record.callbacks_during[0]["transition_step"] == "subscribe_nxt"
    assert logger.raw_capture.queue.snapshot()["accepted_callbacks"] == 0
    assert logger._transition_record_path.parent.name == "session_transitions"
    assert logger.accepting_events


def test_첫_수신_공백_기록_실패는_입력을_막고_종료를_요청한다(transition_live, monkeypatch):
    logger, _ = transition_live
    logger._on_login(0)
    record = logger.run_aftermarket_transition()
    def fail():
        raise OSError("첫 수신 기록 실패")
    monkeypatch.setattr(logger.transition, "_persist_record", fail)
    logger._on_receive_real_data("005930_NX", "주식체결", "")
    assert not logger.accepting_events and logger._shutdown_requested
    assert record.failed_step == "diagnostic_write" and not record.succeeded


def test_새_writer_생성_뒤_계획_파일_실패도_구독_없이_정리한다(transition_live, monkeypatch):
    from pathlib import Path
    logger, _ = transition_live
    logger._on_login(0)
    regular = logger.raw_capture
    write = Path.write_text
    def fail_plan(path, *args, **kwargs):
        if path.name == "subscription_plan.json":
            raise OSError("계획 파일 저장 실패")
        return write(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", fail_plan)
    record = logger.run_aftermarket_transition()
    assert record.failed_step == "open_aftermarket"
    assert logger.raw_capture is not regular
    assert record.next_session_id == logger.raw_capture.identity.session_id
    assert regular.queue.snapshot()["state"] == "closed"
    assert logger.raw_capture.queue.done.wait(1)
    assert logger.raw_capture.queue.snapshot()["state"] == "interrupted"
    assert logger._aftermarket_subscribed_at is None


def test_실제_drain_대기_실패는_새_세션_없이_정리한다(transition_live, monkeypatch):
    logger, _ = transition_live
    logger._on_login(0)
    regular = logger.raw_capture
    monkeypatch.setattr(regular.queue.drained, "wait", lambda timeout: False)
    record = logger.run_aftermarket_transition()
    assert record.failed_step == "finalize_regular"
    assert logger.raw_capture is regular and record.next_session_id is None
    assert regular.queue.done.wait(1)
    assert regular.queue.snapshot()["state"] == "interrupted"
    assert regular.queue.snapshot()["writer_closed"]
    assert not logger.accepting_events
