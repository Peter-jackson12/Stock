"""Read-only collector preflight for the Operator UI.

This module observes local prerequisites only.  It never creates an OCX object,
logs in, subscribes, starts/stops a collector, mutates a lease, opens raw data,
or turns its report into execution approval.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import subprocess
from typing import Callable

from collector.kiwoom.fid_read_ab_admission import (
    inspect_disk,
    inspect_existing_lease,
    inspect_processes,
)

PASS = "PASS"
WARN = "WARN"
BLOCKED = "BLOCKED"
UNVERIFIED = "UNVERIFIED"

PREFLIGHT_RELATIVE = Path("collector/kiwoom/preflight.py")
RUNTIME_RELATIVE = Path(".venv32/Scripts/python.exe")
NON_ACTIONS = (
    "no install/repair/uv sync",
    "no OCX instantiation/CommConnect/login/SetRealReg",
    "no collector start/stop/kill/restart",
    "no lock creation/deletion",
    "no market query",
    "no raw database access",
)

# Load only the existing pure preflight file in the 32-bit interpreter.  In
# particular, do not import kiwoom_universe_logger.py, which imports Qt/ActiveX.
_ISOLATED_PREFLIGHT = """\
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("stock_collector_preflight", sys.argv[1])
if spec is None or spec.loader is None:
    raise RuntimeError("collector preflight module could not be loaded")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps(module.inspect_environment(), ensure_ascii=False))
