"""침묵 스택과 종료 단계 증거. 네이티브 예외의 포괄적 포착기는 아니다.

CPython 3.10의 Windows faulthandler는 MSC 예외 0xE06D7363을 제외한다.
파일이 열려 있어도 모든 C++ 예외나 최종 OS 종료 코드가 남는 것은 아니다.
"""
from datetime import datetime, timezone
import faulthandler
import json
import os
from pathlib import Path
import threading


class CaptureDiagnostics:
    """최대 세 침묵 구간과 종료 단계 기록. 원인 해결/자동 복구 기능은 아니다."""

    MAX_PHASES = 64
    MAX_PHASE_BYTES = 2048

    def __init__(self, directory, *, handler=faulthandler):
        self.handler = handler
        self.file = None
        self.path = None
        self.directory = Path(directory)
        self.keys = set()
        self.stop_recorded = False
        self.owns_handler = False
        self.phase_count = 0
        self.phase_error = None
        self._lock = threading.RLock()

    def __enter__(self):
        # 기존 핸들러를 덮어썼다가 잘못 해제하지 않는다.
        self.owns_handler = not self.handler.is_enabled()
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.path = self.directory / f"collector_fault_{stamp}_{os.getpid()}.log"
        self.file = self.path.open("x", encoding="utf-8")
        try:
            if self.owns_handler:
                self.handler.enable(file=self.file, all_threads=True)
        except BaseException:
            self.file.close()
            self.file = None
            raise
        return self

    def record_phase(self, phase, details=None):
        """저빈도 종료 단계만 flush한다. 호출 복귀는 OS 종료 성공과 다르다.

        새 단계가 한도를 넘거나 쓰기 실패 시 진단만 중단한다. 이전 증거를
        자르거나 원본 수집 상태를 변경하지 않는다. fsync/전원 장애 보장은 없다.
        """
        with self._lock:
            if self.file is None or self.phase_error or self.phase_count >= self.MAX_PHASES:
                return False
            try:
                if not isinstance(phase, str) or not 1 <= len(phase) <= 80:
                    raise ValueError("bounded phase name required")
                text = json.dumps(dict(observed_at_utc=datetime.now(timezone.utc).isoformat(),
                    kind="collector_phase", phase=phase, details=details or {}),
                    ensure_ascii=False, allow_nan=False) + "\n"
                if len(text.encode("utf-8")) > self.MAX_PHASE_BYTES:
                    raise ValueError("phase record too large")
                self.phase_count += 1
                self.file.write(text)
                self.file.flush()
            except Exception as exc:
                self.phase_error = type(exc).__name__
                return False
            return True

    def record_stall(self, key, details):
        with self._lock:
            if self.file is None or key in self.keys or len(self.keys) >= 3:
                return False
            self.keys.add(key)
            self.file.write(json.dumps(dict(observed_at_utc=datetime.now(timezone.utc).isoformat(),
                                           kind="reception_stall", details=details), ensure_ascii=False) + "\n")
            self.file.flush()
            self.handler.dump_traceback(file=self.file, all_threads=True)
            return True

    def __exit__(self, *exc):
        with self._lock:
            if self.file is not None:
                # 이 마커도 best-effort다. 끝까지 기록되지 않았으면 미확인으로 읽는다.
                self.record_phase("diagnostics_closing", {"owns_handler": self.owns_handler})
                if self.owns_handler:
                    self.handler.disable()
                self.file.close()
                self.file = None

    def record_stop(self, details):
        """경고 구간 예산과 별도로 종료 요청 직전 스택을 한 번 남긴다."""
        with self._lock:
            if self.file is None or self.stop_recorded:
                return False
            self.stop_recorded = True
            self.file.write(json.dumps(dict(observed_at_utc=datetime.now(timezone.utc).isoformat(),
                                           kind="silence_stop", details=details), ensure_ascii=False) + "\n")
            self.file.flush()
            self.handler.dump_traceback(file=self.file, all_threads=True)
            return True
