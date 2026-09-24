"""Operator용 읽기 전용 환경 목록 점검.

GUI 기동, 수집 준비, OCX 준비 여부를 판정하거나 환경을 수정하지 않는다.
"""
from __future__ import annotations

from importlib import metadata
import os
from pathlib import Path
import struct
import sys
from typing import Callable

UI_PACKAGES = ("streamlit", "pandas", "plotly", "pyarrow", "numpy", "python-dotenv")
REQUIRED_FILES = (
    "pyproject.toml", "dashboard/app.py", "control_tower/status.py",
    "HANDOFF.md", "START_HERE.md",
)


def _runtime() -> dict:
    return {
        "platform": sys.platform, "version": tuple(sys.version_info[:3]),
        "bits": struct.calcsize("P") * 8, "prefix": sys.prefix,
        "executable": sys.executable,
    }


def _same_path(left: str | Path, right: str | Path) -> bool:
    # resolve()로 .venv의 symlink를 base Python으로 합쳐 판정하지 않는다.
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def inspect_operator_environment(
    root: Path,
    *,
    base: dict | None = None,
    runtime_provider: Callable[[], dict] | None = None,
    package_version: Callable[[str], str] | None = None,
) -> dict:
    """명시된 파일과 설치 metadata만 읽어 Operator 환경 목록을 만든다."""
    root = Path(root)
    result = {} if base is None else dict(base)
    result["scope"] = "environment_inventory_only"
    runtime = (runtime_provider or _runtime)()
    version_reader = package_version or metadata.version
    checks = []

    def check(name: str, okay: bool, detail: str, failure: str = "FAIL") -> None:
        checks.append({"name": name, "status": "PASS" if okay else failure, "detail": detail})

    check("windows", runtime["platform"] == "win32", runtime["platform"])
    check("python_64bit", runtime["bits"] == 64, str(runtime["bits"]))
    check(
        "python_version",
        runtime["version"] >= (3, 14),
        "현재 " + ".".join(map(str, runtime["version"])) + "; 프로젝트 요구 >=3.14",
    )
    check(
        "project_venv",
        _same_path(runtime["prefix"], root / ".venv"),
        "실행 환경: " + runtime["prefix"] + "; 기대 환경: " + str(root / ".venv"),
    )
    for relative in REQUIRED_FILES:
        check(relative, (root / relative).is_file(), "파일 존재만 확인; 실행 검증 아님")
    for package in UI_PACKAGES:
        try:
            version = version_reader(package)
        except metadata.PackageNotFoundError:
            check("package:" + package, False, "설치 metadata 없음; 자동 설치하지 않음")
        else:
            check("package:" + package, True, version + "; import/버전 호환성/lock 일치 미검증")

    collector_exists = (root / ".venv32/Scripts/python.exe").is_file()
    checks.append({
        "name": "collector_runtime_file", "status": "INFO" if collector_exists else "WARN",
        "detail": "파일 있음; 32-bit/OCX 미검증" if collector_exists else "파일 없음; GUI 점검의 필수 조건 아님",
    })
    result.update(
        checks=checks,
        inventory_passed=not any(item["status"] == "FAIL" for item in checks),
        runtime_imports_verified=False,
        ocx_verified=False,
        operator_ready="unverified",
        executable=runtime["executable"],
    )
    return result
