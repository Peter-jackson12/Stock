# 현재 인계 — 2026-09-22 / execution 수정 병합, 격리 복제 합성 PR, 운영 미승인

[문서 인덱스](README.md) · [첫 시험 체크리스트](BACKTEST_TODO.md) ·
[파이프라인 지도](docs/PIPELINE_MAP.md) · [보존본 안내](docs/archive/README.md)

이 파일은 현재 목표·차단 조건·다음 행동만 기록한다. 긴 원시 보고와 이전 인계는
`4c677bf9ed535f4b9ba529af81b0c00f3eb8af97:HANDOFF.md`에 그대로 보존한다.
수집 시작 전 원문은 `4821762fd93230b658339fee084d6c08e3e53ce9:HANDOFF.md`다.
매 작업 시작 시 원격 master와 진행 중 PR을 다시 확인한다. 아래 SHA를 최신값으로 가정하지 않는다.

## 원격 execution 수정 — 병합 완료

이번 컨트롤타워 시작 시 master는 `4c677bf9ed535f4b9ba529af81b0c00f3eb8af97`다.
PR #12 NUM-1은 `fc6378cc03b59ed26d94ca54d760a9853e71bdab`, PR #14 RUN-1은 위 master로 병합됐다.
PR #13은 공통 문서/CI 충돌로 superseded되어 closed / not merged다. PR #14가 실제 RUN-1 반영이다.
이전 인계의 “PR #12/#13 독립 수정 대기”와 상대 결함 xfail은 현재 master 상태가 아니다.