"""


def _axis(check_id: str, label: str, status: str, detail: str, next_check: str) -> dict:
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "detail": detail,
        "next_check": next_check,
    }


def _count(value) -> int | None:
    return len(value) if isinstance(value, list) else None


def _official_axis(runtime: dict, official: dict) -> dict:
    if runtime.get("exists") is not True:
        return _axis(
            "official_preflight", "32-bit/OCX 공식 preflight", BLOCKED,
            ".venv32 수집기 런타임이 없어 공식 preflight를 실행하지 않았습니다.",
            "승인된 환경 준비 절차로 .venv32를 확인한 뒤 다시 점검하세요.",
        )
    if official.get("probe_ok") is not True:
        reason = official.get("error") or "공식 preflight 결과를 읽지 못했습니다."
        return _axis(
            "official_preflight", "32-bit/OCX 공식 preflight", UNVERIFIED,
            str(reason)[:512],
            "사용자 Windows의 .venv32에서 로그인 없는 공식 preflight를 다시 확인하세요.",
        )
    result = official.get("result")
    if not isinstance(result, dict):
        return _axis(
            "official_preflight", "32-bit/OCX 공식 preflight", UNVERIFIED,
            "공식 preflight 출력 형식을 확인할 수 없습니다.",
            "공식 preflight JSON을 다시 확인하세요.",
        )
    safe_contract = (
        result.get("python_bits") == 32
        and result.get("login_attempted") is False
        and result.get("ocx_instantiated") is False
        and result.get("ready") is True
        and result.get("ocx_registered") is True
        and result.get("ocx_file_exists") is True
        and result.get("error") in (None, "")
    )
    if safe_contract:
        return _axis(
            "official_preflight", "32-bit/OCX 공식 preflight", PASS,
            "32-bit Python과 등록된 OCX 파일을 확인했습니다. OCX 객체나 로그인은 만들지 않았습니다.",
            "실행 직전의 공식 admission에서 같은 사실을 새로 확인하세요.",
        )
    fields = (
        f"bits={result.get('python_bits')!r}, registered={result.get('ocx_registered')!r}, "
        f"file={result.get('ocx_file_exists')!r}, ready={result.get('ready')!r}"
    )
    if result.get("login_attempted") is not False or result.get("ocx_instantiated") is not False:
        fields += "; 읽기 전용 계약 위반 결과"
    return _axis(
        "official_preflight", "32-bit/OCX 공식 preflight", BLOCKED, fields,
        "오류를 자동 수정하지 말고 공식 preflight의 표시 원인을 확인하세요.",
    )


def reduce_collector_preflight(
    *,
    runtime: dict,
    official_preflight: dict,
    disk: dict,
    processes: dict,
    lease: dict,
) -> dict:
    """Reduce already-observed facts without I/O or execution authorization."""
    axes = []

    runtime_exists = runtime.get("exists") is True
    axes.append(_axis(
        "collector_runtime", "collector 런타임",
        PASS if runtime_exists else BLOCKED,
        ".venv32 Python 파일이 있습니다." if runtime_exists else ".venv32 Python 파일이 없습니다.",
        "파일 존재는 32-bit/OCX 준비를 뜻하지 않으므로 다음 항목도 확인하세요."
        if runtime_exists else "승인된 별도 환경 준비 절차를 확인하세요. 자동 설치하지 않습니다.",
    ))
    axes.append(_official_axis(runtime, official_preflight))

    free = disk.get("free_bytes") if isinstance(disk, dict) else None
    minimum = disk.get("minimum_code_admission_bytes") if isinstance(disk, dict) else None
    meets = disk.get("meets_code_minimum") if isinstance(disk, dict) else None
    if type(free) is int and type(minimum) is int and meets is True and free >= minimum:
        disk_status = PASS
        disk_detail = f"코드 admission 하한 {minimum:,} bytes 이상입니다 (현재 {free:,} bytes)."
    elif type(free) is int and type(minimum) is int and (meets is False or free < minimum):
        disk_status = BLOCKED
        disk_detail = f"코드 admission 하한 {minimum:,} bytes 미만입니다 (현재 {free:,} bytes)."
    else:
        disk_status = UNVERIFIED
        disk_detail = "저장 볼륨 여유와 코드 admission 하한을 대조하지 못했습니다."
    axes.append(_axis(
        "storage_floor", "저장 볼륨 admission 하한", disk_status, disk_detail,
        "PASS여도 예상 수집량 전체를 보장하지 않으므로 실행별 용량 계획을 확인하세요.",
    ))

    process_ok = isinstance(processes, dict) and processes.get("probe_ok") is True
    collector_count = _count(processes.get("collector_processes")) if process_ok else None
    same_runtime_count = _count(processes.get("same_python_runtime_other_processes")) if process_ok else None
    unidentified_count = _count(processes.get("unidentified_python_processes")) if process_ok else None
    if not process_ok or None in (collector_count, same_runtime_count, unidentified_count):
        process_status = UNVERIFIED
        process_detail = str(processes.get("error") or "프로세스 관측이 누락되거나 형식이 올바르지 않습니다.")[:512]
    elif collector_count:
        process_status = BLOCKED
        process_detail = f"collector 표식이 있는 다른 프로세스 {collector_count}개가 관측됐습니다."
    elif unidentified_count:
        process_status = UNVERIFIED
        process_detail = (
            f"명확한 collector는 없지만 동일 런타임 {same_runtime_count}개, "
            f"식별 불가 Python {unidentified_count}개가 있습니다."
        )
    elif same_runtime_count:
        process_status = WARN
        process_detail = (
            f"collector 표식은 없지만 이 UI 관측 런타임과 같은 Python {same_runtime_count}개가 있습니다."
        )
    else:
        process_status = PASS
        process_detail = "읽기 가능한 프로세스 목록에서 기존 collector 충돌을 찾지 못했습니다."
    axes.append(_axis(
        "collector_process", "기존 수집 프로세스 충돌", process_status, process_detail,
        "실행 직전 새 admission으로 다시 관측하세요. 이 점검은 프로세스를 종료하지 않습니다.",
    ))

    window_count = _count(processes.get("runtime_or_openapi_windows")) if process_ok else None
    if window_count is None:
        window_status = UNVERIFIED
        window_detail = "Runtime/OpenAPI/Kiwoom 창 목록을 확인하지 못했습니다."
    elif window_count:
        window_status = BLOCKED
        window_detail = f"관련 제목의 창 {window_count}개가 관측됐습니다."
    else:
        window_status = PASS
        window_detail = "읽기 가능한 제목의 창에서 Runtime/OpenAPI/Kiwoom 흔적을 찾지 못했습니다."
    axes.append(_axis(
        "runtime_window", "Runtime/OpenAPI 창 상태", window_status, window_detail,
        "관련 창을 자동으로 닫지 말고 기존 세션/오류 상태를 사람이 확인하세요.",
    ))

    lease_state = lease.get("state") if isinstance(lease, dict) else None
    if isinstance(lease, dict) and lease.get("confirmed_free") is True \
            and lease_state in ("absent", "free") and lease.get("file_created") is False:
        lease_status = PASS
        lease_detail = "기존 lease 파일이 없거나 현재 1-byte 잠금이 비어 있음을 확인했습니다."
    elif lease_state in ("not_confirmed_free", "malformed_existing_file"):
        lease_status = BLOCKED
        lease_detail = f"collector lease를 안전하게 비었다고 확인할 수 없습니다 ({lease_state})."
    else:
        lease_status = UNVERIFIED
        lease_detail = f"collector lease 관측이 불완전합니다 ({lease_state!r})."
    axes.append(_axis(
        "collector_lease", "collector lease", lease_status, lease_detail,
        "lock 파일을 삭제하지 말고 실행 직전 공식 admission으로 다시 확인하세요.",
    ))

    axes.extend((
        _axis(
            "run_contract", "실행 대상 revision·CLI 계약", UNVERIFIED,
            "이 일반 UI 점검은 실행할 collector 명령, Git revision, clean tree를 고정하지 않습니다.",
            "실행 직전에 승인된 실행별 admission checker로 정확한 대상과 명령을 확인하세요.",
        ),
        _axis(
            "market_session", "실제 시장 날짜·장 구간", UNVERIFIED,
            "외부 공식 시장 일정과 현재 장 구간을 조회하지 않았습니다.",
            "실행 직전에 공식 출처로 거래일과 목표 구간을 별도 확인하세요.",
        ),
        _axis(
            "execution_approval", "수집 실행 승인", UNVERIFIED,
            "이 UI 점검은 수집 실행 승인을 만들거나 저장하지 않습니다.",
            "모든 최신 근거를 확인한 뒤 사람이 별도로 실행 여부를 결정하세요.",
        ),
    ))

    statuses = {axis["status"] for axis in axes}
    overall = BLOCKED if BLOCKED in statuses else UNVERIFIED if UNVERIFIED in statuses \
        else WARN if WARN in statuses else PASS
    local_axes = [axis for axis in axes if axis["id"] not in {"market_session", "execution_approval"}]
    local_statuses = {axis["status"] for axis in local_axes}
    local = BLOCKED if BLOCKED in local_statuses else UNVERIFIED if UNVERIFIED in local_statuses \
        else WARN if WARN in local_statuses else PASS
    return {
        "schema": "operator_collector_preflight_v1",
        "scope": "local_read_only_observation",
        "status": overall,
        "local_status": local,
        "checks": axes,
        "execution_approved": False,
        "market_verified": False,
        "storage_free_bytes": free if type(free) is int else None,
        "storage_code_minimum_bytes": minimum if type(minimum) is int else None,
        "non_actions": list(NON_ACTIONS),
        "note": (
            "PASS는 해당 순간의 좁은 관측만 뜻합니다. 이 보고서는 collector 실행 명령이나 "
            "로그인 승인을 만들지 않으며 시작 버튼 상태를 바꾸지 않습니다."
        ),
    }


def run_official_preflight(
    executable: Path,
    preflight_module: Path,
    *,
    runner: Callable = subprocess.run,
) -> dict:
    """Run only the existing pure preflight module in the 32-bit interpreter."""
    try:
        completed = runner(
            [str(executable), "-I", "-B", "-c", _ISOLATED_PREFLIGHT, str(preflight_module)],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"probe_ok": False, "error": f"{type(exc).__name__}: {str(exc)[:384]}"}
    if completed.returncode != 0:
        error = (completed.stderr or "").strip()[:384]
        return {"probe_ok": False, "error": f"preflight process exit={completed.returncode}: {error}"}
    try:
        result = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        return {"probe_ok": False, "error": f"invalid preflight JSON: {exc}"}
    if not isinstance(result, dict):
        return {"probe_ok": False, "error": "preflight JSON is not an object"}
    return {"probe_ok": True, "result": result}


def inspect_collector_preflight(
    root: Path,
    *,
    official_runner: Callable[[Path, Path], dict] = run_official_preflight,
    disk_reader: Callable[[Path], dict] = inspect_disk,
    process_reader: Callable[[Path], dict] = inspect_processes,
    lease_reader: Callable[[Path], dict] = inspect_existing_lease,
) -> dict:
    """Collect bounded local facts, then reduce them without external queries."""
    root = Path(root).resolve()
    executable = root / RUNTIME_RELATIVE
    runtime = {"path": str(executable), "exists": executable.is_file()}
    if runtime["exists"]:
        official = official_runner(executable, root / PREFLIGHT_RELATIVE)
    else:
        official = {"probe_ok": False, "error": "collector runtime file missing; probe not run"}

    def read(reader, label):
        try:
            return reader(root)
        except Exception as exc:
            return {"probe_ok": False, "error": f"{label}: {type(exc).__name__}: {str(exc)[:384]}"}

    report = reduce_collector_preflight(
        runtime=runtime,
        official_preflight=official,
        disk=read(disk_reader, "disk probe failed"),
        processes=read(process_reader, "process probe failed"),
        lease=read(lease_reader, "lease probe failed"),
    )
    report.update(
        observed_at_local=datetime.now().astimezone().isoformat(),
        collector_runtime_path=str(executable),
    )
    return report
