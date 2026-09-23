# 현재 인계 — 2026-09-23 / FID A-B-A 통합 후보

[문서 인덱스](README.md) · [실험/감사 계약](tests/FID_READ_AB_DIAGNOSTIC.md) ·
[파이프라인 지도](docs/PIPELINE_MAP.md) · [첫 시험 체크리스트](BACKTEST_TODO.md) ·
[보존본 안내](docs/archive/README.md)

## ref와 작업 경계

이 인계는 PR #20 → #22 → #23의 선형 스택을 **현재 master 위 하나의 통합 후보 트리**로
재구성한 상태를 설명한다. 소스 스택의 최종 HEAD는
`b5ae402aac0088d27382aa54ff29a619fc078185`이며, 기능/테스트 코드는 그 최종 트리를 기준으로 한다.
통합 후보는 master에 아직 병합되지 않았고 실제 시장 실행·로컬 배포 승인도 아니다.
원격 GitHub에 보이지 않는 로컬 동시 작업까지 없다고 인증하는 것은 아니다.

확인한 master는 `5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70`다.
FID 진단 스택은 PR #20→#22→#23, master 기준 통합 후보는 PR #24,
bounded analyzer는 PR #25, 실제 결과 전 사전등록 판정은 현재 후속 PR에서 진행 중이다.
모두 draft/open/unmerged이며 자동 병합하지 않는다. PR #18/#19/#21도 별개 범위로 유지한다.
과거 HEAD/CI와 중간 실패는 각 PR/Actions 및
[PR #21 고정 HANDOFF](https://github.com/Peter-jackson12/Stock/blob/4ce504dabca9baf37fd5c0a8dc062f484511c7ad/HANDOFF.md),
[PR #20 검토 기준 HANDOFF](https://github.com/Peter-jackson12/Stock/blob/c263003da6be82cefe740d6b22896babfcc89cbc/HANDOFF.md)에 보존한다.
집중·전체·반복 테스트는 합산하지 않고 deselected는 통과로 세지 않는다.

## 이번 보강과 남은 한계

실제 진입점에 진단 manifest 연구 배제를 연결했다: `run_raw_v2`, 예약 provenance를 받는
`run_research`, UI 장외 워커, qualification의 적격성 출력. 일반 raw reader/무결성 검사는 유지한다.
직접 backend 생성에서도 mock/diagnostic scope를 먼저 확인한다.
FID 조회 중 예외까지 읽힌 원문을 보존하고 시도/성공 반환/실패 계수를 구분한다.
추가 timing clock의 예외·부적절한 값은 raw 오류나 원래 queue 예외를 덮지 않는다.

일반 OFF의 FID 목록/순서, raw-v2 기본 저장·queue·종료·기본 CLI는 바꾸지 않는다.
logger의 구독/종료 실행 코드는 바꾸지 않고 대역+작은 합성 DB로 계약을 검증한다.
실험 문서는 순수 COM 비용이라는 과장을 제거하고 None/문자열/JSON/저장 비용의 동반 변화를 적었다.
30초 phase 간 backlog는 초기화되지 않는다. 종류별 5초 최초 callback은 종목별 표본이 아니다.
source progression과 lag slope는 같은 시계에서 나온 값이며 독립 증거가 아니다.

duration 90초가 종료 **요청** 기준이고 hard cutoff가 아니라는 점은 유지한다.
다만 PR #23에서는 FID 읽기 정책 결정 시점이 [60,90)이면 A2_FULL, >=90이면
POST_90S_FULL로 분리한다. POST도 읽기 정책은 FULL이며 종료 처리 tail을 버리지 않는다.
따라서 A2 누적치에 post-90 시작 callback이 섞이는 문제는 제거되지만, phase 간 backlog를
초기화하지 않으므로 A2 callback 수/30을 독립 정상상태의 순수 service rate로 과장하지 않는다.
sidecar schema는 `fid_read_ab_test_v2`이고 A1/B/A2만 analysis_window=true다.
sidecar 누락/flush 실패/진단 오류가 있는 결과를 완전한 실험으로 해석하지 않는다.
임의로 출처 정보를 버린 normalized stream까지 진단 데이터라고 알아낼 수는 없다.

스택 전용 `.github/workflows/fid-read-audit.yml`은 임시 base branch에만 반응하므로
통합 후보에서는 제거하고 전용 FID 회귀를 기존 `CI`의 조기 단계에 편입한다.
이렇게 해야 master 기준 PR에서도 같은 경계 검사가 유지되고 죽은 workflow를 남기지 않는다.

통합 후보의 master-base CI와 누적 diff 검증 뒤, 실제 실행 절차는
[COLLECTION_RUNBOOK의 Mock 전종목 FID A-B-A 절](docs/COLLECTION_RUNBOOK.md#mock-전종목-fid-a-b-a--실행-전-사전점검과-1회-실행-계약)에 고정했다.
다음 기본 행동은 Windows 전용 clean checkout/worktree에서 **로그인 없는 `--preflight`**를 실행해
32비트 Python·OCX 등록·정확한 revision·기존 collector/PID/Runtime 잔류·free space를 확인하는 것이다.
preflight 자체는 실제 Mock 로그인·수집 승인이 아니다.

실제 Mock 90초 1회는 공식 거래일/시장 구간을 당일 다시 확인하고 사전점검이 모두 통과한 뒤 별도 실행한다.
GitHub에서 가능한 작업은 일반 채팅에서 수행한다. 로컬 원본/OCX가 필요한 일만 별도 승인 후 넘긴다.

## FID A-B-A 결과 판정 사전등록 — 실제 운영 결과 전

PR #25의 bounded analyzer 위에서 `fid_read_ab_assessment_v1` 규칙을 별도 후속 브랜치에 고정했다.
이 작업은 실제 Mock A-B-A 운영 결과를 보기 전에 완료하는 pre-registration이며 collector hot path를 수정하지 않는다.

- analyzer가 `CAPTURE_COMPLETE_ANALYSIS_READY`가 아니면 자동 A-B-A 판정을 하지 않는다.
- 1차 지표는 `fid_read_ns`; 체결/호가는 절대 합치지 않는다.
- A1/B/A2 각 phase·real_type의 유효 표본 최소 3개는 coverage guardrail이며 통계적 power 기준이 아니다.
- 효과크기 threshold와 p-value는 두지 않고 실제 결과 뒤 조정하지 않는다.
- B 중앙값이 A1/A2 둘보다 낮음/높음/그 외만 기술적으로 분류한다.
- `processing_ns`는 보조, `queue_submit_ns`는 guardrail, source clock·callback/30·resource history는 자동 판정에서 제외한다.
- PRE/POST는 A-B-A 비교에서 제외한다.
- 결과 label은 causal verdict, research eligibility, 실시장 승인, Qt/COM/GIL/native 원인 규명이 아니다.

실제 실행일에는 그 시점의 최종 PR 스택 HEAD를 다시 확인하고, 동일 revision에서 preflight를 재실행한 뒤
`scripts/assess_fid_read_ab.py`로 bounded analyzer + 사전등록 판정을 함께 보존한다.

## FID A-B-A 실행 당일 admission과 short-lived plan

`check_fid_read_ab_admission.py`는 exact HEAD·clean tree·32비트 OCX preflight·free space·
collector/Runtime 창·lease·09:15~15:15 KST·사용자 1회 승인을 로그인 전에 읽기 전용으로 확인해
`RUN_READY / RUN_BLOCKED / RUN_UNCERTAIN`을 낸다. 공식 거래일은 컨트롤타워가 확인한 날짜·근거를
attestation으로 넣고 도구가 추정하지 않는다.

READY 뒤에도 즉시 실행하지 않는다. `prepare_fid_read_ab_run.py`가 60초 이내 admission만 받아
create-only plan을 만들고 5분 TTL을 둔다. `verify_fid_read_ab_run_plan.py`는 trusted exact SHA와
fresh 실행 승인을 다시 요구하고 admission을 재실행한다. 그때도 READY면 fresh Python/worktree에서
exact collector 명령을 재계산해 plan과 일치할 때만 **수동 실행용 명령을 표시**한다.
prepare/verify는 git fetch, OCX 생성/로그인, collector launch, kill/restart, lock 삭제, raw scan을 하지 않는다.
READY/MANUAL_COMMAND_READY도 실제 수집 성공·서버 가용성·native 안정성을 인증하지 않는다.

## 2026-09-23 FID A-B-A 로컬 preflight — 사용자 전달 보고

일반 ChatGPT의 직접 관측이 아니라 사용자 전달 로컬 보고다.
전용 sibling worktree에서 당시 PR #24 HEAD `1bd0266…`를 clean 상태로 사용했고,
`C:\Projects\Stock\.venv32\Scripts\python.exe` CPython 3.10.11 x86,
OCX `C:\OpenAPI\KHOpenAPI.ocx` 등록/파일 존재, C: free 1,273,824,886,784 bytes,
collector/python/pythonw 및 Runtime/OpenAPI/Kiwoom 잔류 없음이 보고됐다.
`--preflight`는 `login_attempted=false`, `ocx_instantiated=false`, `ready=true`였고
실제 로그인·SetRealReg·raw session 생성 없이 `LOCAL_PREFLIGHT_READY`로 끝났다.
이는 서버 가용성·실시간 수신·native 안정성을 인증하지 않으며 **실행 당일 현재 HEAD에서 다시 확인**한다.

## 2026-09-23 장애 — 사용자 전달 로컬 보고

대상 `7a35b11eddff4dcea88c98acdc8b37df`, collection revision은 위 master다.
CPython 3.10.11 x86, live, telemetry ON / explicit teardown OFF였다.
작은 canary와 전종목 비교, 96,000 callback / 60초 x86 offline 시험은 이미 완료됐다.
작은 queue/offline 성공으로 실제 Qt/COM/GIL/native 병목을 배제하지 않는다.
메모리 peak 10:08:19.624 KST 약 1,767.7MiB, 이후 FID 시각 차이 약 1,700초 보고를
native allocation 실패의 최초 원인과 동일시하지 않는다. FID 시각 차이는 network latency가 아니다.

최초 popup은 미확정이다. 보존 근거는 first appearance ≤ 10:47:08.204 KST이며
last confirmed absent가 없다. WER 14:08 또는 dump 11:30을 최초 오류 시각으로 쓰지 않는다.
malloc NULL → CMemoryException, 요청 0x3e14=15,892 bytes, LAA OFF 복원은 로컬 보고다.
failure-time VA exhaustion/fragmentation과 최초 인과는 미확정이다.
저장/Qt finally/lease 해제 뒤 PID·창 잔류 보고가 있으므로 lease free만으로 exit를 판단하지 않는다.
이번 ChatGPT는 운영 raw/dump/PID/창을 직접 관측하지 않았다.

## 50.6GB 원본·sidecar와 미완료 연구

9월 21일 session `6f39117671c048f6b60477ceafbf40b6`, collection revision
`4821762fd93230b658339fee084d6c08e3e53ce9`, 원본 50,635,071,488바이트다.
최초 사용자 파일 SHA-256 주장은
`E4304FE3C1CAD8A85EC6C297CEB9CFDADCA2D20001E93756303D567EC5077569`,
payload SHA-256 주장은 `a988d3bf86e36f44a209480658f537088a8910768c68c9fec83349f3de7f1755`다.
다른 해시 대상을 혼동하거나 이번 감사가 재검증했다고 쓰지 않는다.
callbacks=42,796,226 / final_seq=42,836,791이며 차이 40,564는 parse_error 예상 단서이지 SQL 집계가 아니다.
제한 표본 unsigned FID15 5건/대응 parse_error는 품질 문제다. 임의 방향 보정은 없다.

실제 -wal 0바이트 / -shm 32,768바이트는 보존 중이다. 원본 writable 재연결,
sidecar cleanup, 전체 qualification은 대상 identity·동시 접근 배제·namespace/경로 격리·
free space·총 I/O/time 예산·실패 보존 설계와 별도 사용자 승인이 필요하다.
합성 clone lab 성공은 운영 파일 집합 처리 승인이나 전체 품질 인증이 아니다.
whole-file 무결성/품질 분포와 첫 실제 연구는 미완료이고 FIRST_RESEARCH_CANDIDATE 미승격이다.
9월 22일 12,940,107,776바이트 raw와 이 50.6GB 원본을 혼동하지 않는다.

## 계속 보존할 실패와 금지선

PR #22의 초기 focused/full harness 실패, CI #156의 flush 예외 close 누락,
CI #162의 raw startup 실패(근본 원인 미확정), PR #19 #205/#206 harness 실패와 최종 성공을
성공 이력으로 덮어쓰지 않는다. 세부 원문은 각 PR/Actions와 위 고정 HANDOFF에 보존한다.

운영 raw/dump/operations_state/Daily_baseline/old_data/사용자 변경은 보존한다.
자동 kill/restart/relogin, lock 삭제, Runtime 창 강제 종료, LAA 변경, queue 확대,
기본 FID 축소, 원본 시간 절단/방향 보정은 하지 않는다.
closed ≠ quality pass, 표본 clean ≠ whole-file clean, 무결성 ≠ 연구 적격성,
qualification ≠ 전략 검증, CI 성공 ≠ native/실시장 인증이다.
raw→LOB/feature 변환은 현 raw-v2 틱 연구의 필수 선행 단계가 아니다.
시장 실행 전 당일 공식 거래일·시장 구간과 RUNBOOK/market_sessions/현재 CLI를 다시 대조한다.
