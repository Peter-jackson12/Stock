# 현재 인계 — 2026-09-23 / 수집 의사결정 계약 감사·live 분리

[문서 인덱스](README.md) · [첫 시험 체크리스트](BACKTEST_TODO.md) ·
[파이프라인 지도](docs/PIPELINE_MAP.md) · [보존본 안내](docs/archive/README.md)

매 작업 시작 시 원격 master/열린 PR/최신 CI를 직접 확인한다. 아래 값은 감사 시작 기준이지 영구 최신값이 아니다.
이전 상세 인계는 `5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70:HANDOFF.md`에 보존한다.

## 오늘 live — 사용자 보고와 원격 감사의 분리

2026-09-23 작업 요청에서 사용자가 실행 중이라고 알린 세션:
`7a35b11eddff4dcea88c98acdc8b37df`, collection revision
`5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70`.
알려진 옵션은 `--capture-telemetry` ON / `--explicit-ocx-teardown` OFF다.
이는 사용자 보고이며 이번 GitHub 감사의 프로세스·CLI 전체·서버·수신/저장 상태 직접 관측이 아니다.
보고에 없는 옵션이나 현재 생존/무누락/정상 종료를 추정하지 않는다.

이 브랜치의 문서·테스트 변경은 해당 실행에 적용되지 않는다. **master 변경·PR 병합은 보류한다.**
live 종료 뒤 별도 확인 전에는 자동 병합하지 않는다. PR/CI 성공도 로컬 동기화·재시작 승인이 아니다.
사용자 PC·운영 체크아웃·OCX·`.venv32`·raw/evidence·operations_state·DB·모니터 창을
조회하거나 변경하지 않는다. 로컬 git 작업·pytest·추가 로그인·qualification·백테스트도 하지 않는다.

