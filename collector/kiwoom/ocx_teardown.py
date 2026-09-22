"""Qt 소유 스레드에서 한 번만 시도하는 명시적 ActiveX 해제.

네이티브 장애를 잡거나 수신을 복구하지 않는다. 실제 컨트롤 대신 대역으로
검증할 수 있지만, 대역 성공은 키움 OCX 실측을 대신하지 않는다.
"""
from __future__ import annotations

import threading


def record_phase(diagnostics, phase, **details):
    """진단 실패가 저장·해제를 건너뛰게 하지 않는다. 콜백 핫패스용이 아니다."""
    try:
        recorder = getattr(diagnostics, "record_phase", None)
        if recorder is not None:
            return bool(recorder(phase, details))
    except Exception:
        pass
    return False


class OcxTeardown:
    def __init__(self, *, owner_thread, enabled=False):
        if type(enabled) is not bool:
            raise ValueError("explicit OCX teardown flag must be bool")
        self.owner_thread = owner_thread
        self.enabled = enabled
        self.state = "not_attempted"
        self.error = None

    def release(self, control, *, writer_stopped, diagnostics=None):
        """clear 호출 반환과 isNull 확인만 기록한다. DLL unload 인증이 아니다.

        writer_stopped는 호출자가 증명한 워커 종료다. 타임아웃이나 closed 문자열만
        받아서 True로 바꾸지 않는다. clear 실패 후 자동 재시도하지 않는다.
        """
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError("OCX teardown requires its owning Qt thread")
        if type(writer_stopped) is not bool:
            raise ValueError("explicit writer-stopped observation required")
        if self.state in {"cleared", "absent", "disabled"}:
            return True
        if self.state in {"clearing", "failed"}:
            return False
        if not self.enabled:
            self.state = "disabled"
            record_phase(diagnostics, "ocx_clear_disabled")
            return True
        if not writer_stopped:
            # 후속 main finally의 기존 drain 대기 이후 다시 판단할 수 있다.
            if self.state != "deferred":
                record_phase(diagnostics, "ocx_clear_deferred", writer_stopped=False)
            self.state = "deferred"
            return False
        if control is None:
            self.state = "absent"
            record_phase(diagnostics, "ocx_clear_absent")
            return True
        self.state = "clearing"  # 네이티브 재진입 전에 고정한다.
        record_phase(diagnostics, "ocx_clear_enter", writer_stopped=True)
        try:
            already_null = bool(control.isNull())
            if not already_null:
                control.clear()
            null_after = bool(control.isNull())
            if not null_after:
                raise RuntimeError("ActiveX control remains non-null after clear")
        except Exception as exc:
            self.state = "failed"
            self.error = f"{type(exc).__name__}: {exc}"[:512]
            record_phase(diagnostics, "ocx_clear_failed", error=self.error)
            return False
        self.state = "cleared"
        record_phase(diagnostics, "ocx_clear_returned", already_null=already_null,
                     is_null_after=True)
        return True