[master CI #88](https://github.com/Peter-jackson12/Stock/actions/runs/35686874418/job/106615471537)의
decoded 로그에서 **1,418 passed / 6 deselected / 0 xfailed**를 직접 확인했다.
6 deselected는 통과 수에 넣지 않는다. 기본 oracle와 수치 행렬은 합계
**9,450 조합 / 62,886 checkpoint**다. 이후 PR의 검증 건수와 이 기준선을 혼합하지 않는다.

- NUM-1: exact finite-decimal 정수 계수로 affordability/gross/fee/cash를 계산하고
  state 변경 전에 cost/solvency/ledger envelope를 검증한다. cash 사후 clamp가 아니다.
- RUN-1: 예약 top-level raw_manifest 구조와 선택적 비음수 real int event_count를
  출력 폴더 생성 전에 검증한다. generic provenance는 opaque하며 런타임 failed/진단 계약은 유지한다.
- RES-1 one-pass retry와 CLK-1 advance 호출열 정책은 유지한다. 새로운 behavior-change 승인이 아니다.

상세 실행 계약·독립 oracle 범위는 [실행 감사 명세](tests/EXECUTION_ORACLE_SPEC.md)와
[Simulation Reality Contract](TICK_RESEARCH_RUNBOOK.md)를 따른다.
합성 CI는 실제 전략 수익성·운영 raw 적합성을 인증하지 않는다.

## 진행 중 합성 복제 — PR #15

[PR #15](https://github.com/Peter-jackson12/Stock/pull/15)의
`scripts/lab_raw_v2_clone.py`와 `tests/test_raw_v2_clone_lab.py`가 담당한다.
실험 범위·관측과 남은 한계는 [복제 lab 명세](tests/RAW_V2_CLONE_LAB_SPEC.md)에 둔다.
최종 HEAD/run/job의 decoded CI 로그를 PR에서 확인한다. 열린 PR을 master 구현으로 읽지 않는다.

원본 main/WAL/SHM을 모두 보호한 handle에서 원시 evidence와 working 복제본을 분리해 만든다.
원본은 SQLite로 재연결하지 않고 working 복제본에서만 SQLite-managed 처리를 한다.
기존 qualification은 수정하지 않으며 clean/unsigned 품질, bytes/manifest/seq/payload를 비교한다.

이것은 [복제본 계획](TICK_RESEARCH_RUNBOOK.md)의 부분 구현이다.
전체 운영 경로의 구현·검증 완료가 아니다. 기존 이름의 rename/delete 배제와
새 journal 등 자식 이름 생성의 보편적 배제는 다르다. 생성 탐지만으로 연속 배제를 인증하지 않는다.
속성 전용 directory handle의 빈 폴더 rename 공백도 발견해 FILE_LIST_DIRECTORY와 별도 회귀로 다룬다.
운영 입력 CLI·실 raw 복사·in-place cleanup·후속 qualification 자동 실행은 추가하지 않는다.

## 현재 live — 사용자 보고, 직접 로컬 관측 아님

2026-09-22 session `39b5af8b45024458be9a3ae2a2259685`가 사용자 Windows PC에서 수집 중이라는 보고다.
오늘 raw 경로는 `sampledata/raw_ticks_v2/20260922/39b5af8b45024458be9a3ae2a2259685.db`다.
07:20경 로그인/구독, 후보 3,756개. 09:11:12 보고값은 체결 748,442 / 호가 1,430,768 / 대기큐 8이다.
이 계수로 현재 상태·현재 PID·현재 정상 종료를 추론하지 않는다.

종료를 별도로 확인하기 전에는 오늘 raw 조회/SQLite open/sidecar/qualification/백테스트,
추가 OCX 로그인, 수집기 재시작, 사용자 로컬 checkout·환경 변경을 하지 않는다.
GitHub-hosted의 작은 합성 fixture만 사용한다. 사용자 working tree/process를 확인한 것으로 말하지 않는다.

## 2026-09-21 대상과 종료 근거 — 로컬 과거 보고

- session_id: `6f39117671c048f6b60477ceafbf40b6`
- collection revision: `4821762fd93230b658339fee084d6c08e3e53ce9`
- raw: `sampledata/raw_ticks_v2/20260921/6f39117671c048f6b60477ceafbf40b6.db`
- size: `50,635,071,488` bytes

종료 closed / accepting=false / writer_closed=true / error=null이 보고됐다.
accepted=committed callbacks `42,796,226`; final_seq `42,836,791`이다.
dropped/pending/queued/in-flight/write failures=0이라는 일관된 종료 근거다.
이를 공급자 무누락이나 품질 합격 증거로 읽지 않는다.

최초 DB 파일 바이트 SHA-256 보고:
`E4304FE3C1CAD8A85EC6C297CEB9CFDADCA2D20001E93756303D567EC5077569`

status/journal/manifest의 payload 스트림 SHA-256 주장:
`a988d3bf86e36f44a209480658f537088a8910768c68c9fec83349f3de7f1755`

두 해시는 대상이 다르다. payload hash는 저장 payload UTF-8 바이트와 개행의 누적값이다.
본체 재해시·전체 iterator 소진을 이번 원격 작업에서 수행하지 않았다.
추가 근거 위치·시각·로그는 위 4c677bf revision의 HANDOFF에 보존돼 있다.

## 제한 품질 조사와 계수 단서

처음/중간/말미 각 100 records, 총 300 records에서 정규화 mismatch 0이라는 보고다.
말미 무부호 FID15 체결 5건과 대응 parse_error 5건을 확인했다. is_buy=null /
trade_direction_unverified를 유지한다. 이미 본 표본을 이유 없이 다시 읽지 않는다.
세부 조사 기록은 [후보 조사](BACKTEST_TODO.md)에 둔다.

말미 표본으로 장중 부재·경계 집중·전체 원인을 추론하지 않는다. 무부호 FID15 예외/대체 방향의
OpenAPI 문서 근거는 확인되지 않았다. signed 규칙 외 값을 임의 매수/매도로 보정하지 않는다.

`42,836,791 - 42,796,226 - 1 = 40,564`는 코드/계수 기반 parse_error 예상 단서다.
SQL COUNT나 무부호 trade 40,564건의 확인이 아니다. whole-file에서 실제 reason count와 대조한다.

## 실제 sidecar와 운영 차단

실제 원본에는 WAL 0 bytes, SHM 32,768 bytes가 표본 reader 실행 뒤 생겼다는 보고다.
rollback journal은 관측되지 않았다. 삭제/checkpoint/VACUUM/교체하지 않았다.
과거 Windows/NTFS lab은 mode=ro residue 생성/잔존, 단순 connect-close 미정리,
복제본 metadata page read + close 정리, committed WAL의 main-only 복사 손실,
열린 WAL handle의 부분 정리, 원본 잠금 해제 후 late writer와 identity 교체 반례를 재현했다.
상세는 [sidecar 실험](TICK_RESEARCH_RUNBOOK.md)을 따른다.
로컬 TEMP 결과를 이번 ChatGPT가 직접 읽은 것이 아니다.

**원본 lock release → writable SQLite reconnect의 in-place 정리는 채택하지 않는다.**
0-byte WAL / 32 KiB SHM만으로 폐기·정리를 승인하지 않는다. qualification은 모든 sidecar를 계속 거부한다.
CollectorLease/PID 부재/파일 크기 일치는 비협조적 접근이나 전체 이름 공간 격리의 증거가 아니다.

## 바로 다음 행동

1. PR #15의 최종 합성 CI·실패 보존·명세를 검토한다. 미검증 조건을 성공으로 채우지 않는다.
   일반 운영 namespace 격리와 대상별 안전한 acquisition은 후속 설계다. 운영 원본 재연결로 우회하지 않는다.
2. 실제 적용 전 대상 identity, main/WAL/SHM 출처, 외부 reader/writer 차단, 경로 보호,
   장외 시각, 동시 collector 부재, free space, 총 I/O/time 예산, failure preservation과
   **별도 사용자 승인**을 확인한다. 이번 인계는 원본 조회/해시/복사/삭제 승인이 아니다.
3. 그 뒤에만 격리 복제본으로 50.6GB whole-file qualification을 수행한다.
   stream integrity, actual parse_error total/reason counts, trade_direction_unverified,
   종목/시간 분포, 예상 40,564, manifest/session/seq/checksum과 whole iterator exhaustion을 따로 확인한다.
   integrity=true / research_eligible=false는 가능한 정상 진단 결과다.
4. 결과 전에 raw discard, 시간 절단, 방향 자동 보정, 파생 데이터·FIRST_RESEARCH_CANDIDATE 승격을 결정하지 않는다.

정상 종료 ≠ 전체 무결성 ≠ 품질 합격 ≠ 전략 검증 ≠ 실거래 승인이다.
운영 raw·Daily_baseline·old_data·operations_state·사용자 변경·오류 근거를 보존한다.
