"""Read-only Windows process binding; no discovery, launch, kill or OCX API.

Keep the handle for the channel lifetime so PID reuse cannot retarget a command.
The caller still owns session authorization and exclusive socket delivery.
"""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import ntpath
import os


@dataclass(frozen=True)
class ProcessFacts:
    pid: int
    started_at_utc: str
    executable: str
    python_bits: int

    def matches(self, identity):
        return (self.pid == identity.pid
                and self.started_at_utc == identity.started_at_utc
                and ntpath.normcase(ntpath.normpath(self.executable)) ==
                    ntpath.normcase(ntpath.normpath(identity.executable))
                and self.python_bits == identity.python_bits)


class WindowsProcess:
    """Pin one OS process with QUERY_LIMITED_INFORMATION and SYNCHRONIZE only."""

    def __init__(self, pid):
        if os.name != "nt":
            raise OSError("Windows process inspection required")
        if type(pid) is not int or not 0 < pid <= 0xffffffff:
            raise ValueError("valid Windows PID required")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "IsWow64Process2": ([wintypes.HANDLE, ctypes.POINTER(wintypes.WORD),
                                  ctypes.POINTER(wintypes.WORD)], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(kernel, name)  # Unsupported OS fails closed.
            function.argtypes, function.restype = args, result
        self._kernel = kernel
        self._handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.require_alive()
            stamps = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(self._handle, *(ctypes.byref(t) for t in stamps)):
                raise ctypes.WinError(ctypes.get_last_error())
            ticks = (stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime
            # Python 3.10 peers accept microsecond ISO timestamps. Pinning the
            # handle, rather than rounding a wall clock, prevents PID reuse.
            seconds, fraction = divmod(ticks, 10_000_000)
            date = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
            created = date.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction // 10:06d}Z"
            size = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(self._handle, 0, path, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            process_machine, native_machine = wintypes.WORD(), wintypes.WORD()
            if not kernel.IsWow64Process2(self._handle, ctypes.byref(process_machine), ctypes.byref(native_machine)):
                raise ctypes.WinError(ctypes.get_last_error())
            machine = process_machine.value or native_machine.value
            bits = {0x014c: 32, 0x01c4: 32, 0x8664: 64, 0xaa64: 64}.get(machine)
            if bits is None:
                raise OSError("unsupported process machine type")
            self.facts = ProcessFacts(pid, created, path.value, bits)
            self.require_alive()
        except BaseException:
            self.close()
            raise

    def require_alive(self):
        if not self._handle:
            raise ConnectionError("process handle closed")
        status = self._kernel.WaitForSingleObject(self._handle, 0)
        if status == 0:
            raise ProcessLookupError("bound process exited")
        if status != 258:  # WAIT_TIMEOUT: still running, without waiting.
            raise ctypes.WinError(ctypes.get_last_error())

    def verify(self, identity, *, require_alive=True):
        if not self._handle or not self.facts.matches(identity):
            raise ValueError("OS process identity mismatch or closed handle")
        if require_alive:
            self.require_alive()

    def close(self):
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
