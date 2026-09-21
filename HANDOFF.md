# 현재 인계 — 2026-09-21 / qualification 구현·합성 검증 완료, 실제 검사 차단

현재 목표·차단 조건·다음 행동만 유지한다. [문서 인덱스](README.md),
[첫 시험 체크리스트](BACKTEST_TODO.md), [파이프라인 지도](docs/PIPELINE_MAP.md)를 따른다.
과거 기록은 [보존본 안내](docs/archive/README.md)와 Git 이력에 보존한다.
시작 전 인계 원문은 `4821762fd93230b658339fee084d6c08e3e53ce9:HANDOFF.md`에 남아 있다.

## 현재 판정과 근거 수준

2026-09-21 신규 raw-v2의 **종료 증거는 일관되지만 제한 표본에서 품질 문제가 확인됐다.**
`FIRST_RESEARCH_CANDIDATE`로 승격하지 않으며, 현행 엄격 연구 경로에서 차단한다.
원본을 폐기하지 않는다. whole-file 무결성·전체 품질 분포·연구 입력 합격·첫 백테스트는 미완료다.

전략을 실행하지 않는 원본 비변경 whole-file qualification 경로는 작업 브랜치
`codex/raw-v2-qualification`의 `a563e26`에 구현됐다. 이는 합성 데이터 검증 완료이지
아래 실제 50.6GB raw의 검사 완료가 아니다. 대상의 기존 sidecar 때문에 현재 도구는 안전하게 거부한다.

이 문서는 사용자가 전달한 PowerShell 출력과 두 차례 로컬 제한 검사 보고를,
원격 코드 `4821762fd93230b658339fee084d6c08e3e53ce9`와 대조한 인계다.
ChatGPT가 로컬 raw·저널·프로세스를 직접 검사한 결과가 아니다.
로컬 HEAD/origin/master 일치·working tree clean은 보고 당시 관측이며 다음 작업에서 재확인한다.
별도 표본/원문 조사 결과 파일은 생성하지 않았다는 보고다. 채팅 보고를 원시 증거 파일로 가장하지 않는다.

## 대상과 종료 증거

- session_id: `6f39117671c048f6b60477ceafbf40b6`
- 수집 code_revision: `4821762fd93230b658339fee084d6c08e3e53ce9`
- raw: `sampledata/raw_ticks_v2/20260921/6f39117671c048f6b60477ceafbf40b6.db`
- evidence: `operations_state/capture_sessions/6f39117671c048f6b60477ceafbf40b6/`
- source/server/feed_scope: `kiwoom` / `live` / `kiwoom_universe_venue_unverified`
- venue: `unknown`、가격/방향 정책: `signed_magnitude` / `signed_volume`
- 종료: 2026-09-21 15:35 KST. 첫 이벤트 08:30:03, 마지막 수신 15:32:41.
- status·peer journal: `starting → draining → closed`, 동일 identity/PID 7784/경로/revision.
- `accepting=false`, `writer_closed=true`, `error=null`, finalization 존재.
- accepted=committed=`42,796,226`; dropped/pending/queued/in-flight/write failures=0.
- trade callbacks=`13,902,198`, quote callbacks=`28,894,028`.
- committed_seq=final_seq=`42,836,791`.
- 종료 로그·writer drain·저널/manifest finalization 일치, 대상/관련 collector 프로세스 부재 보고.
  PID 7784 fault 로그는 0 bytes. 대상 세션 이전 두 로그인 실패와 섞지 않는다.
- raw 본체 크기=`50,635,071,488` bytes, 생성 07:31:10 KST,
  정밀 LastWriteTime=`2026-09-21T15:35:00.9280174+09:00`.

사용자가 최초 계산한 DB 파일 바이트 SHA-256:
`E4304FE3C1CAD8A85EC6C297CEB9CFDADCA2D20001E93756303D567EC5077569`

status/journal/manifest가 주장하는 raw payload 스트림 SHA-256:
`a988d3bf86e36f44a209480658f537088a8910768c68c9fec83349f3de7f1755`

두 해시는 대상이 다르다. `RawV2Writer`의 payload 해시는 각 저장 payload UTF-8 바이트와 개행의 누적 해시다.
파일 바이트 해시 확보나 세 근거의 주장 일치는 reader를 끝까지 소진한 payload 검증을 대신하지 않는다.
추가 제한 조사에서 DB 본체를 재해시하지 않았다. 크기/mtime 유지도 바이트 동일성의 새 인증이 아니다.
콜백 계수 일치는 해당 저장 파이프라인의 대조이지 공급자부터의 무누락 증거가 아니다.

로컬 근거 위치:
- evidence root의 `status.json`, `operations_state/peer_reports.sqlite3`
- `logs/kiwoom_universe_20260921.log`
- `logs/collector_fault_20260920T223057019658Z_7784.log`
- 시작 전 점검: `operations_state/preflight_20260921/report.md`

## 제한 조사로 확인된 것과 미확인인 것

