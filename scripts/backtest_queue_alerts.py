"""
scripts/backtest_queue_alerts.py — 세션 로그로 침묵/적체 경보 재보정 (rev.2 §6)

collector/kiwoom/session_monitor.py 의 임계값(침묵 120초 / 대기큐 바닥 20,000건 /
적체 판정창 300초)이 실측에서 얼마나 울렸을지 사후 재생으로 확인한다.

합성 드라이런(tests/test_kiwoom_session_monitor.py 의 엔드투엔드 테스트)은
"조작하면 울리는가"(민감도)를 본다. 이 스크립트는 반대로 "실제로 바쁜 정상적인
하루에 오작동하는가"(특이도)를 본다 — 로그의 상태줄(1분 간격 샘플)에 남은
체결/호가 누적 건수와 대기큐 잔량을 시간 순서대로 SessionMonitor 에 그대로
먹여서, 그날 실제로 몇 번 경보가 발동했을지 재현한다.

실행:
    uv run python scripts/backtest_queue_alerts.py logs/kiwoom_universe_20260915.log
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collector.kiwoom.session_monitor import SessionMonitor  # noqa: E402

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*"
    r"체결: (?P<trades>[\d,]+)건 \| 호가: (?P<quotes>[\d,]+)건 \(대기큐: (?P<depth>[\d,]+)\)"
)


class _ReplayClock:
    """실측 로그 타임스탬프로 시계를 흉내낸다."""

    def __init__(self, t0: float) -> None:
        self.t = t0

    def __call__(self) -> float:
        return self.t


def parse_status_lines(log_path: Path) -> list[tuple[datetime, int, int, int]]:
    """상태줄에서 (시각, 누적 체결, 누적 호가, 대기큐 잔량)을 뽑는다."""
    samples = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        m = LINE_RE.search(line)
        if not m:
            continue
        samples.append((
            datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S"),
            int(m["trades"].replace(",", "")),
            int(m["quotes"].replace(",", "")),
            int(m["depth"].replace(",", "")),
        ))
    return samples


def backtest(log_path: Path) -> int:
    samples = parse_status_lines(log_path)
    if not samples:
        print(f"❌ {log_path}: 상태줄 패턴을 찾지 못했습니다.")
        return 1

    clock = _ReplayClock(samples[0][0].timestamp())
    monitor = SessionMonitor(clock=clock)   # 실 운영 기본 임계값 그대로
    monitor.start()

    alerts: list[tuple[str, datetime, str]] = []
    prev_trades = prev_quotes = 0
    for ts, trades, quotes, depth in samples:
        clock.t = ts.timestamp()
        if trades > prev_trades:
            monitor.on_trade()
        if quotes > prev_quotes:
            monitor.on_quote()
        prev_trades, prev_quotes = trades, quotes

        for kind, notice in (("silence", monitor.tick()), ("queue", monitor.sample_queue_depth(depth))):
            if notice:
                alerts.append((kind, ts, notice))

    clock.t = samples[-1][0].timestamp()
    report = monitor.finish("백테스트 종료(로그 마지막 샘플)")

    print(f"📄 {log_path.name} — 샘플 {len(samples)}개 "
          f"({samples[0][0].strftime('%H:%M:%S')} ~ {samples[-1][0].strftime('%H:%M:%S')})")
    print(f"장중 대기큐 최대: {report.queue_max_depth:,}건 "
          f"({datetime.fromtimestamp(report.queue_max_ts).strftime('%H:%M:%S') if report.queue_max_ts else '없음'})")
    print(f"침묵 구간: {len(report.gaps)}건")
    print(f"재생 중 경보 발동: {len(alerts)}건")
    for kind, ts, notice in alerts:
        print(f"  [{kind}] {ts.strftime('%H:%M:%S')} {notice}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_path", type=Path, help="collector/kiwoom 세션 로그 파일")
    args = parser.parse_args()
    return backtest(args.log_path)


if __name__ == "__main__":
    raise SystemExit(main())
