"""읽기 전용 운영 안내. 수집/GUI/연구 실행이나 환경 수정을 하지 않는다."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
from pathlib import Path
import sys

# 직접 실행에서도 기존 관측 모듈의 bytecode를 운영 체크아웃에 쓰지 않는다.
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from control_tower.operator_environment import (
    REQUIRED_FILES,
    UI_PACKAGES,
    _runtime,
    inspect_operator_environment,
)

SCHEMA = "stock_operator_v1"
UNVERIFIED = (
    "process_state", "feed_health", "market_session", "data_quality", "research_eligibility",
)


def _base(kind: str, root: Path) -> dict:
    return {
        "schema": SCHEMA, "kind": kind,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository_root": str(root), "read_only": True,
        "execution_approved": False,
    }


def doctor(root: Path) -> dict:
    """실행 중인 64-bit 환경과 소수의 명시된 파일/패키지 metadata만 점검한다."""
    return inspect_operator_environment(
        root,
        base=_base("doctor", root),
        runtime_provider=_runtime,
        package_version=metadata.version,
    )


def status(root: Path) -> dict:
    """기존 bounded reader를 재사용한다. 원본 DB/PID/창/lease는 조회하지 않는다."""
    from control_tower.status import observe_collector, observe_raw_capture

    result = _base("status", root)
    now = datetime.now(timezone.utc)
    log = observe_collector(root, now=now)
    raw = observe_raw_capture(root, now=now)
    payload = raw.get("payload") or {}
    snapshot = payload.get("snapshot") or {}
    result.update({key: "unverified" for key in UNVERIFIED})
    result.update(
        scope="bounded_saved_evidence_only",
        log_evidence={
            "status": log["status"], "path": log["log_path"],
            "age_seconds": log.get("age_seconds"),
            "heartbeat": log.get("heartbeat"),
        },
        raw_status_evidence={
            "status": raw["status"], "path": raw["path"],
            "age_seconds": raw.get("age_seconds"),
            "producer_observed_at_utc": payload.get("observed_at_utc"),
            "session_id": (payload.get("identity") or {}).get("session_id"),
            "producer_claim_state": snapshot.get("state"),
            "accepted_callbacks": snapshot.get("accepted_callbacks"),
            "committed_seq": snapshot.get("committed_seq"),
            "pending_callbacks": snapshot.get("pending_callbacks"),
        },
    )
    return result


def _ps_quote(value: str | Path) -> str:
    text = str(value)
    if any(character in text for character in ("\r", "\n", "\x00")):
        raise ValueError("명령 경로에 줄바꿈/널 문자를 허용하지 않습니다")
    return "'" + text.replace("'", "''") + "'"


def ui_command(root: Path) -> dict:
    """기존 GUI의 수동 명령만 보여준다. shell/process/browser를 실행하지 않는다."""
    root = root.absolute()
    result = _base("ui-command", root)
    result.update(
        scope="manual_command_only", launched=False,
        command=(
            "Set-Location -LiteralPath " + _ps_quote(root) + "\n& "
            + _ps_quote(root / ".venv/Scripts/python.exe")
            + " -B -m streamlit run " + _ps_quote(root / "dashboard/app.py")
            + " --server.address 127.0.0.1"
        ),
        warning="GUI는 읽기 전용이 아닙니다. 작업 상태 DB를 열 수 있으며 실행은 별도 로컬 승인 범위입니다.",
    )
    return result


def render(report: dict) -> str:
    lines = ["Stock 운영 도우미 — " + report["kind"], "저장소: " + report["repository_root"]]
    if report["kind"] == "doctor":
        lines += [f"[{c['status']}] {c['name']}: {c['detail']}" for c in report["checks"]]
        lines.append("환경 목록 점검: " + ("통과" if report["inventory_passed"] else "미충족"))
        lines.append("목록 통과 != GUI 정상 기동 != 수집/시장/연구 실행 승인. 자동 수정하지 않았습니다.")
        lines.append("다음: START_HERE.md에서 누락 항목과 실행 경계를 확인하세요.")
    elif report["kind"] == "status":
        log, raw = report["log_evidence"], report["raw_status_evidence"]
        lines += [
            "저장된 로그 근거: " + log["status"] + f" (경과 {log['age_seconds']}초)",
            "저장된 수집 상태 근거: " + raw["status"] + f" (경과 {raw['age_seconds']}초)",
            "과거 생산자 주장 상태: " + str(raw["producer_claim_state"]),
            "프로세스 생존 / 피드 정상 / 시장 구간 / 데이터 품질 / 연구 적격성: 모두 미확인",
            "파일 부재는 STOPPED가 아니며, recent/closed도 현재 정상 운용 인증이 아닙니다.",
            "다음: START_HERE.md의 상태 읽는 법. 자동 수집 시작/재시작은 하지 않습니다.",
        ]
    else:
        lines += [report["warning"], "아래는 안내일 뿐이며 실행하지 않았습니다:", report["command"],
                  "실행 후 브라우저 탭 종료 != 수집기/워커 종료. START_HERE.md를 먼저 확인하세요."]
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(
        description="Stock 읽기 전용 안내: 기존 GUI와 엔진을 대체하지 않습니다.",
        epilog="시작 안내: START_HERE.md. collector start/stop/canary와 backtest는 연결하지 않습니다.",
    )
    parser.add_argument("command", nargs="?", default="help",
                        choices=("help", "doctor", "status", "ui-command"))
    parser.add_argument("--json", action="store_true", help="기계 판독용 JSON 출력")
    args = parser.parse_args(argv)
    if args.command == "help":
        parser.print_help()
        return 0
    try:
        report = {"doctor": doctor, "status": status, "ui-command": ui_command}[args.command](root)
    except (OSError, ValueError, ImportError) as exc:
        error = {"schema": SCHEMA, "kind": args.command, "error": type(exc).__name__,
                 "message": str(exc), "execution_approved": False}
        print(json.dumps(error, ensure_ascii=False) if args.json else f"점검 실패: {exc}")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    return 1 if report.get("inventory_passed") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
