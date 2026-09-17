"""tests/test_collector_diagnostics.py — 장애 진단 보강 두 가지

2026-09-17 오전 세션은 10:31 에 수신이 멎고 11:30:26 에 네이티브 메모리 접근
위반으로 죽었다. 그때 남은 것은 5초마다 덮어쓰는 상태 파일 한 장뿐이어서
(1) 죽기 전 메모리 추이를 볼 수 없었고, (2) 65분 침묵 동안 경고만 반복하며
수집 잠금을 쥔 채 살아 있었다. 여기서는 그 두 공백을 메운 장치를 확인한다.

무엇을 확인하지 '않는지' 도 분명히 해 둔다. 메모리 기록은 단서일 뿐이고,
침묵 종료 권고는 종료 '시도' 를 부르는 신호일 뿐 파일 닫힘을 보장하지 않는다.

실행:
    uv run pytest tests/test_collector_diagnostics.py -v
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.kiwoom.resource_log import MemorySample, ResourceHistory  # noqa: E402
from collector.kiwoom.session_monitor import SessionMonitor  # noqa: E402


class FakeClock:
    def __init__(self, start): self.now = float(start)
    def __call__(self): return self.now
    def advance(self, seconds): self.now += float(seconds); return self.now


def _session_day_clock(hour, minute=0, second=0):
    """오늘 날짜의 특정 시각을 가리키는 시계. 장중 판정이 날짜에 의존하기 때문이다."""
    day = datetime.now().date()
    return FakeClock(datetime.combine(day, dtime(hour, minute, second)).timestamp())


def _sample(commit, peak=None, pid=1234):
    return MemorySample(pid=pid, at_utc="2026-09-17T02:30:00+00:00",
                        working_set=commit, peak_working_set=peak or commit,
                        commit=commit, peak_commit=peak or commit)


# ── 메모리 추이 기록 ────────────────────────────────────────────────────────

def test_상태파일과_달리_이력은_덮어쓰지_않고_쌓인다(tmp_path=None):
    tmp = Path(tmp_path or "/tmp/_rl1"); tmp.mkdir(parents=True, exist_ok=True)
    target = tmp / "resource_history.jsonl"
    if target.exists(): target.unlink()
    clock = FakeClock(1000.0)
    values = iter([_sample(100), _sample(200), _sample(300)])
    history = ResourceHistory(target, clock=clock, sampler=lambda: next(values), interval_sec=60)
    for _ in range(3):
        assert history.sample(force=True) is not None
        clock.advance(60)
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert [r["commit"] for r in rows] == [100, 200, 300]   # 추이가 남는다
    assert rows[0]["pid"] == 1234 and rows[0]["at_utc"]      # PID·시각도 함께


def test_간격이_차기_전에는_표본하지_않는다(tmp_path=None):
    tmp = Path(tmp_path or "/tmp/_rl2"); tmp.mkdir(parents=True, exist_ok=True)
    target = tmp / "h.jsonl"
    if target.exists(): target.unlink()
    clock = FakeClock(0.0)
    history = ResourceHistory(target, clock=clock, sampler=lambda: _sample(1), interval_sec=60)
    assert history.sample() is not None          # 첫 호출은 기록
    clock.advance(30)
    assert history.sample() is None              # 간격 미달
    clock.advance(31)
    assert history.sample() is not None          # 간격 충족


def test_최댓값은_줄어들지_않는다(tmp_path=None):
    tmp = Path(tmp_path or "/tmp/_rl3"); tmp.mkdir(parents=True, exist_ok=True)
    target = tmp / "h.jsonl"
    if target.exists(): target.unlink()
    clock = FakeClock(0.0)
    values = iter([_sample(500, peak=500), _sample(100, peak=100)])
    history = ResourceHistory(target, clock=clock, sampler=lambda: next(values))
    history.sample(force=True); history.sample(force=True)
    assert history.snapshot()["peak_commit"] == 500   # 현재값이 떨어져도 최댓값은 보존


def test_표본_실패가_수집을_멈추지_않는다(tmp_path=None):
    tmp = Path(tmp_path or "/tmp/_rl4"); tmp.mkdir(parents=True, exist_ok=True)
    def boom(): raise OSError("조회 실패")
    history = ResourceHistory(tmp / "h.jsonl", clock=FakeClock(0.0), sampler=boom)
    assert history.sample(force=True) is None        # 예외가 새어나오지 않는다
    assert history.snapshot() is None
    none_history = ResourceHistory(tmp / "h2.jsonl", clock=FakeClock(0.0), sampler=lambda: None)
    assert none_history.sample(force=True) is None   # 지원하지 않는 OS 도 마찬가지


def test_이력은_무한정_자라지_않는다(tmp_path=None):
    tmp = Path(tmp_path or "/tmp/_rl5"); tmp.mkdir(parents=True, exist_ok=True)
    target = tmp / "h.jsonl"
    if target.exists(): target.unlink()
    history = ResourceHistory(target, clock=FakeClock(0.0), sampler=lambda: _sample(1), max_lines=10)
    for _ in range(25):
        history.sample(force=True)
    assert len(target.read_text(encoding="utf-8").splitlines()) == 10


# ── 장시간 침묵 시 안전한 종료 권고 ─────────────────────────────────────────

def _monitor(clock, **kw):
    monitor = SessionMonitor(clock=clock, gap_threshold_sec=120, silence_stop_sec=600, **kw)
    monitor.start()
    return monitor


def test_구독_완료_전에는_침묵_종료를_판정하지_않는다():
    # 로그인·등록 전 침묵은 결손이 아니라 정상 대기다.
    clock = _session_day_clock(10, 0)
    monitor = _monitor(clock)
    clock.advance(3600)
    assert monitor.silence_stop_reason() is None      # 구독 완료 표시가 없으면 판정 안 함
    monitor.mark_reception_expected()
    clock.advance(601)
    assert monitor.silence_stop_reason() is not None  # 표시 후에는 판정한다


def test_장중이_아니면_판정하지_않는다():
    clock = _session_day_clock(16, 0)                 # 마감 후
    monitor = _monitor(clock)
    monitor.mark_reception_expected()
    clock.advance(3600)
    assert monitor.silence_stop_reason() is None


def test_한도_미만이면_권고하지_않는다():
    clock = _session_day_clock(10, 0)
    monitor = _monitor(clock)
    monitor.mark_reception_expected()
    monitor.on_trade()
    clock.advance(599)
    assert monitor.silence_stop_reason() is None
    clock.advance(2)
    reason = monitor.silence_stop_reason()
    assert reason is not None and "침묵" in reason and "종료를 시도" in reason


def test_한_세션에서_한_번만_권고한다():
    clock = _session_day_clock(10, 0)
    monitor = _monitor(clock)
    monitor.mark_reception_expected()
    monitor.on_trade()
    clock.advance(601)
    assert monitor.silence_stop_reason() is not None
    clock.advance(601)
    assert monitor.silence_stop_reason() is None      # 반복해서 부르지 않는다


def test_수신이_회복되면_권고가_풀린다():
    clock = _session_day_clock(10, 0)
    monitor = _monitor(clock)
    monitor.mark_reception_expected()
    monitor.on_trade()
    clock.advance(601)
    assert monitor.silence_stop_reason() is not None
    monitor.on_trade()                                 # 수신 재개
    monitor.tick()                                     # 침묵 구간이 닫히며 권고가 풀린다
    clock.advance(601)
    assert monitor.silence_stop_reason() is not None   # 다시 침묵하면 다시 권고


def test_한도를_끄면_권고하지_않는다():
    clock = _session_day_clock(10, 0)
    monitor = SessionMonitor(clock=clock, silence_stop_sec=None)
    monitor.start(); monitor.mark_reception_expected(); monitor.on_trade()
    clock.advance(36000)
    assert monitor.silence_stop_reason() is None


def test_기존_침묵_경고는_그대로_동작한다():
    # 종료 권고를 얹었다고 기존 경고가 사라지면 안 된다.
    clock = _session_day_clock(10, 0)
    monitor = _monitor(clock)
    monitor.mark_reception_expected(); monitor.on_trade()
    clock.advance(121)
    warning = monitor.tick()
    assert warning is not None and "결손 의심" in warning
