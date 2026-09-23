# 현재 인계 — 2026-09-24 / FID 체인 fail-closed 감사와 보강

[문서 인덱스](README.md) · [실험 계약](tests/FID_READ_AB_DIAGNOSTIC.md) ·
[실행 절차](docs/COLLECTION_RUNBOOK.md) · [보존본 안내](docs/archive/README.md)

## 이번 branch의 상태

PR #29 branch `audit/fid-chain-fail-closed-20260924`는 PR #28 `c156108c269ffec55fc1bf7c843397d56781f112` 위의
감사 커밋 `8faa80ee1194bbabb3245862fb18f3355377d7e9`와 그 반례를 해소하는 보강 커밋으로 구성된다.
감사 CI #321(run 35926442378, job 107402550106)은 원래 production에서 26개 중 23개 실패였다.
이 실패 이력은 지우지 않는다. 23개는 parameter case 수이며 독립 근본 원인 수가 아니다.

보강은 새 실행 계층 없이 기존 admission / run-plan / analyzer / assessment와 prepare/verify CLI를 고친다.

- admission: 명시적 빈 조회와 누락·잘림·접근 불가를 구분(후자는 UNCERTAIN), 모든 interpreter의
  script/`-m` collector 탐지, CommandLine 원문 미출력, 관측 시작·완료 두 시점 평가와 단조 시계 대조,
  Git `--show-toplevel`·checker 출처·collector entrypoint의 tracked HEAD blob 결합, skip-worktree 차단.
- builder와 verifier가 같은 strict pure evaluator(`require_ready_admission`)로 READY를 재계산한다.
- run plan: 반개구간 `created <= t < expires`, 60초 신선도는 관측 시작 기준, verify 완료 시각 재검사,
  embedded admission의 plan 생성 시점 유효성 재검증, PowerShell `&` 호출 표시, 실제 read byte 상한.
- analyzer: 실제 read byte·JSONL 줄/누적 상한, 기존 lifecycle validator로 full identity/Finalization,
  명시적 UTC, bool이 아닌 정수 계수, 중첩 오류, 음수·비유한 telemetry를 근거 오류로 처리.
- assessment: malformed 요약은 `INVALID_PHASE_METRIC_SUMMARY`로 not-assessable. 최소 3개·중앙값 방향·
  효과크기 임계/p-value 없음이라는 사전등록 규칙은 바꾸지 않았다.

감사 대조군 `test_control_explicit_block_withholds_command`는 입력 객체가 plan과 공유돼 digest mismatch로
먼저 거부되던 harness 결함이 있었다. fresh 응답을 deepcopy로 분리해 BLOCKED/UNCERTAIN 분기를 직접 검증한다.
production도 plan에 admission 사본을 넣는다. 감사 assertion 자체는 약화하지 않았다.

CI는 감사 전용 workflow 대신 PR #28의 정상 CI로 복원하고 FID 집중 단계에 감사/보강 회귀를 넣었다.
정확한 최종 HEAD·run·job·결과는 PR #29의 checks와 완료 보고에서 확인한다(이 파일에 고정하지 않음).
집중·전체·반복·subtest 결과를 합산하지 않는다.

## 검증 범위와 남는 한계

프로세스 입력은 합성 대역, Git 쓰기는 pytest 임시 저장소, PowerShell은 AST 파싱과 `python -c` dummy뿐이다.
운영 프로세스 조회·실제 collector·OCX·사용자 raw/operations_state 접근은 없었다. Python 3.10은 3.10.11 x86
인터프리터의 compile 검사이며 32비트 런타임에서 회귀를 실행한 것은 아니다.

- verify 완료 뒤 사람이 명령을 실행하기까지는 검사하지 못한다(race-free 아님).
- SHA-256 digest는 서명이 아니며 `--execution-approved`는 사용자 인증이 아니다.
- 명령줄에 collector 이름이 있는 비-collector 프로세스, 제목에 kiwoom/키움/OpenAPI가 있는 창(터미널·브라우저
  포함)도 보수적으로 BLOCKED다. 권한 밖 Python 프로세스는 UNCERTAIN으로 남아 실행을 막을 수 있다.
- plan created를 60초 신선도 안에서 옮긴 경우는 구별하지 못한다(verify는 fresh admission을 다시 요구).
- 컨트롤타워 R1(부모 PID·같은 executable만으로 launcher 제외)은 부모·checker 명령줄의 인자 일치라는
  명시적 연결이 있을 때만 제외하도록 좁혔다. 명령줄 누락은 UNCERTAIN, 무관한 부모는 제외하지 않는다.
  인자까지 같은 무관한 부모 프로세스는 구별하지 못한다.
