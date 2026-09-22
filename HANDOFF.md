# 현재 인계 — 2026-09-22 / 종료·수신 보강과 격리 복제 합성 경로

[문서 인덱스](README.md) · [첫 시험 체크리스트](BACKTEST_TODO.md) ·
[파이프라인 지도](docs/PIPELINE_MAP.md) · [보존본 안내](docs/archive/README.md)

이전 상세 인계는 `4c677bf9ed535f4b9ba529af81b0c00f3eb8af97:HANDOFF.md`,
종료·수신 보강 병합 직후 인계는 `c409eb6aa1c4b6eee22d562391165ca7f72f3020:HANDOFF.md`에 보존한다.
매 작업 시작 시 원격 master/PR/CI를 확인한다. 아래 값은 통합 시작 기준이지 영구 최신 SHA가 아니다.

## 원격 기준과 검증 범위

이번 통합 시작 master: `c409eb6aa1c4b6eee22d562391165ca7f72f3020` (PR #17 병합).
NUM-1(PR #12), RUN-1(PR #14), 수집 종료 보강(PR #16), 수신 진단(PR #17)은 병합됐다.
PR #13은 superseded, closed/not merged다. 이전의 수정 대기/xfail 표시는 현재 판정이 아니다.
시작 master의 CI #192는 전체 1,516 passed / 6 deselected였다.
execution 감사 범위는 9,450조합 / 62,886 checkpoint다. RES-1/CLK-1 정책은 유지한다.
집중 검사와 전체 검사는 중복이며 합산하지 않는다. deselected는 통과가 아니다.

- PR #16: `adf370ecb70f5490b44d629d46a3b87fa500c707`로 병합.
  [종료 계약](docs/COLLECTOR_TEARDOWN.md). `--explicit-ocx-teardown`은 기본 꺼짐이다.
  해제 보류와 실제 실패를 분리하고, 일반 log close 실패에도 Qt quit을 시도한다.
- PR #17: [수신 진단 계약](docs/COLLECTOR_TELEMETRY.md). `--capture-telemetry`도 기본 꺼짐이며
  해제 옵션과 독립이다. close 경합 인계, signed FID 시계 차이, 표본/진단 실패 경계를 유지한다.
  CI #156의 flush 예외 close 누락과 #162의 일회성 raw startup 실패 이력은 지우지 않는다.
  startup 상태 상세·4회 반복 회귀를 추가했지만 #162의 근본 원인은 미확정이다.
- [PR #15](https://github.com/Peter-jackson12/Stock/pull/15): [격리 복제 합성 lab 명세](tests/RAW_V2_CLONE_LAB_SPEC.md).
  독립 원격 감사는 기존 HEAD `257e8f1ea0e54e926466e8094d69865e9c6c4d37`에서 수행됐다.
  해당 감사의 medium 권고를 반영해 계측처럼 보이던 상수 필드를 제거하고 원래 작업 오류와
  최종 source 검증 오류를 별도 보존한다. source before/after 직접 대조와 실패/취소 회귀를 보강한다.
  최신 master 위에 CI 집중 단계와 문서 인덱스를 합집합으로 통합하며 collector/qualification 코드는
  이번 복제 변경에서 수정하지 않는다. 최종 HEAD/run/job와 병합 여부는 PR/Actions에서 확인한다.

원격 합성/CI 성공은 실제 32비트 키움/Qt/보안 모듈, native 오류 재발 방지, 실수집 처리량을 인증하지 않는다.
이번 작업에서 로컬 설치·코드 동기화·새 로그인·수집 실행·운영 raw 복사는 하지 않았다.

## 2026-09-22 수집 장애 — 과거 첨부 사본 및 사용자 관측

session_id: `39b5af8b45024458be9a3ae2a2259685`.
수집 revision: `6a6d6076649befc767e5d8d59151cbcfb2f27c34`, PID 11788, Python 3.10.11 32비트.
raw: `sampledata/raw_ticks_v2/20260922/39b5af8b45024458be9a3ae2a2259685.db`.

첨부 로그/status 사본: 마지막 콜백 10:23:47 KST, 보호 종료 요청 10:33:47,
저장 마무리 보고 10:33:48. accepted=committed=11,198,913, final_seq=11,212,670,
writer_closed=true, pending/queued/in-flight/dropped=0, data_quality=unverified다.
쓰기 실패 전용 필드는 없으므로 0으로 만들어 적지 않는다.
로컬 에이전트의 17:40 조회 보고: 프로세스 부재, raw 12,940,107,776바이트, sidecar 부재.
과거 보고이지 현재 프로세스 상태 인증이 아니다. 실제 raw 내용·해시·peer journal은 열지 않았다.

사용자는 16시경 Runtime Error 창을 이미 보았고 17:07경 직접 닫았다.
17:07 Application Error/WER는 최초 팝업 시각이 아니다. 정확한 최초 표시는 미상이며
10:33:48 이후라는 하한도 입증되지 않았다. 수신 중단과 native 오류의 동일 원인은 미확정이다.
앞단 버퍼 소진은 메모리/콜백 패턴과 양립하는 가설이지 입증된 원인이 아니다.
화면의 대기큐는 Python 저장 큐이며 OCX/Qt 앞단 대기량이 아니다.
faulthandler의 app.exec_()와 C++ e06d7363/KERNELBASE 표기만으로 최초 원인 모듈을 특정하지 않는다.

## 2026-09-21 원본과 기존 차단 조건

session_id `6f39117671c048f6b60477ceafbf40b6`, collection revision `4821762fd93230b658339fee084d6c08e3e53ce9`.
raw 크기 50,635,071,488바이트. 최초 사용자 파일 SHA-256 주장은
`E4304FE3C1CAD8A85EC6C297CEB9CFDADCA2D20001E93756303D567EC5077569`다.
payload SHA-256 주장은 `a988d3bf86e36f44a209480658f537088a8910768c68c9fec83349f3de7f1755`다.
두 해시의 대상은 다르며 재검증하지 않았다.

종료 계수 callbacks=42,796,226 / final_seq=42,836,791.
차이에서 도출한 40,564는 parse_error 예상 단서이지 SQL 집계나 unsigned 체결 확인 수가 아니다.
제한 표본의 unsigned FID15 5건과 대응 parse_error는 품질 문제다. 임의 방향 보정은 없다.
whole-file stream integrity, 품질 이유·시간·종목 분포, 첫 연구 실행은 미완료다.

실제 -wal 0바이트 / -shm 32,768바이트는 삭제하지 않았다.
원본 보호 해제 → writable SQLite 재연결의 in-place cleanup은 채택하지 않는다.
PR #15는 외부 DB 인수를 받지 않는 작은 fixture lab이다. 원본 handle 보호·evidence/working 분리의
합성 검증과 운영 namespace/동시 접근/실제 sidecar 출처 인증을 구별한다.
새 자식 이름의 순간 생성·제거, 비협조적 working 쓰기, 총 물리 I/O, 전원 장애/실패 기록 원자성은
이 lab의 성공으로 해결되지 않는다. 9월 22일 12.94GB raw와 21일 50.6GB raw를 혼동하지 않는다.

## 다음 작업과 승인 경계

PR #15의 최신 master 통합 diff/CI를 확인해 원격 작업을 마무리한다. 정확한 최종 결과는 PR에 둔다.
그 이후 수집기 쪽 다음 단계는 로컬 32비트 환경의 읽기 전용 사전점검과 제한 실측 설계다.
코드 배포·새 로그인·수집 재실행은 자동 승인하지 않는다. telemetry와 명시적 OCX 해제는 별도 조건으로 검토한다.
수집 진단 실측과 50.6GB 파일 복제/qualification은 서로 다른 작업이다.

운영 파일 집합 복제에는 대상 identity·sidecar 출처·외부 reader/writer 차단·namespace/경로 격리·
장외 시각·collector 부재·free space·총 I/O/time 예산·실패 보존 설계와 별도 사용자 승인이 필요하다.
그 후에도 whole-file qualification에서 무결성·연구 적격성·실제 parse_error 이유/분포를 분리한다.

closed != data quality pass; sample clean != whole-file clean;
stream integrity != research eligibility; parse_error != file corruption;
qualification != strategy validation; backtest != live trading approval.
`Daily_baseline`·`old_data`·운영 raw·operations_state·사용자 변경·오류 근거를 보존한다.
원본 폐기·시간 절단·방향 보정·venue 인증·FIRST_RESEARCH_CANDIDATE 승격은 승인하지 않는다.
