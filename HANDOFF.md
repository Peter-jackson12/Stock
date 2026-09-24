# 현재 인계 — 2026-09-24 / FID 병합 뒤 과거 비교의 상시 CI 분리 후보

[문서 인덱스](README.md) · [실험 계약](tests/FID_READ_AB_DIAGNOSTIC.md) ·
[실행 절차](docs/COLLECTION_RUNBOOK.md) · [보존본 안내](docs/archive/README.md)

## 현재 원격과 이번 후보

PR #29는 사용자의 원격 병합 진행 요청 후 master에 병합됐다.
병합 commit은 `8d84c9be728be25a707063559459324a61557585`이며 부모는 기존 master
`5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70`와 검토 HEAD
`53a44659de859c6d974f84efa6d83c2b463d428b`다. #24도 GitHub가 간접 병합으로 표시했다.
컨트롤타워가 명시적으로 호출한 merge 대상은 #29뿐이다. 브랜치 삭제·이력 재작성은 하지 않았다.
이것은 원격 코드 통합이며 운영 배포·OCX·Mock/Live 수집·실제 raw 처리 승인이 아니다.

현재 branch `audit/offline-revision-compare-20260923`는 기존 PR #19의 후속 후보다.
원래 HEAD `32e6285e2bd5a224a63152c2ed1f492b57afd841`와 위 master의 이력을 모두 보존한다.
새 PR이나 새 실행 계층을 만들지 않는다. #18 운영 판단 계약과 #21 UI는 이 후보에 넣지 않았다.
최종 HEAD/base/CI는 사용 직전 PR/Actions와 실제 원격 ref로 다시 확인한다.
병합 전 성공한 PR의 옛 base_sha나 과거 merge ref를 현재 master 검증으로 대신하지 않는다.

## 이번 변경과 검증의 의미

과거 비교 스크립트는 원래 blob `6cfd405efa249fd45bb4d72f6d89ee4132f24faf` 그대로 보존한다.
`ci.yml`은 위 master의 내용을 그대로 사용해 master push/PR Git-only 회귀와 FID 검사를 유지한다.
#19의 역사적 벤치마크 상시 실행 단계만 계승하지 않는다. 비교 스크립트와 실패/완료 근거는
[과거 비교 계약](tests/COLLECTOR_REVISION_BENCHMARK.md)에 연결하고 README에서 찾을 수 있게 한다.
새 자동 benchmark workflow나 성능 gate는 추가하지 않는다. 이번 일반 CI에서 벤치마크를 다시
실행했다고 주장하지 않는다. collector·queue·raw writer·FID·admission·연구 진입점은 변경하지 않는다.

#29의 병합 전 PR CI #328(run 35935823284, job 107432507947)은 전체 1,826 passed /
6 deselected, FID 집중 259 passed / 8 subtests, 시작·종료 집중 71 passed였다.
검증 ref `71a550bfb2c8a836a1973c283de1d6a428b29eeb`는 당시 master와 #29 HEAD의 결합이었다.
병합 후 master CI #329(run 35938525443)는 별도 실행이다. 완료 상태·수치는 Actions에서 확인한다.
#19의 원래 benchmark 결과는 push #207와 PR #208의 역사적 결과이지 이번 후보의 재측정값이 아니다.
집중·전체·로컬·반복·subtest 결과를 합산하지 않는다. deselected는 통과가 아니다.
GitHub-hosted Windows / Python 3.14 x64와 3.10 문법 검사는 실제 3.10 x86/OCX 검증이 아니다.

## FID 누적 계약과 남는 한계

#29에는 Mock 전용 FULL/ESSENTIAL/FULL 진단, PRE/A1/B/A2/POST 분리, 진단 scope의 실제 연구
진입점 배제, bounded analyzer, 사전등록 assessment, strict admission/manual run-plan 보강이 들어 있다.
90초는 종료 요청이지 hard cutoff가 아니다. 진단 OFF의 FID·payload·구독·종료 계약을 보존한다.
`kiwoom_universe_fid_read_diagnostic`는 research_eligible=false이며 analyzer READY는
raw 품질 합격·프로세스 종료·연구 적격성이 아니다. 효과크기 임계/p-value를 새로 만들지 않는다.

실행 검사는 exact SHA·clean root·Git toplevel·checker 출처·tracked entrypoint·32비트 preflight·
공간·process/window·lease·시각·외부 거래일 확인·사용자 승인을 대조한다. 누락/접근 불가는 clear가 아니다.
정상 venv launcher 예외는 부모·checker의 읽을 수 있는 비어 있지 않은 인자 일치가 필요하다.
원문 CommandLine은 출력하지 않는다. admission은 시작 기준 0 <= age < 60초, plan은 TTL 300초와
created <= t < expires를 사용하고 검사 완료 시각·단조 시계도 대조한다. prepare/verify는 실행하지 않는다.

verify 뒤 실제 수동 실행까지 race-free가 아니다. digest는 서명/사용자 인증이 아니다.
같은 인자의 무관 부모·보수적 창/marker 오탐, entrypoint 외 import shadowing/.pth/sitecustomize는 남는다.
실제 read byte 상한은 OS I/O 시간 보장이 아니다. 상세 계약을 이 인계에 다시 구현하지 않는다.

## 삭제하지 않는 실패와 미확정 항목