- 최종 후보를 별도 agent가 독립 재검토했다. READY 오발급 경로는 찾지 못했고, 지적된 정상 경로 차단
  (venv launcher 부모, 실행 경로 대소문자), CLI traceback, 엄격 타입 누락은 반영하고 회귀를 추가했다.
- entrypoint 외 모듈의 ignored 파일 shadowing, `.pth`/sitecustomize 같은 import 환경은 범위 밖이다.
- byte 상한은 메모리 상한이며 OS I/O 시간을 보장하지 않는다.
- analyzer READY는 raw 품질 합격·프로세스 종료·연구 적격성이 아니다.

## 원격·기존 작업

감사 시작 master `5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70`. PR #18~#29는 open이며 이번 작업에서
병합·종료·force-push하지 않는다. 정확한 각 PR HEAD/CI는 사용 직전 다시 확인한다.
#18 운영 판단 계약, #19 revision 비교, #21 UI 표시는 별도이며 변경하지 않는다(#21 UI 미통합).
FID 통합 후보 #24 → analyzer #25 → 사전등록 #26 → admission #27 → run-plan #28 → 감사·보강 #29 순서다.

이전 전체 인계와 수치/경로 원문은
[PR #28 고정 HANDOFF](https://github.com/Peter-jackson12/Stock/blob/c156108c269ffec55fc1bf7c843397d56781f112/HANDOFF.md),
로컬 장애 상세는
[PR #21 고정 HANDOFF](https://github.com/Peter-jackson12/Stock/blob/4ce504dabca9baf37fd5c0a8dc062f484511c7ad/HANDOFF.md)를 따른다.
archive 원문은 수정하지 않는다.

## 보존·승인 경계

9월 21일 raw session `6f39117671c048f6b60477ceafbf40b6`, revision
`4821762fd93230b658339fee084d6c08e3e53ce9`, 50,635,071,488 bytes 및 WAL/SHM은 보존한다.
unsigned FID15/parse_error 문제, sidecar 출처·whole-file 무결성/품질 확인과 첫 실제 연구는 미완료다.
FIRST_RESEARCH_CANDIDATE 미승격을 유지한다. writable 재연결·sidecar cleanup·전체 qualification에는
대상 identity·접근 배제·namespace/경로 격리·free space·I/O/시간 예산과 별도 승인이 필요하다.

9월 23일 preflight READY, 장애 session `7a35b11eddff4dcea88c98acdc8b37df`의 메모리/dump,
저장/Qt finally/lease 해제 뒤 PID·창 잔류는 사용자 전달 로컬 보고이지 직접 관측이 아니다.
완료된 작은 canary·96,000 callback x86 offline 시험은 반복하지 않는다.
popup 최초 표시와 failure-time VA exhaustion/fragmentation·최초 장애 인과는 미확정이다.
FID clock difference는 network latency가 아니고 progression/lag slope는 독립 증거가 아니다.
작은 processing_ns·queue 0·offline 성공으로 Qt/COM/GIL/native 병목을 배제하지 않는다.

CI #156/#162, PR #19 #205/#206, PR #22 초기 harness 실패, PR #26 CI #280 HANDOFF 초과 실패,
PR #29 감사 CI #321의 23 failed를 보존한다. #162의 근본 원인은 미확정이다. deselected는 통과가 아니다.

운영 raw/dump/operations_state/Daily_baseline/old_data/사용자 변경을 보존한다.
자동 kill/restart/relogin, lock 삭제, Runtime 창 닫기, LAA 변경, queue 확대, 기본 FID 축소는 금지한다.
raw→LOB/feature 변환을 현 raw-v2 연구의 필수 선행 단계로 바꾸지 않는다.

## 다음 행동

1. PR #29 보강 HEAD의 자동 CI(push/pull_request) 결과를 확인하고, 별도 독립 재검토로 새 반례를 찾는다.
2. 병합 순서와 여부는 사용자가 결정한다. #24~#29 readiness를 운영 승인처럼 쓰지 않는다.
3. 실제 Mock A-B-A는 별도 승인 후 당일 공식 거래일/시장 구간·최종 revision·현재 CLI를 다시 대조하고
   RUNBOOK 3-1/3-2 절차를 따른다. 이 branch 자체를 로컬 수집에 사용하지 않는다.
