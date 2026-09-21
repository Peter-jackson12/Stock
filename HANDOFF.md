# 현재 인계 — 2026-09-21 / sidecar 합성 검증 완료, 운영 적용 미승인

현재 목표·차단 조건·다음 행동만 유지한다. [문서 인덱스](README.md),
[첫 시험 체크리스트](BACKTEST_TODO.md), [파이프라인 지도](docs/PIPELINE_MAP.md)를 따른다.
과거 기록은 [보존본 안내](docs/archive/README.md)와 Git 이력에 보존한다.
수집 시작 전 인계 원문은 `4821762fd93230b658339fee084d6c08e3e53ce9:HANDOFF.md`에 남아 있다.

## 현재 판정과 근거 수준

2026-09-21 신규 raw-v2는 **종료 증거가 일관되지만 제한 표본에서 품질 문제가 확인됐다.**
`FIRST_RESEARCH_CANDIDATE`로 승격하지 않는다. 원본을 폐기하지 않는다.
실제 whole-file 무결성·전체 품질 분포·연구 입력 합격·첫 백테스트는 모두 미완료다.

전략 없는 qualification 구현과 추가 보호 경계는 PR #8의 변경이다. 후속
[잔여 sidecar 합성 검증](TICK_RESEARCH_RUNBOOK.md#잔여-sidecar의-합성-검증-결과)은 작은 Windows
fixture에서 완료했다. 실제 50.6GB DB는 열지 않았다. 현재 도구는 기존 sidecar가 하나라도 있으면
계속 안전하게 거부하며, 실제 운영 적용은 미승인이다.

생산 데이터 사실은 사용자가 전달한 PowerShell 출력·로컬 제한 검사 보고다. ChatGPT가 로컬 raw·
저널·프로세스를 직접 검사한 결과가 아니다. 로컬 working tree와 프로세스 상태는 다음 로컬 작업에서
재확인한다. 별도 표본/원문 조사 파일을 생성하지 않았다는 보고이므로 채팅 보고를 원시 파일로 가장하지 않는다.
이번 GitHub 검토·수정·CI 확인은 아래 원격 근거와 구분한다.

## 원격 검토와 검증

- PR #7의 수집 종료·품질 차단 문서를 검토하고 정상 merge했다.
  merge commit: `fe5d96c1fb2c76d86d608b5d8cf598c0441d4c91`.
- qualification 최초 구현은 `a563e26`, 인계 시 branch HEAD는 `f75d799`였다.
  `codex/raw-v2-qualification`은 PR #7 head `bb59d029`를 조상으로 보존한다. 강제 푸시·이력 재작성은 하지 않았다.
- PR #8 검토 중 두 경계를 수정했다: 입력 resolve로 junction/reparse가 숨는 문제,
  사유 누락/null/빈 목록의 parse_error가 연구 합격으로 빠지는 문제.
  반례 추가 `3223af0`, 수정 `04fa5cc`. 기존 reader/전략 정책을 완화하지 않았다.
- 새 [경계 회귀](tests/test_raw_v2_qualification_boundaries.py) 14건을 추가했다.
  reasonless parse_error, junction 상위 경로, parent traversal, 0-byte sidecar 보존, CLI exit 3을 포함한다.
- 코드 `04fa5ccf734917422d32f5efc67aecf5c1247ee0`와 당시 master의 PR 병합 트리에 대해
  Actions `35580010889`, job `106270479685` 로그를 직접 확인했다.
  Windows / Python 3.14.7: **1,249 passed, 6 deselected**. 기존 qualification 14건과 신규 경계 14건 모두 통과했다.
  이는 GitHub 합성 실행이며 사용자의 Windows PC에서 새로 실행한 결과가 아니다.
- 이전 로컬 보고: qualification 14 passed, 관련 raw/archive/docs 84 passed, 전체 1,235 passed/6 deselected.
  실제 raw를 검사했다는 뜻이 아니다. 이후 문서 변경의 최신 HEAD·PR·CI는 원격에서 다시 확인한다.
- 이번 sidecar 합성 변경의 로컬 결과: 요청된 qualification/boundary/sidecar/archive/raw-v2/docs 묶음
  **103 passed**, 전체 Git-only **1,254 passed, 6 deselected**. `local_data`/`local_env`와 운영 raw 검사는
  실행하지 않았다. 합성 통과는 아래 운영 격리 조건을 인증하지 않는다.

## 대상과 종료 증거 — 로컬 보고

- session_id: `6f39117671c048f6b60477ceafbf40b6`
- 수집 code_revision: `4821762fd93230b658339fee084d6c08e3e53ce9`
- raw: `sampledata/raw_ticks_v2/20260921/6f39117671c048f6b60477ceafbf40b6.db`
- evidence: `operations_state/capture_sessions/6f39117671c048f6b60477ceafbf40b6/`
- source/server/feed_scope: `kiwoom` / `live` / `kiwoom_universe_venue_unverified`
- venue: `unknown`; 가격/방향 정책: `signed_magnitude` / `signed_volume`
- 종료 2026-09-21 15:35 KST; 첫 이벤트 08:30:03, 마지막 수신 15:32:41.
- 동일 identity/PID 7784/경로/revision에서 status·peer journal `starting → draining → closed`.
- `accepting=false`, `writer_closed=true`, `error=null`, finalization 존재.
- accepted=committed=`42,796,226`; dropped/pending/queued/in-flight/write failures=0.
- trade callbacks=`13,902,198`, quote callbacks=`28,894,028`.
- committed_seq=final_seq=`42,836,791`; 종료 로그·drain·finalization 일치, 관련 collector 부재 보고.
- raw 크기=`50,635,071,488` bytes; 생성 07:31:10 KST,
  정밀 LastWriteTime=`2026-09-21T15:35:00.9280174+09:00`.

사용자가 최초 계산한 DB 파일 바이트 SHA-256:
`E4304FE3C1CAD8A85EC6C297CEB9CFDADCA2D20001E93756303D567EC5077569`

status/journal/manifest가 주장하는 payload 스트림 SHA-256:
`a988d3bf86e36f44a209480658f537088a8910768c68c9fec83349f3de7f1755`

두 해시는 대상이 다르다. payload 해시는 저장 payload UTF-8 바이트와 개행의 누적값이다.
파일 해시나 세 근거의 주장 일치는 전체 reader 소진을 대신하지 않는다. 추가 제한 조사에서 본체를
재해시하지 않았고 크기/mtime 유지도 새 바이트 동일성 인증은 아니다. 콜백 계수 일치는 공급자 무누락 증거가 아니다.

기존 근거 위치: evidence root의 `status.json`, `operations_state/peer_reports.sqlite3`,
`logs/kiwoom_universe_20260921.log`, `operations_state/preflight_20260921/report.md`.
`logs/collector_fault_20260920T223057019658Z_7784.log`는 0 bytes라는 보고다.
대상 세션 이전 두 로그인 실패와 혼동하지 않는다.

## 제한 조사와 계수 단서

최초/중간/말미 각각 100 records, 총 300 records/294 ticks의 정규화 mismatch는 0건이었다.
말미 무부호 FID15 체결 5건과 대응 parse_error 5건으로 품질 실패다.
정확한 seq·종목·FID20은 [현재 후보 조사](BACKTEST_TODO.md#2026-09-21-신규-세션의-제한-조사)에 둔다.
이미 조사한 표본/5건을 이유 없이 다시 읽지 않는다.

5건의 FID20은 15:32:10–15:32:39이며 153000이 없다. “15:30 체결의 2분 지연 수신”을 지지하지 않는다.
말미 선택 표본이므로 장중 부재·경계 집중·전체 원인을 추론하지 않는다. 체결 유형/원인은 분류 미확정이다.
FID20은 초 정밀도이며 표시값과 수신 시각의 차이를 순수 네트워크 지연으로 해석하지 않는다.
로컬 `C:/OpenAPI/koa_devguide.xml` 조사 보고에 무부호 FID15의 예외/대체 방향 근거가 없다.

`final_seq - committed_callbacks - 1 = 40,564`는 코드·계수 기반 parse_error 예상값이다.
**SQL 집계도, 무부호 체결 40,564건의 확인도 아니다.** whole-file에서 실제 control/reason별 count와 대조한다.
전체 문제를 말미 5건으로 축소하거나 15:30 절단으로 합격한다고 가정하지 않는다.

## SQLite sidecar — 합성 일부 미검증, 운영 미승인

최초 표본 조회 전 DB 본체만 있었고 조회 시각부터 아래 파일이 관측됐다는 보고다.
- `.db-wal`: 0 bytes, 생성/수정 `2026-09-21T16:08:26.3320855+09:00`.
- `.db-shm`: 32,768 bytes, 생성은 위와 같고 최종 수정 `2026-09-21T16:23:29.9155482+09:00`.
  마지막 조회 전후 SHA-256은 `FD4C9FDA9CD3F9AE7C962B0DDF37232294D55580E1AA165AA06129B8549389EB`로 동일하다는 보고다.
- rollback journal은 관측되지 않았다. 삭제·checkpoint·VACUUM·복사본 교체는 하지 않았다.

일반 reader의 mode=ro/query_only는 SQL 쓰기 금지이지 sidecar 비생성 보장이 아니다.
qualification은 별도 sealed reader이며 reparse 입력/상위 경로·sidecar·활성 write/delete handle을 거부한다.
단, 파일 공유 잠금은 상위 디렉터리 전체의 이름 공간 잠금이 아니다. 외부 접근·경로 변경을 배제하지
못하면 실행하지 않는다. immutable을 무조건 붙여 우회하지 않는다.

전용 lab을 Windows 11/64-bit Python 3.14.7/SQLite 3.53.1/NTFS에서 실행했다. 최종 근거는
`%TEMP%/Stock_raw_sidecar_lab_d2799ff9fb0e4bf78ebf9b35f8753fdd/result.json`이다.
일반 mode=ro 실제 조회가 0-byte WAL/32 KiB SHM을 만들고 명시적 close·자식 exit 뒤에도 남기는 현상을
재현했다. 합성 복제본에서 쓰기 가능 connect/close만으로는 안 없어졌고, metadata 페이지 read 뒤
cursor/connection close에서는 main SHA-256·전체 2행·payload checksum을 유지한 채 없어져 qualification이
통과했다. 일반 reader 재조회는 sidecar를 다시 만들었다.

그러나 committed WAL의 표식 행은 main-only 복사에서 사라졌고, rollback journal/incomplete/출처 불명
sidecar/junction은 모두 거부 대상이다. 열린 WAL handle에서는 SHM만 없어지고 WAL은 남았다. sealed handle
유지 중 write는 막혔지만, handle 해제 직후 후발 writer가 먼저 진입해 commit하는 경합과 경로 identity
교체가 재현됐다. 처리 전체의 연속 배제 조건을 입증하지 못했으므로 **합성은 일부 미검증, 운영은 미승인**이다.
0-byte WAL/32 KiB SHM만으로 삭제를 승인하지 않는다. 상세 수치·허용 후보·거부 조건은 런북 한 곳에 둔다.

## 바로 다음 작업과 역할

1. 합성 결과·코드·잠금/내용 보존 반례를 이 ChatGPT 대화에서 검토한다. 현재는 차단을 유지한다.
2. 실제 처리를 검토하려면 비협조적 후발 writer와 경로 교체까지 처리 전체에서 막는 운영 격리 조건,
   대상 identity/sidecar 출처, 실패 시 원본 보존·복구 절차를 별도로 설계하고 승인받는다.
3. 승인 전 운영 raw/evidence를 조회·해시·복사하거나 sidecar를 정리하지 않는다. 이번 인계는 삭제 승인이 아니다.
4. 승인된 sidecar 해결 뒤에만 50.6GB qualification의 장외 시각·I/O/시간·중단 기준·공간·새 출력 경로와
   동시 collector 부재를 확정한다. 실제 검사 실행도 이번 작업 범위가 아니다.
5. whole-file 결과에서 stream integrity·reason별 count·종목/시간 분포·예상 40,564를 대조한 뒤
   다음 연구 입력 정책을 결정한다. 결과 전에 후보 승격·재수집·파생 경로·시간 절단을 자동 승인하지 않는다.

정상 종료 ≠ 전체 무결성 ≠ 품질 합격 ≠ 전략 수익성 ≠ 실거래 승인이다.
venue=unknown을 KRX/NXT로 인증하지 않는다. FID15 무부호 값을 임의 매수/매도로 바꾸지 않는다.
`Daily_baseline`·`old_data`·운영 raw·operations_state·사용자 변경과 오류 근거를 보존한다.
