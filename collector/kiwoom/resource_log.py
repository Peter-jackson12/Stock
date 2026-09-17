"""수집기 프로세스의 메모리 사용 추이를 남긴다 — 장애 직전 상태를 사후에 보기 위해.

2026-09-17 오전 세션은 11:30:26 에 네이티브 메모리 접근 위반(0xC0000005)으로
죽었고, 남은 기록은 5초마다 덮어쓰는 상태 파일 한 장뿐이었다. 그래서 "죽기 전에
메모리가 차오르고 있었는가" 를 확인할 방법이 없었다. 이 모듈은 그 공백만 메운다.

한계를 분명히 해 둔다. 이 기록은 **단서일 뿐 판정이 아니다.** 사용량이 낮게
찍혔다고 해서 메모리 훼손이나 주소 공간 문제가 배제되는 것은 아니다. 실제로
그날 덤프는 보안 모듈의 널 함수 호출을 가리켰고, 그런 고장은 사용량 그래프에
흔적을 남기지 않을 수도 있다. 사용량만으로 원인 후보를 기각하거나 원인을
규명할 수는 없다.

프로젝트 의존성에 psutil 이 없으므로 Windows 에서는 표준 라이브러리의 ctypes 로
직접 읽는다. 다른 OS 나 조회 실패 시에는 None 을 돌려주고, 기록은 건너뛴다.
수집을 방해하지 않는 것이 우선이라 모든 실패를 삼킨다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path

#: 기본 표본 간격. 1초 주기 루프에서 불리지만 이 간격으로만 실제 기록한다.
DEFAULT_SAMPLE_INTERVAL_SEC = 60.0
#: 이력 파일이 무한정 자라지 않도록 하는 줄 수 상한. 60초 간격이면 하루 약 390줄이다.
DEFAULT_MAX_LINES = 5_000


@dataclass(frozen=True)
class MemorySample:
    pid: int
    at_utc: str
    working_set: int          # 현재 물리 메모리 점유
    peak_working_set: int     # 프로세스 생애 최대 물리 메모리 점유
    commit: int               # 현재 프로세스 커밋량; 예약/매핑을 포함한 주소 공간 점유와 다르다
    peak_commit: int          # 생애 최대 커밋


def read_windows_memory(pid=None):
    """Windows 프로세스 메모리를 표준 라이브러리로 읽는다. 실패하면 None."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    class _Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    try:
        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb)
        if not ok:
            return None
    except Exception:
        return None

    return MemorySample(
        pid=os.getpid() if pid is None else pid,
        at_utc=datetime.now(timezone.utc).isoformat(),
        working_set=int(counters.WorkingSetSize),
        peak_working_set=int(counters.PeakWorkingSetSize),
        commit=int(counters.PagefileUsage),
        peak_commit=int(counters.PeakPagefileUsage),
    )


class ResourceHistory:
    """주기적으로 메모리를 표본하고 한 줄씩 덧붙인다. 상태 파일과 달리 덮어쓰지 않는다."""

    def __init__(self, path, *, clock, sampler=read_windows_memory,
                 interval_sec=DEFAULT_SAMPLE_INTERVAL_SEC, max_lines=DEFAULT_MAX_LINES):
        self.path = Path(path)
        self._clock = clock
        self._sampler = sampler
        self.interval_sec = float(interval_sec)
        self.max_lines = int(max_lines)
        self._last_sample_at: float | None = None
        self.latest: MemorySample | None = None
        self.peak_commit_seen: int = 0

    def sample(self, *, force=False):
        """간격이 찼으면 한 번 표본한다. 기록했으면 표본을, 아니면 None."""
        now = self._clock()
        if not force and self._last_sample_at is not None:
            if now - self._last_sample_at < self.interval_sec:
                return None
        self._last_sample_at = now
        try:
            sample = self._sampler()
        except Exception:
            return None
        if sample is None:
            return None
        self.latest = sample
        self.peak_commit_seen = max(self.peak_commit_seen, sample.peak_commit)
        self._append(sample)
        return sample

    def _append(self, sample):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(asdict(sample), ensure_ascii=False) + "\n")
            self._trim()
        except Exception:
            return  # 기록 실패가 수집을 멈추게 하지 않는다.

    def _trim(self):
        try:
            if not self.path.exists():
                return
            with self.path.open("r", encoding="utf-8") as stream:
                lines = stream.readlines()
            if len(lines) <= self.max_lines:
                return
            keep = lines[-self.max_lines:]
            temporary = self.path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                stream.writelines(keep)
            temporary.replace(self.path)
        except Exception:
            return

    def snapshot(self):
        """상태 파일에 함께 실을 요약. 표본이 없으면 None."""
        if self.latest is None:
            return None
        return dict(pid=self.latest.pid, at_utc=self.latest.at_utc,
                    working_set=self.latest.working_set,
                    peak_working_set=self.latest.peak_working_set,
                    commit=self.latest.commit,
                    peak_commit=max(self.latest.peak_commit, self.peak_commit_seen),
                    history_path=str(self.path),
                    note="메모리 추이는 단서일 뿐이며 낮은 사용량이 메모리 훼손을 배제하지 않는다")