최초/중간/말미 각각 100 records를 조사했다. 총 300 records/294 ticks에서 정규화 mismatch는 0건이다.
말미에 무부호 FID15 체결 5건과 대응 `parse_error` 5건이 있어 표본 품질 검사는 실패했다.
정확한 seq·종목·FID20은 [현재 후보 조사](BACKTEST_TODO.md#2026-09-21-신규-세션의-제한-조사)에 둔다.
이미 조사한 표본/5건을 이유 없이 다시 읽지 않는다.

5건의 FID20은 `15:32:10–15:32:39`이고 `153000`은 없다.
따라서 “15:30 체결을 2분 늦게 수신” 가설은 이 표본으로 지지되지 않는다.
말미를 골라 조사했으므로 장중에는 없거나 경계에만 집중된다는 분포 결론을 내리지 않는다.
체결 유형/원인은 **분류 불가**, 시장 경계 가설은 미확정이다.
FID20은 초 정밀도이며 날짜/시계 동기화를 인증하지 않는다. 표시값과 수신 시각의 약 2.78초 차이는 순수 네트워크 지연 측정이 아니다.
로컬 `C:/OpenAPI/koa_devguide.xml` 조사 보고에는 무부호 FID15 예외/매수·매도 대체 근거가 없다.

`final_seq - committed_callbacks - 1 = 40,564`라는 코드·계수 기반 품질 기록 예상값이 있다.
**DB 집계 결과도, 무부호 체결 40,564건의 확인도 아니다.** 계수 해석과 한계는 체크리스트에 둔다.
이 때문에 “전체 문제는 말미 5건뿐” 또는 “15:30에서 잘라내면 합격”이라고 가정하지 않는다.

## SQLite sidecar — 보존, 아직 해결하지 않음

로컬 보고상 최초 표본 조회 전에는 DB 본체만 있었고 조회 시각부터 다음 파일이 관측됐다.
- `.db-wal`: 0 bytes, 생성/수정 `2026-09-21T16:08:26.3320855+09:00`.
- `.db-shm`: 32,768 bytes, 생성은 위와 같고 최종 수정 `2026-09-21T16:23:29.9155482+09:00`.
  마지막 조회 전후 SHA-256은 `FD4C9FDA9CD3F9AE7C962B0DDF37232294D55580E1AA165AA06129B8549389EB`로 동일하다는 보고다.
- rollback journal은 관측되지 않았다. 삭제·checkpoint·VACUUM·복사본 교체는 하지 않았다.

기존 표본 검사기/일반 reader의 `mode=ro` + `query_only`는 SQL 쓰기 금지이지 파일시스템 무변경 보장이 아니다.
SQLite 공식 [WAL 읽기 전용 설명](https://www.sqlite.org/wal.html#read_only_databases)과 부합한다.
[immutable 계약](https://www.sqlite.org/uri.html)은 잠금·변경 감지를 생략하므로 상수처럼 무조건 붙이지 않는다.
불변성/잔여 WAL 처리 보장 없이 `immutable=1`로 우회하거나 현재 sidecar를 임의 삭제하지 않는다.

새 [qualification 경로](scripts/qualify_raw_v2.py)는 Windows 로컬 NTFS·reparse 아님·sidecar 전무·
write/delete handle 배제를 모두 확인한 동안에만 `immutable=1`을 사용한다. scan 전후 파일 identity·
크기·mtime·sidecar 부재도 대조한다. 0-byte WAL을 포함해 sidecar가 하나라도 있거나 활성 쓰기 핸들이
있으면 읽지 않는다. 일반 reader의 동작은 유지하며 역할을 분리했다.

전략 없이 전체 reader를 소진하면서 구조 무결성과 품질 적합성을 별도 결과로 남긴다.
정상 `parse_error`는 reason·종목·수신 UTC 5분 bucket·고정 상한 예시로 집계하고 scan을 계속한다.
대응 tick issue와 parse_error는 raw record 수는 각각 유지하되 logical issue로 중복 계산하지 않는다.
sequence gap·manifest/session 불일치·checksum·I/O 오류는 `stream_integrity_verified=false`다.
Windows 합성 회귀 14건, 관련 raw/문서 회귀 84건, Git-only 전체 1,235건이 통과했고 6건은 선택 해제됐다.
일반 reader의 SHM 생성, qualification 비변경, non-empty WAL·sidecar·활성 쓰기 핸들 거부를 합성 확인했다.
실제 운영 raw는 조회·COUNT·hash·qualification·재생하지 않았다.

## 바로 다음 작업과 역할

1. 원격 `codex/raw-v2-qualification`에 푸시했고 GitHub Actions CI `35577653551`은 success다.
   PR #7의 수집 종료 인계 커밋을 보존한 위에 현재 변경을 쌓았다. GitHub 브라우저 인증이 없어
   새 PR 생성만 미완료이며, 인증 가능한 환경에서 이 브랜치로 PR을 만든다.
2. 현재 `.db-wal`/`.db-shm`을 삭제·checkpoint하지 않은 채 보존한다. main DB와 sidecar의 관계를
   안전하게 판정·해결할 별도 절차를 먼저 설계하고 작은 Windows 합성 회귀로 검증한다.
3. sidecar 해결 뒤에만 실제 50.6GB qualification의 장외 시각·I/O/시간 예산·중단 기준·여유 공간·
   새 출력 경로를 확정한다. 실행 전 외부 종료 근거와 expected session_id를 다시 대조한다.
4. whole-file 결과에서 예상 parse_error 40,564와 실제 reason별 집계를 구분해 대조한다.
   현재는 실제 건수 미확인이다. 연구 합격 전에는 백테스트를 실행하지 않는다.

추가 수집 반복, 경로 B 파생·시간 절단·품질 예외 허용은 아직 승인/구현하지 않았다.
이 인계는 실제 전체 스캔이나 현재 sidecar 삭제 승인도 아니다.

## 계속 지킬 경계

정상 종료 ≠ 전체 무결성 ≠ 품질 합격 ≠ 전략 수익성 ≠ 실거래 승인이다.
표본의 B 가설을 공식 체결 유형으로 바꾸지 않고, venue=unknown을 KRX/NXT로 인증하지 않는다.
원본 전체 해시 재계산·COUNT·전체 스캔·실제 재생·압축·복원·추가 OCX 로그인은 이번 원격 정리에서 하지 않았다.
`Daily_baseline`·`old_data`·운영 raw·operations_state·사용자 변경을 보존한다.