- #321(run 35926442378, job 107402550106): 원본 production의 23 failed / 3 passed.
  23개는 parameter case이며 독립 근본 원인 수가 아니다. 원본 감사 commit
  `8faa80ee1194bbabb3245862fb18f3355377d7e9`와 F1~F9 보강 이력을 보존한다.
- #323: 전체 1,814 passed / 6 deselected. R1 후 #324는 감사 PowerShell AST parser의
  10초 TimeoutExpired였고 같은 SHA의 #325는 success였다. 감사 timeout만 30초로 보정했다.
  OS 내부 지연 원인은 미확정이며 운영 timeout은 바꾸지 않았다.
- #327(run 35934021661, job 107426874350): 전체 1 failed / 1,823 passed / 6 deselected.
  `test_silence_stop_requests_shutdown_even_if_final_dump_fails[False]`에서 len(calls)==0과
  raw v2 시작 실패가 있었다. 최초 startup 예외 전문은 잘렸으며 로컬 단독/파일/인접 순서에서
  재현되지 않았다는 사용자 보고가 있다. 5초 queue ready 대기나 환경 문제로 원인을 확정하지 않는다.
- `53a44659...`의 start_capture helper는 시작 성공을 먼저 검사하고 원래 오류와 가능한 작은 상태를
  남긴다. 의도한 writer 시작 실패 대조군을 추가했으며 재시도하지 않는다. production 수정이 아니다.
  #328 success는 #327 원인 해결이나 재발 불가가 아니다. 재발하면 새 원문으로 조사한다.
- #156/#162, #19 #205/#206, #22 초기 harness, #26 #280 HANDOFF 크기 실패를 보존한다.
  #162 최초 원인은 미확정이며 #327과 같은 원인으로 묶지 않는다.

기존 상세 감사/한계는 [#29 고정 HANDOFF](https://github.com/Peter-jackson12/Stock/blob/53a44659de859c6d974f84efa6d83c2b463d428b/HANDOFF.md),
이전 전체 인계는 [#28 고정 HANDOFF](https://github.com/Peter-jackson12/Stock/blob/c156108c269ffec55fc1bf7c843397d56781f112/HANDOFF.md),
로컬 장애 원문은 [#21 고정 HANDOFF](https://github.com/Peter-jackson12/Stock/blob/4ce504dabca9baf37fd5c0a8dc062f484511c7ad/HANDOFF.md)를 따른다.
archive 원문은 수정하지 않는다.

## 원본과 운영 승인 경계

9월 21일 raw session `6f39117671c048f6b60477ceafbf40b6`, revision
`4821762fd93230b658339fee084d6c08e3e53ce9`, 50,635,071,488 bytes 및 WAL/SHM을 보존한다.
unsigned FID15/parse_error, sidecar 출처·whole-file 무결성/품질·첫 실제 연구는 미완료이며
FIRST_RESEARCH_CANDIDATE 미승격이다. writable 재연결·cleanup·qualification에는 대상 identity·
접근 배제·namespace/경로 격리·free space·I/O/시간 예산과 별도 승인이 필요하다.

9월 23일 장애 session `7a35b11eddff4dcea88c98acdc8b37df`의 메모리/dump/PID/Runtime 상태,
과거 LOCAL_PREFLIGHT_READY와 Python 3.10.11 x86 compile/집중 결과는 사용자 전달 로컬 보고다.
현재 로컬 HEAD·clean·프로세스 부재를 직접 관측한 사실이 아니다. 완료된 작은 canary와
96,000 callback x86 offline 시험은 반복하지 않는다. 운영 `.venv32`에 패키지를 임의 설치하지 않는다.

FID clock difference는 network latency가 아니다. source progression/lag slope는 독립 증거가 아니다.
ESSENTIAL은 COM 외 문자열/JSON/저장 비용도 바꾸며 backlog carry-over와 종목 표본 차이가 남는다.
작은 processing_ns·queue 0·offline 성공으로 Qt/COM/GIL/native 병목을 배제하지 않는다.
저장 closed / Qt 종료 / PID 부재 / Runtime 창 부재 / lease free는 별도 사실이며 최초 장애 인과는 미확정이다.

운영 raw/dump/operations_state/Daily_baseline/old_data/사용자 변경을 보존한다.
자동 kill/restart/relogin, lock 삭제, Runtime 창 닫기, LAA 변경, queue 확대, 기본 FID 축소는 금지한다.
raw→LOB/feature 변환을 raw-v2 연구의 필수 선행 단계로 바꾸지 않는다.

## 다음 행동

1. 이번 PR #19 후보의 실제 HEAD와 자동 PR CI를 확인한다. 옛 benchmark와 전체 감사를 재실행하지 않는다.
2. 병합 우선순위는 #18 운영 판단 계약 → #21 UI → #19다. 이번 #19 준비가 두 PR의 병합을 대신하지 않는다.
   선행 병합으로 master가 바뀌면 최신 결합 diff/충돌/CI를 확인한다. 기존 #20/#22/#23/#25~#28은 임의 종료하지 않는다.
3. 실제 전종목 Mock A-B-A는 미실행이다. 별도 승인 후 당일 공식 거래일/특별개장·시장 구간·최종 revision·
   현재 CLI·local preflight/process를 다시 대조한다. 과거 9월 28일 언급은 예약/실행 승인이 아니다.
   09:15 <= KST < 15:15는 이 실험 목표 창이지 프로젝트 전체 수집 시간이나 침묵 판정 범위가 아니다.
   첫 결과 검토 전 Live 비교·동시 두 계정·반복 전종목 실험으로 확대하지 않는다.
