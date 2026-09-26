"""Legacy KIS 일일 데몬의 단계별 결과 판정 (PIPELINE_AUDIT 2026-09-26 P2).

`run_daily_daemon.py`는 장중 raw 수집 → LOB 변환 → 일봉 갱신을 이어서 돌리지만, 예전에는
각 단계 반환값을 보지 않고 마지막에 무조건 "완벽히 끝났다"고 출력했다. 이 모듈은 각 단계의
**기존 반환 계약**을 그대로 읽어 단계 결과로 바꾸고, 모든 필수 단계가 성공일 때만 완전
성공으로 본다. 네트워크·파일·API 에 의존하지 않아 합성 테스트로 검증한다.

이 모듈은 저장 데이터를 삭제·되돌리지 않고, 재시도·자동 복구도 하지 않는다.
Kiwoom 운영 수집기와는 별개의 legacy 경로다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

SUCCEEDED = "succeeded"
PARTIAL = "partial"          # 일부만 됐다 — 완전 성공이 아니다
NO_DATA = "no_data"          # 이 단계에서 무데이터는 성공이 아니다
FAILED = "failed"
UNVERIFIED = "unverified"    # 반환값이 기존 계약과 달라 결과를 확인할 수 없다
NOT_RUN = "not_run"          # 앞 단계 때문에 실행하지 않았다

RAW_CAPTURE = "raw_capture"
LOB_BUILD = "lob_build"
DAILY_UPDATE = "daily_update"
REQUIRED_STEPS = (RAW_CAPTURE, LOB_BUILD, DAILY_UPDATE)

LABELS = {RAW_CAPTURE: "실시간 raw 수집", LOB_BUILD: "1초봉 LOB 변환", DAILY_UPDATE: "일봉 CSV 갱신"}
STATUS_LABELS = {SUCCEEDED: "성공", PARTIAL: "일부 성공", NO_DATA: "무데이터", FAILED: "실패",
                 UNVERIFIED: "결과 미확인", NOT_RUN: "미실행"}
MAX_DETAIL_CHARS = 512


@dataclass(frozen=True)
class StepResult:
    name: str
    status: str
    detail: str = ""


def raw_capture_step(*, approval_ok: bool, tick_count: int) -> StepResult:
    """raw 수집: 승인키 실패는 실패, 적재 0건은 무데이터(장중 수집 데몬에서 성공이 아니다)."""
    if not approval_ok:
        return StepResult(RAW_CAPTURE, FAILED, "Approval Key 발급 실패 — 수집하지 않음")
    if type(tick_count) is not int or tick_count < 0:
        return StepResult(RAW_CAPTURE, UNVERIFIED, f"적재 건수 미확인: {tick_count!r}")
    if tick_count == 0:
        return StepResult(RAW_CAPTURE, NO_DATA, "적재 0건")
    return StepResult(RAW_CAPTURE, SUCCEEDED, f"적재 {tick_count:,}건")


def lob_step(result) -> StepResult:
    """`resample_raw_to_lob` 반환 계약.

    None = 원본 틱 파일 없음(함수가 ❌로 출력하고 바로 반환) → 실패.
    dict 의 failures 가 있으면 CLI(`build_lob_db.main`)도 1을 반환한다 → 실패.
    변환한 종목/행이 0이면 무데이터. 그 밖의 형태는 결과 미확인.
    """
    if result is None:
        return StepResult(LOB_BUILD, FAILED, "원본 틱 파일 없음 — LOB 를 만들지 않음")
    if not isinstance(result, dict) or not {"codes", "empty", "failures", "rows"} <= set(result):
        return StepResult(LOB_BUILD, UNVERIFIED, f"예상하지 않은 반환값: {type(result).__name__}")
    failures = result["failures"] or []
    converted = result["codes"] - len(result["empty"] or []) - len(failures)
    if failures:
        codes = ", ".join(str(code) for code, *_ in failures[:5])
        return StepResult(LOB_BUILD, FAILED,
                          f"실패 {len(failures)}종목 ({codes}{' 외' if len(failures) > 5 else ''}), 변환 {converted}종목")
    if result["codes"] == 0 or result["rows"] == 0 or converted <= 0:
        return StepResult(LOB_BUILD, NO_DATA, f"변환 대상 {result['codes']}종목 / {result['rows']:,}초봉")
    return StepResult(LOB_BUILD, SUCCEEDED,
                      f"변환 {converted}종목 / {result['rows']:,}초봉 (체결 0건 스킵 {len(result['empty'] or [])})")


def daily_step(result) -> StepResult:
    """`FastDailyCollector.collect` 반환 계약 — journal 의 no_data/completed 와 같은 사실.

    completed 라도 요청 기간 가격을 받지 못한 종목이 있으면 일부 성공이다.
    None(이전 revision 처럼 반환 없음)이나 모르는 status 는 결과 미확인이다.
    """
    if not isinstance(result, dict) or "status" not in result:
        return StepResult(DAILY_UPDATE, UNVERIFIED, "collect() 결과를 확인할 수 없음")
    journal = f" · 기록 {result.get('journal')}" if result.get("journal") else ""
    if result["status"] == "no_data":
        return StepResult(DAILY_UPDATE, NO_DATA, "수집된 일봉/스냅샷 없음" + journal)
    if result["status"] != "completed":
        return StepResult(DAILY_UPDATE, UNVERIFIED, f"모르는 결과 상태: {result['status']!r}" + journal)
    targets, priced = result.get("targets"), result.get("priced")
    if type(targets) is not int or type(priced) is not int:
        return StepResult(DAILY_UPDATE, UNVERIFIED, "종목 수 미확인" + journal)
    if targets > 0 and priced == targets:
        return StepResult(DAILY_UPDATE, SUCCEEDED, f"가격 수신 {priced}/{targets}종목" + journal)
    return StepResult(DAILY_UPDATE, PARTIAL, f"가격 수신 {priced}/{targets}종목" + journal)


def exception_step(name: str, exc: BaseException) -> StepResult:
    return StepResult(name, FAILED, f"예외 {type(exc).__name__}: {exc}"[:MAX_DETAIL_CHARS])


@dataclass
class DaemonOutcome:
    steps: list[StepResult] = field(default_factory=list)

    def status_of(self, name: str) -> str:
        for step in self.steps:
            if step.name == name:
                return step.status
        return NOT_RUN

    @property
    def complete(self) -> bool:
        return all(self.status_of(name) == SUCCEEDED for name in REQUIRED_STEPS)

    @property
    def status(self) -> str:
        """complete / partial(한 단계 이상 성공·일부 성공) / failed."""
        if self.complete:
            return "complete"
        if any(self.status_of(name) in (SUCCEEDED, PARTIAL) for name in REQUIRED_STEPS):
            return "partial"
        return "failed"

    @property
    def exit_code(self) -> int:
        return 0 if self.complete else 1

    def lines(self) -> list[str]:
        out = []
        for name in REQUIRED_STEPS:
            step = next((s for s in self.steps if s.name == name), StepResult(name, NOT_RUN))
            detail = f" — {step.detail}" if step.detail else ""
            out.append(f"   {LABELS[name]}: {STATUS_LABELS.get(step.status, step.status)}{detail}")
        if self.complete:
            out.append("🎉🎉 [수집 완료] 오늘의 모든 데이터 수집 및 전처리가 완벽히 끝났습니다!")
        elif self.status == "partial":
            out.append("⚠️ [일부 완료] 성공하지 않은 단계가 있습니다. 저장된 산출물은 지우지 않았고 자동 재시도하지 않습니다.")
        else:
            out.append("❌ [수집 실패] 필수 단계가 성공하지 않았습니다. 자동 재시도하지 않습니다.")
        return out


def post_process(outcome: DaemonOutcome, date: str, codes: Sequence[str], *,
                 resample: Callable, collector_factory: Callable,
                 daily_start: str, emit: Callable[[str], None] = print) -> DaemonOutcome:
    """장 마감 후 LOB 변환 → 일봉 갱신. 각 단계의 반환값을 판정해 outcome 에 쌓는다.

    예외는 그 단계를 실패로 기록하고 요약을 출력한 뒤 그대로 다시 던진다 — 기존처럼
    뒤 단계는 실행하지 않고(미실행으로 표시), 프로세스는 0 이 아닌 상태로 끝난다.
    """
    steps = (
        (LOB_BUILD, "1️⃣ [LOB 생성] 원본 틱 ➔ 1초봉 LOB 변환 시작...", lambda: lob_step(resample(date))),
        (DAILY_UPDATE, "2️⃣ [일봉 갱신] 오늘 날짜 일봉 8대 매트릭스 CSV 갱신 시작...",
         lambda: daily_step(collector_factory().collect(
             start_date=daily_start, end_date=date, target_tickers=list(codes)))),
    )
    for name, banner, run in steps:
        emit(banner)
        try:
            outcome.steps.append(run())
        except BaseException as exc:
            outcome.steps.append(exception_step(name, exc))
            for line in outcome.lines():
                emit(line)
            raise
    return outcome
