"""장중 침묵과 네이티브 충돌의 Python 스택을 다음 실행부터 보존한다."""
from datetime import datetime, timezone
import faulthandler
import json
import os
from pathlib import Path


class CaptureDiagnostics:
    """OCX 호출 없이 최대 세 침묵 구간을 기록한다. 원인 해결/자동 복구 기능은 아니다."""

    def __init__(self, directory, *, handler=faulthandler):
        self.handler = handler
        self.file = None
        self.path = None
        self.directory = Path(directory)
        self.keys = set()
        self.stop_recorded = False
        self.owns_handler = False

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
            raise
        return self

    def record_stall(self, key, details):
        if self.file is None or key in self.keys or len(self.keys) >= 3:
            return False
        self.keys.add(key)
        self.file.write(json.dumps(dict(observed_at_utc=datetime.now(timezone.utc).isoformat(),
                                       kind="reception_stall", details=details), ensure_ascii=False) + "\n")
        self.file.flush()
        self.handler.dump_traceback(file=self.file, all_threads=True)
        return True

    def __exit__(self, *exc):
        if self.file is not None:
            if self.owns_handler:
                self.handler.disable()
            self.file.close()
            self.file = None

    def record_stop(self, details):
        """경고 구간 예산과 별도로 종료 요청 직전 스택을 한 번 남긴다."""
        if self.file is None or self.stop_recorded:
            return False
        self.stop_recorded = True
        self.file.write(json.dumps(dict(observed_at_utc=datetime.now(timezone.utc).isoformat(),
                                       kind="silence_stop", details=details), ensure_ascii=False) + "\n")
        self.file.flush()
        self.handler.dump_traceback(file=self.file, all_threads=True)
        return True
