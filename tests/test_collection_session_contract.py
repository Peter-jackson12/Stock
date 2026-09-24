"""판정 창 밖의 합성 호가도 기본 콜백/저장 경로에서 보존됨을 검사한다.

GitHub-hosted 회귀: 기존 fake Qt/OCX와 tmp_path의 작은 raw만 쓴다.
해당 시각의 실피드 수신, 전체 coverage, venue 또는 연구 적격성 인증이 아니다.
"""
from datetime import datetime, timedelta, timezone

import pytest

import collector.kiwoom.live_capture as capture_module
from collector.kiwoom.market_sessions import PROFILE_KRX_REGULAR, QUOTE, active_session
from collector.raw_v2 import read_raw_v2
from tests.test_tick_collector_shutdown import collector  # noqa: F401 - 가짜 Qt/OCX fixture
from tests.test_live_collector import live  # noqa: F401 - 합성 raw-v2 fixture


@pytest.mark.parametrize("hour,minute,second", [
    (8, 30, 0), (8, 50, 0), (8, 59, 59), (15, 31, 0),
])
def test_default_stores_quote_outside_silence_window(live, monkeypatch, hour, minute, second):
    logger, values, messages = live
    moment = datetime(2026, 9, 23, hour, minute, second)
    kst = timezone(timedelta(hours=9))

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.replace(tzinfo=kst).astimezone(tz)

    # OS 시계는 바꾸지 않고 대상 모듈의 시계만 주입한다.
    monkeypatch.setitem(logger._on_login.__globals__, "datetime", FixedDateTime)
    monkeypatch.setattr(capture_module, "datetime", FixedDateTime)
    monkeypatch.setattr(logger.monitor, "_clock", lambda: moment.timestamp())
    logger.monitor.start()
    values[21] = moment.strftime("%H%M%S")
    logger._on_login(0)
    assert logger.storage == "raw-v2" and logger.plan is None
    assert logger.monitor._sessions is None  # 실제 기본은 legacy any-event 경로다.
    assert active_session(PROFILE_KRX_REGULAR, moment, QUOTE) is None
    assert logger.monitor.tick() is None
    assert logger.monitor.silence_stop_reason() is None

    logger._on_receive_real_data("005930", "주식호가잔량", "")
    assert logger.monitor.quote_count == 1
    logger._shutdown("합성 판정 구간 외 호가 종료")
    assert logger.exit_code == 0, "\n".join(messages)
    snapshot = logger.raw_capture.queue.snapshot()
    assert snapshot["accepted_callbacks"] == snapshot["committed_callbacks"] == 1
    assert snapshot["writer_closed"] and snapshot["state"] == "closed"
    with read_raw_v2(logger.db_path) as (_, rows):
        records = list(rows)
    assert len(records) == 2  # session_start + 호가, 품질 오류 없는 합성 입력
    assert records[1]["raw_fields"]["fids"]["21"] == moment.strftime("%H%M%S")
    assert records[1]["event"].venue == "unknown"
