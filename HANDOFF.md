# 현재 인계 — 2026-09-24 / PR #18 수집 의사결정 계약의 최신 master 통합 후보

[문서 인덱스](README.md) · [첫 시험 체크리스트](BACKTEST_TODO.md) ·
[파이프라인 지도](docs/PIPELINE_MAP.md) · [보존본 안내](docs/archive/README.md)

매 작업 시작 시 원격 master/열린 PR/최신 CI를 직접 확인한다. 아래 값은 이 통합 시작 기준이지 영구 최신값이 아니다.
이전 상세 인계 원문은 고정 SHA로 보존한다:
[#18 원래 인계](https://github.com/Peter-jackson12/Stock/blob/65a2feb333d81f3666ce41ee58d23e15199e0b69/HANDOFF.md),
[#29 병합 시점 인계](https://github.com/Peter-jackson12/Stock/blob/8d84c9be728be25a707063559459324a61557585/HANDOFF.md),
[#15 병합 시점 인계](https://github.com/Peter-jackson12/Stock/blob/5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70/HANDOFF.md).

## 원격 기준과 PR 상태

- master `8d84c9be728be25a707063559459324a61557585`: PR #29(FID 누적 후보) 병합 commit.
  병합 후 master CI #329(run 35938525443, job 107440979211)는 completed/success다.
  전체 1,826 passed / 6 deselected, FID 집중 259 passed / 8 subtests, 시작·종료 집중 71 passed는
  컨트롤타워의 로그 확인 보고다. 집중·전체·subtest를 합산하지 않고 deselected는 통과가 아니다.
- PR #24는 GitHub에서 merged로 처리됐다(#29를 통한 간접 병합). #25~#28은 #29로 이어진 개발 이력 보존용이다.
- PR #19(역사적 revision 비교, `9421afdccf9cfd1797cd7b5ac4e1f5d67ebe1127`)는 draft/open/unmerged다.
  PR CI #330은 completed/success다. #18에는 #19 변경·benchmark를 가져오지 않았다.
- **PR #18(이 branch)**: 원래 HEAD `65a2feb333d81f3666ce41ee58d23e15199e0b69`(수집 시각·범위 판단 계약,
  live 작업 경계, 문서 의존 회귀)에 최신 master를 정상 merge한 병합 직전 후보다. 실제 master 병합은
  컨트롤타워 검토 단계로 남긴다. 최종 HEAD/run/job은 PR #18의 checks와 완료 보고에서 확인한다.
- PR #21(독립 근거 UI)은 미통합이며 이번 작업에서 수정·병합·retarget하지 않는다.

## 두 계약의 역할

- [수집 의사결정 계약](docs/COLLECTION_RUNBOOK.md#collection-decision)·[구간별 coverage](docs/COLLECTION_RUNBOOK.md#collection-coverage)·
  [live 작업 경계](docs/COLLECTION_RUNBOOK.md#collection-live-boundary)는 일반 수집 판단의 기준이다.
  장전 보존 목표와 로그인·구독 완료 시점, 침묵 판정 창과 수집/보존 창, 기본 6자리와 명시적 `_NX` 경로를 구분한다.
- [Mock FID A-B-A 실행 계약](docs/COLLECTION_RUNBOOK.md#fid-aba-contract)과
  [실험 계약](tests/FID_READ_AB_DIAGNOSTIC.md)은 특정 진단 실험만 다룬다. 09:15<=KST<15:15는
  그 실험의 목표 창이며 프로젝트 전체 수집 시간이 아니다. `--fid-read-ab-test`는 기본 꺼짐·mock 전용·연구 비적격이다.
- 코드 시간표·`--preflight`·admission READY는 실제 거래일·수신·저장·native 안정성을 인증하지 않는다.
  종료 요청 / 저장 closed / PID·창 부재 / lease free는 서로 다른 사실이다.
- 모든 실행 예시는 별도 승인된 운영 단계용이며 문서·CI 작업에서 실행하지 않는다.

## FID 체인 보강의 남는 한계와 CI 실패 기록

#29는 admission/run-plan/analyzer/assessment를 fail-closed로 보강했다. 남는 한계:
verify 완료 뒤 사람이 실행하기까지는 검사하지 못한다(race-free 아님). admission digest는 서명이 아니고
`--execution-approved`는 사용자 인증이 아니다. collector 이름이 든 명령줄·kiwoom/키움/OpenAPI 제목 창은
보수적으로 BLOCKED, 권한 밖 Python은 UNCERTAIN이다. 인자까지 같은 무관한 부모는 launcher와 구별하지 못한다.
사전등록 규칙(최소 3개·중앙값 방향·효과크기 임계/p-value 없음)과 진단 raw의 연구 배제는 그대로다.
실제 32비트 런타임 회귀·운영 적용·Mock 실행은 하지 않았다.

보존하는 실패와 해석:
- #29 감사 CI #321: 원래 production에서 23 failed / 3 passed(parameter case 수, 독립 원인 수 아님).
- #324: 감사 PowerShell AST parser subprocess 10초 `TimeoutExpired`(감사 테스트 timeout만 30초로 조정).
- #327(run 35934021661, job 107426874350): `test_silence_stop_requests_shutdown_even_if_final_dump_fails[False]`가
  `raw v2 시작 실패` 뒤 `len(calls)==0`으로 실패. **최초 startup 원인은 미확정**이다. 로컬 재현 실패,
  운영 timeout 미변경. 시작 성공 전제와 원문 보존 메시지를 테스트에 추가했을 뿐이다.
- #328/#329/#330 성공은 위 실패의 원인 해결 근거가 아니다.
- CI #156 flush 예외 close 누락, #162 일회성 raw startup 실패(근본 원인 미확정), PR #19 #205/#206,
  PR #22 초기 harness 실패, PR #26 CI #280 HANDOFF 초과 실패를 보존한다.

## live·과거 장애 근거 — 현재 상태로 쓰지 않음

2026-09-23 사용자가 실행 중이라고 알린 세션 `7a35b11eddff4dcea88c98acdc8b37df`(collection revision
`5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70`, `--capture-telemetry` ON / `--explicit-ocx-teardown` OFF)은
사용자 보고다. 이후 전달된 메모리/dump, 저장/Qt finally/lease 해제 뒤 PID·창 잔류도 로컬 보고이며
직접 관측이 아니다. 현재 생존·종료를 이 문서로 확정하지 않는다. popup 최초 표시, VA exhaustion/fragmentation,
최초 장애 인과는 미확정이다. 완료된 작은 canary·96,000 callback x86 offline 시험은 반복하지 않는다.

2026-09-22 session `39b5af8b45024458be9a3ae2a2259685`(revision `6a6d6076649befc767e5d8d59151cbcfb2f27c34`):
과거 사본의 마지막 콜백 10:23:47, 보호 종료 요청 10:33:47, 저장 마무리 보고 10:33:48, accepted=committed=11,198,913,
writer_closed=true, data_quality=unverified. 16시경 Runtime Error 창(17:07 사용자 종료)과 수신 중단의
동일 원인은 미확정이다. 화면 대기큐는 Python 저장 큐이지 OCX/Qt 앞단 대기량이 아니다.
FID clock difference는 network latency가 아니고, 작은 processing_ns·queue 0·offline 성공으로
Qt/COM/GIL/native 병목을 배제하지 않는다.

## 2026-09-21 원본과 승인 경계

session `6f39117671c048f6b60477ceafbf40b6`, revision `4821762fd93230b658339fee084d6c08e3e53ce9`,
raw 50,635,071,488 bytes, 실제 -wal 0 / -shm 32,768 bytes는 보존한다. 사용자 파일 SHA-256 주장
`E4304FE3C1CAD8A85EC6C297CEB9CFDADCA2D20001E93756303D567EC5077569`와 payload SHA-256 주장
`a988d3bf86e36f44a209480658f537088a8910768c68c9fec83349f3de7f1755`는 대상이 다르며 재검증하지 않았다.
callbacks=42,796,226 / final_seq=42,836,791의 차 40,564는 parse_error 예상 단서일 뿐이다. unsigned FID15는
품질 문제이며 임의 방향 보정은 없다. sidecar 출처, whole-file stream integrity, 품질 이유·분포, 첫 실제 연구는
미완료이고 FIRST_RESEARCH_CANDIDATE 미승격이다. writable 재연결 in-place cleanup은 채택하지 않는다.
운영 파일 집합 복제·qualification에는 대상 identity·sidecar 출처·외부 reader/writer 차단·namespace 격리·
장외 시각·collector 부재·free space·I/O/time 예산·실패 보존 설계와 별도 사용자 승인이 필요하다.

closed != data quality pass; sample clean != whole-file clean; stream integrity != research eligibility;
parse_error != file corruption; qualification != strategy validation; backtest != live trading approval.
`Daily_baseline`·`old_data`·운영 raw·dump·operations_state·사용자 변경·오류 근거를 보존한다.
자동 kill/restart/relogin, lock 삭제, Runtime 창 닫기, LAA 변경, queue 확대, 기본 FID 축소는 금지한다.
raw→LOB/feature 변환을 현 raw-v2 틱 연구의 필수 선행 단계로 바꾸지 않는다.

## 다음 행동

1. PR #18 최종 HEAD의 자동 PR CI를 확인한다. 실패하면 최초 원문을 보존하고 필요한 부분만 고친다.
2. 컨트롤타워가 #18 diff·CI를 검토한 뒤 master 병합 여부를 정한다. #19 병합과 #21 UI 정리는 별도 작업이다.
3. NXT 전체 coverage, 장시간 프리/애프터 rollout, 시장별 침묵 정책 보정은 별도 후속이다.
4. 실제 Mock A-B-A·수집은 별도 승인 후 당일 공식 거래일/시장 구간·사용자가 승인한 exact SHA·현재 CLI를
   다시 대조하고 RUNBOOK 절차를 따른다. 문서/CI 성공은 로그인·배포·재시작 승인이 아니다.
