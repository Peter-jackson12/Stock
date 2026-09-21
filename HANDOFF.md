# 현재 인계 — 2026-09-21 / 정상 종료 보고, 연구 입력 차단·검사 전용 진단 준비

현재 목표·차단 조건·다음 행동만 유지한다. [문서 인덱스](README.md),
[첫 시험 체크리스트](BACKTEST_TODO.md), [파이프라인 지도](docs/PIPELINE_MAP.md)를 따른다.
과거 기록은 [보존본 안내](docs/archive/README.md)와 Git 이력에 보존한다.
시작 전 인계 원문은 `4821762fd93230b658339fee084d6c08e3e53ce9:HANDOFF.md`에 남아 있다.

## 현재 판정과 근거 수준

2026-09-21 신규 raw-v2의 **종료 증거는 일관되지만 제한 표본에서 품질 문제가 확인됐다.**
`FIRST_RESEARCH_CANDIDATE`로 승격하지 않으며, 현행 엄격 연구 경로에서 차단한다.
원본을 폐기하지 않는다. whole-file 무결성·전체 품질 분포·연구 입력 합격·첫 백테스트는 미완료다.

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

현 검사기/reader의 `mode=ro` + `query_only`는 SQL 쓰기 금지이지 파일시스템 무변경 보장이 아니다.
SQLite 공식 [WAL 읽기 전용 설명](https://www.sqlite.org/wal.html#read_only_databases)과 부합한다.
[immutable 계약](https://www.sqlite.org/uri.html)은 잠금·변경 감지를 생략하므로 상수처럼 무조건 붙이지 않는다.
불변성/잔여 WAL 처리 보장 없이 `immutable=1`로 우회하거나 현재 sidecar를 임의 삭제하지 않는다.

## 바로 다음 작업과 역할

**추가 수집이나 백테스트가 아니라 검사 기반 정비가 우선이다.** 경로 A의 이번 수집은 실행됐지만 연구 합격은 실패했다.
반복 수집만으로 해결된다고 가정하지 않는다. 예비 경로 B의 파생·시간 절단·품질 예외 허용은 아직 승인/구현하지 않았다.

일반 ChatGPT/GitHub에서 먼저 진행할 작업:
1. 원본에 sidecar를 만들지 않는 닫힌 파일 검사 경로를 설계·합성 검증한다.
   기존 파일 보호 코드를 재사용하고 쓰기 중 파일·잔여 sidecar·경합에서는 안전하게 거부한다.
2. 기존 raw reader/품질 계약을 재사용하는 **검사 전용 품질 분포 진단**을 준비한다.
   전략/주문/체결을 호출하지 않고 구조·순번·payload checksum과 품질 적합성을 별도 결과로 기록한다.
   품질 오류는 합격으로 바꾸지 않되 구조가 유효하면 집계하고 계속 읽는 진단 목적을 구분한다.
   제어 기록 유형, 고유 문제 tick, 문제 사유, 종목/수신시각/FID20별 분포와 처음/마지막 seq를 대조한다.
   중단/예산 초과/구조 오류는 전체 완료로 보고하지 않는다. 상세 계약은 연구 런북에 둔다.
3. 관련 합성 회귀/CI로 변경을 검토한다. 직접 연구 CLI를 검사 도구로 쓰지 않는다.

Windows/로컬에서만 필요한 작업:
- 기존 로컬 근거와 전달 보고를 새 evidence 경로에 출처를 구분해 보존한다. 근거 확보를 이유로 raw를 재조회하지 않는다.
- 준비된 파일 보호/검사 도구의 Windows 파일 잠금·sidecar 회귀를 작은 합성 데이터로 검증한다.
- 이후에만 실제 50.6GB 진단의 장외 시각·I/O·메모리·시간 예산·중단 기준·새 출력 경로를 별도로 확정한다.
  현재 sidecar 해결 방식이 먼저 검증돼야 한다. 이 인계는 전체 스캔 실행 승인이 아니다.

## 계속 지킬 경계

정상 종료 ≠ 전체 무결성 ≠ 품질 합격 ≠ 전략 수익성 ≠ 실거래 승인이다.
표본의 B 가설을 공식 체결 유형으로 바꾸지 않고, venue=unknown을 KRX/NXT로 인증하지 않는다.
원본 전체 해시 재계산·COUNT·전체 스캔·실제 재생·압축·복원·추가 OCX 로그인은 이번 원격 정리에서 하지 않았다.
`Daily_baseline`·`old_data`·운영 raw·operations_state·사용자 변경을 보존한다.