시각·범위 질문에는 [수집 의사결정 계약](docs/COLLECTION_RUNBOOK.md#collection-decision)을 먼저 읽는다.
허용/금지 작업은 [live 작업 경계](docs/COLLECTION_RUNBOOK.md#collection-live-boundary)를 따른다.
시장 시간표를 이 인계에 복제하지 않는다. 프로필에 없는 시간을 수집 불가로 판단하거나
기본 6자리 장전 수신을 NXT 프리마켓 coverage로 부르지 않는다.

## 원격 기준과 검증 범위

감사 시작 master: `5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70`, 열린 PR 0개,
최신 master CI #195 success. 이전 PR #15는 이 master로 병합됐다.
PR #15 최종 CI #194의 전체 Git-only는 1,565 passed / 6 deselected였다.
현재 감사의 최종 HEAD/run/job·통과 건수는 이 브랜치 PR/Actions에서 확인한다.
집중 검사와 전체 검사는 중복이며 합산하지 않는다. deselected는 통과가 아니다.

NUM-1(PR #12), RUN-1(PR #14), 수집 종료 보강(PR #16), 수신 진단(PR #17),
격리 복제 합성 lab(PR #15)은 병합됐다. PR #13은 superseded, closed/not merged다.
execution 감사 범위는 9,450조합 / 62,886 checkpoint이며 RES-1/CLK-1 정책은 유지한다.
[종료 계약](docs/COLLECTOR_TEARDOWN.md)과 [수신 진단 계약](docs/COLLECTOR_TELEMETRY.md)의
두 옵션은 기본 OFF이며 서로 독립이다. 오늘 사용자가 알린 실행 옵션과 코드 기본값을 혼동하지 않는다.
CI #156의 flush 예외 close 누락과 #162의 일회성 raw startup 실패 이력은 지우지 않는다.
startup 상세·4회 반복 회귀를 추가했지만 #162의 근본 원인은 미확정이다.

[복제 lab 명세](tests/RAW_V2_CLONE_LAB_SPEC.md)는 외부 DB 인수를 받지 않는 작은 fixture lab이다.
선언된 원본 재연결 금지와 실제 호출 계측, 원래 작업 오류와 최종 source 검증 오류·취소를 구분한다.
원격 합성/CI 성공은 실제 32비트 키움/Qt/보안 모듈, native 오류 재발 방지, 실수집 처리량을 인증하지 않는다.
이번 감사는 운영 collector 동작·raw schema·방향/venue 정책을 변경하는 작업이 아니다.

## 2026-09-22 장애 — 과거 근거를 현재 상태로 쓰지 않음

session_id `39b5af8b45024458be9a3ae2a2259685`, collection revision
`6a6d6076649befc767e5d8d59151cbcfb2f27c34`, PID 11788, Python 3.10.11 32비트.
과거 첨부 사본의 마지막 콜백 10:23:47 KST, 보호 종료 요청 10:33:47, 저장 마무리 보고 10:33:48.
accepted=committed=11,198,913, final_seq=11,212,670, writer_closed=true,
pending/queued/in-flight/dropped=0, data_quality=unverified였다.
쓰기 실패 전용 필드가 없으므로 0으로 만들어 적지 않는다.
과거 로컬 에이전트의 17:40 보고는 프로세스 부재·raw 12,940,107,776바이트·sidecar 부재다.
이번 감사는 실제 raw 내용·해시·peer journal을 열지 않았다.

사용자는 16시경 Runtime Error 창을 이미 보았고 17:07경 직접 닫았다고 보고했다.
17:07 Application Error/WER는 최초 팝업 시각이 아니다. 정확한 최초 표시는 미상이며
10:33:48 이후라는 하한도 입증되지 않았다. 수신 중단과 native 오류의 동일 원인은 미확정이다.
앞단 버퍼 소진은 가설이며 화면 큐는 Python 저장 큐이지 OCX/Qt 앞단 대기량이 아니다.
faulthandler의 app.exec_()와 C++ e06d7363/KERNELBASE만으로 최초 원인 모듈을 특정하지 않는다.

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
원본 handle 보호·evidence/working 분리의 합성 검증과 운영 namespace/동시 접근/sidecar 출처 인증을 구별한다.
새 자식 이름의 순간 생성·제거, 비협조적 working 쓰기, 총 물리 I/O, 전원 장애/실패 기록 원자성은
lab 성공으로 해결되지 않는다. 22일 12.94GB raw와 21일 50.6GB raw를 혼동하지 않는다.

## 다음 작업과 승인 경계

이 감사 브랜치의 문서 의존 연결·구간 외 합성 저장 회귀를 GitHub Actions에서 검증한다.
PR 최종 CI를 확인하되 merge하지 않는다. live 종료 후 별도 확인 때 master/PR/CI와 실행 revision을 다시 대조한다.
NXT 전체 coverage, 장시간 프리/애프터 rollout, 시장별 침묵 정책 보정은 별도 후속 작업이다.

로컬 32비트 사전점검·제한 진단 실측은 GitHub 감사와 별개다. 코드 배포·새 로그인·재실행을 자동 승인하지 않는다.
수집 진단 실측과 50.6GB 복제/qualification도 서로 다른 작업이다.
운영 파일 집합 복제에는 대상 identity·sidecar 출처·외부 reader/writer 차단·namespace/경로 격리·
장외 시각·collector 부재·free space·총 I/O/time 예산·실패 보존 설계와 별도 사용자 승인이 필요하다.
그 후에도 whole-file qualification에서 무결성·연구 적격성·실제 parse_error 이유/분포를 분리한다.

closed != data quality pass; sample clean != whole-file clean;
stream integrity != research eligibility; parse_error != file corruption;
qualification != strategy validation; backtest != live trading approval.
`Daily_baseline`·`old_data`·운영 raw·operations_state·사용자 변경·오류 근거를 보존한다.
원본 폐기·시간 절단·방향 보정·venue 인증·FIRST_RESEARCH_CANDIDATE 승격은 승인하지 않는다.
