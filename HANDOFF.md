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

확인한 master는 `5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70`, 최신 master CI #195 success다.
기존 전체 기준은 1,565 passed / 6 deselected다. PR #20의 기존 CI #210
(run `35835764747`, job `107098720365`) 로그는 1,588 passed / 6 deselected였다.
PR #22 최종 전체 CI #234는 1,636 passed / 6 deselected, PR #23 최종 전체 CI #244는
1,638 passed / 6 deselected였다. PR #23 집중 audit #5는 133 passed였다.
통합 후보 자체의 정확한 HEAD와 master-base CI 결과는 현재 consolidation PR/Actions를 확인한다.
집중·전체·반복 실행은 합산하지 않으며 deselected는 통과가 아니다.

함께 열린 PR은 별개이며 자동 병합하지 않는다.

| PR | 역할 | 인계 기준 HEAD |
|---|---|---|
| #18 | 일반 ChatGPT 운영 판단/수집 시각 계약 | `65a2feb333d81f3666ce41ee58d23e15199e0b69` |
| #19 | 과거 revision 고정 합성 입력 비교 | `32e6285e2bd5a224a63152c2ed1f492b57afd841` |
| #20 | 전종목 Mock FID A-B-A 준비 | `c263003da6be82cefe740d6b22896babfcc89cbc` |
| #21 | 독립 근거 UI 표시·회귀 보강 | `4ce504dabca9baf37fd5c0a8dc062f484511c7ad` |
| #22 | FID 진단 연구 배제·예외 보존 독립 보강 | `f50bd55120d1a3f754fc135cd98c5e2eadf4438d` |
| #23 | A2 90초 이후 POST phase 분리 | `b5ae402aac0088d27382aa54ff29a619fc078185` |

PR #21이 미병합이므로 master 인계는 과거 상태다. 최신 장애 보고와 로컬 근거 경로는
[PR #21 고정 HANDOFF](https://github.com/Peter-jackson12/Stock/blob/4ce504dabca9baf37fd5c0a8dc062f484511c7ad/HANDOFF.md)에 있다.
이 감사에 #18 문서 변경이나 #21 UI 코드를 무단 통합하지 않았다. #21의 1,622/6 및
집중 114 성공은 그 PR의 결과이지 이번 감사의 테스트 수가 아니다. 그 검사를 재실행하지 않는다.

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

## 2026-09-23 FID A-B-A 로컬 preflight — 사용자 전달 보고

아래는 일반 ChatGPT가 원격 GitHub에서 직접 관측한 사실이 아니라 **사용자가 전달한 로컬 에이전트 보고**다.
preflight는 실제 로그인·Mock/Live 수집·SetRealReg·90초 실험 없이 수행됐다고 보고됐다.

- 기존 저장소: `C:\Projects\Stock` — branch `diag/fid-read-aba-20260923` / `c263003…`, clean.
- 전용 sibling worktree: `C:\Projects\Stock-preflight-fid-aba-20260923` — detached
  `1bd0266b70bf0126c01bca5b8c53234a00aa4753`, origin consolidation branch와 일치, clean.
- Python: `C:\Projects\Stock\.venv32\Scripts\python.exe`, CPython 3.10.11 x86 / 32-bit.
- preflight JSON: `login_attempted=false`, `ocx_instantiated=false`, `ready=true`,
  `ocx_registered=true`, `ocx_file_exists=true`, OCX path `C:\OpenAPI\KHOpenAPI.ocx`.
- C: free bytes 보고값: `1,273,824,886,784`.
- preflight 전후 `python.exe`/`pythonw.exe` 및 `kiwoom_universe_logger` 실행 없음,
  Runtime/OpenAPI/Kiwoom 관련 창 없음으로 보고됐다.
- 실제 raw session/operations_state 생성 없음. worktree에 import 부산물 `collector/**/__pycache__/*.cpython-310.pyc`만
  생겼고 gitignored 상태로 보고됐다.
- 최종 로컬 판정은 `LOCAL_PREFLIGHT_READY`다.

`ready=true`와 이 로컬 판정은 Mock 로그인 성공, 서버 가용성, 실시간 수신, 전종목 처리 안정성,
native 오류 부재를 인증하지 않는다. 실제 Mock 90초 1회는 실행 당일 공식 거래일/시장 구간과
원격 PR #24 HEAD, clean worktree, collector/PID/Runtime 잔류를 다시 확인한 뒤에만 진행한다.
첫 실제 실행 결과 검토 전에는 live 비교·동시 두 계정 비교·반복 전종목 실험으로 확대하지 않는다.

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

PR #22 최초 HEAD `a19cbd88cd8de85fd4d0fe77621abd6af487397e`의 집중 run `35853068603`은
129 passed / 2 failed였다. 새 테스트가 시작 전 legacy db_path를 실제 raw-v2 경로로 오인했고,
callback_error의 FID 위치를 event.details 대신 raw_fields로 읽었다. 실제 경로/스키마로 교정하고
원본 보존·미완료 manifest 검증은 강화했다. 전체 CI #230(run `35853068700`)은 사전 회귀에서
132 passed / 2 failed로 중단돼 전체 단계가 실행되지 않았다. 추가된 정책 hash에 따른 파일 수
기대값과 빠진 archive 링크를 수정했다. 이 실패를 운영 collector/native 장애나 전체 통과로 쓰지 않는다.

CI #156의 flush 예외 close 누락, #162의 raw startup 실패 이력을 지우지 않는다.
#162 근본 원인은 미확정이다. PR #19의 #205/#206 harness 실패와 최종 성공도 구분한다.
이전 인계 원문은
[검토 기준 HANDOFF](https://github.com/Peter-jackson12/Stock/blob/c263003da6be82cefe740d6b22896babfcc89cbc/HANDOFF.md)에 보존된다.

운영 raw/dump/operations_state/Daily_baseline/old_data/사용자 변경은 보존한다.
자동 kill/restart/relogin, LAA 변경, queue 확대, 기본 FID 축소, 원본 시간 절단/방향 보정은 하지 않는다.
closed ≠ quality pass, 표본 clean ≠ whole-file clean, 무결성 ≠ 연구 적격성,
qualification ≠ 전략 검증, CI 성공 ≠ native/실시장 인증이다.
raw→LOB/feature 변환은 현 raw-v2 틱 연구의 필수 선행 단계가 아니다.
시장 실행이 별도 승인되면 당일 공식 거래일·시장 구간과 RUNBOOK/market_sessions/현재 CLI를 다시 대조한다.
