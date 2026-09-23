# 현재 인계 — 2026-09-24 / FID 체인 독립 실패 주입 감사

[문서 인덱스](README.md) · [실험 계약](tests/FID_READ_AB_DIAGNOSTIC.md) ·
[실행 절차](docs/COLLECTION_RUNBOOK.md) · [보존본 안내](docs/archive/README.md)

## 이번 branch의 목적과 범위

PR #28 `c156108c269ffec55fc1bf7c843397d56781f112`의 production을 그대로 두고,
`tests/test_fid_chain_fail_closed_audit.py`로 독립적인 fail-closed 계약을 공격한다.
이 branch는 새 실행 계층이나 운영 적용 후보가 아니다. 감사 결과와 정확한 HEAD/run/job는 감사 PR에 기록한다.
기존 CI #320은 success였지만 의미적 결함 부재를 인증하지 않는다. 기존 전체 검사를 재실행하지 않는다.

이 branch의 CI는 pull_request 이벤트에서 독립 감사만 한 번 수행한다. production diff가 없는지 먼저 확인한다.
실패를 skip/xfail/continue-on-error로 숨기지 않는다. 이 감사용 CI 설정은 통합/병합 대상이 아니다.
수정 작업에서는 테스트를 유지하고 정상 CI를 복원해 변경 범위에 맞는 focused/full 검증을 적용한다.

시험 대상은 불완전 process 조회, 다른 interpreter의 module collector, 검사 중 시각/만료 경계,
READY 내부 모순, plan 재타임스탬프, ignored 하위 경로의 Git identity 차용,
PowerShell 명령 AST, 실제 읽은 byte 상한, malformed 완료 보고, 음수/비유한 계측이다.
기존 fixture는 입력 구성에만 재사용하며 pass/fail oracle는 새 테스트에 독립 정의했다.
OS 조회는 대역, Git 변경은 runner 임시 fixture 저장소, PowerShell은 AST 파싱만이다.
실제 collector/OCX/사용자 raw/operations_state 조회나 수집 실행은 하지 않는다.

## 원격·기존 작업

감사 시작 master: `5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70`, 최신 master CI #195 success.
PR #18~#28은 시작 조회에서 모두 open이다. 정확한 각 PR HEAD/CI는 사용 직전 다시 확인한다.
#18 운영 판단 계약, #19 revision 비교, #21 UI 표시는 별도이며 변경하지 않는다.
FID 통합 후보 #24 → 결과 analyzer #25 → 사전등록 #26 → admission #27 → run-plan #28도
자동 병합하거나 닫지 않는다. 임의 실행 승인도 하지 않는다.

이전 전체 인계와 수치/경로 원문은
[PR #28 고정 HANDOFF](https://github.com/Peter-jackson12/Stock/blob/c156108c269ffec55fc1bf7c843397d56781f112/HANDOFF.md)에 보존한다.
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
저장/Qt finally/lease 해제 뒤 PID·창 잔류는 사용자 전달 로컬 보고이지 이번 ChatGPT의 직접 관측이 아니다.
완료된 작은 canary·96,000 callback x86 offline 시험은 반복하지 않는다.
popup 최초 표시와 failure-time VA exhaustion/fragmentation·최초 장애 인과는 미확정이다.
FID clock difference는 network latency가 아니고 progression/lag slope는 독립 증거가 아니다.
작은 processing_ns·queue 0·offline 성공으로 Qt/COM/GIL/native 병목을 배제하지 않는다.

CI #156/#162, PR #19 #205/#206, PR #22 초기 harness 실패와 PR #26 CI #280 HANDOFF 초과 실패를 보존한다.
#162의 근본 원인은 미확정이다. 집중·전체·반복·subtest를 합산하지 않고 deselected는 통과로 세지 않는다.

운영 raw/dump/operations_state/Daily_baseline/old_data/사용자 변경을 보존한다.
자동 kill/restart/relogin, lock 삭제, Runtime 창 닫기, LAA 변경, queue 확대, 기본 FID 축소는 금지한다.
raw→LOB/feature 변환을 현 raw-v2 연구의 필수 선행 단계로 바꾸지 않는다.
시장 실행은 별도 승인 후 당일 공식 거래일/시장 구간·현재 CLI·최종 revision을 다시 대조한다.

## 다음 행동

감사 실패를 production 장애와 혼동하지 말고 실제 assertion/관측값으로 결함을 확인한다.
확인된 결함의 보강은 한 묶음으로 수행하며 새 gate/새 PR 스택을 계속 추가하지 않는다.
시간·identity·I/O 경계를 바꾸는 수정은 높은 추론 수준에서 반례 우선으로 수행하고 별도로 재검토한다.
실제 실행 명령·날짜·revision은 지금 고정하지 않는다. 감사 branch를 로컬 수집에 사용하지 않는다.
