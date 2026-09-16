"""Read-only Windows/32-bit OCX registration check; never instantiate or log in."""
import os
from pathlib import Path
import struct
import sys


def inspect_environment():
    result = dict(python_bits=struct.calcsize("P") * 8, executable=sys.executable,
                  default_storage="raw-v2", login_attempted=False, ocx_instantiated=False,
                  ready=False, ocx_registered=False, ocx_file_exists=False)
    if os.name != "nt" or result["python_bits"] != 32:
        result["error"] = "Windows 32-bit Python required"
        return result
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"KHOPENAPI.KHOpenAPICtrl.1\CLSID") as key:
            clsid = winreg.QueryValueEx(key, "")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\InprocServer32") as key:
            path = winreg.QueryValueEx(key, "")[0]
        result.update(ocx_registered=True, ocx_path=path,
                      ocx_file_exists=Path(os.path.expandvars(path.strip('"'))).is_file())
        result["ready"] = result["ocx_file_exists"]
    except OSError as exc:
        result["error"] = str(exc)
    return result
