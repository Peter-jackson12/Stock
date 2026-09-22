# 현재 인계 — 2026-09-22 / 종료·수신 진단 PR, 운영 미승인

[문서 인덱스](README.md) · [첫 시험 체크리스트](BACKTEST_TODO.md) ·
[파이프라인 지도](docs/PIPELINE_MAP.md) · [보존본 안내](docs/archive/README.md)

이전 상세 인계와 원문은 `4c677bf9ed535f4b9ba529af81b0c00f3eb8af97:HANDOFF.md`에 보존한다.
매 작업 시작 시 원격 master/PR/CI를 다시 확인한다. 아래 SHA를 최신값으로 가정하지 않는다.

## 원격 기준과 진행 중 PR

현재 master: `adf370ecb70f5490b44d629d46a3b87fa500c707` (PR #16 병합).
NUM-1(PR #12), RUN-1(PR #14), 수집 종료 보강(PR #16)은 병합됐다. PR #13은 superseded, closed/not merged다.
이전 인계의 수정 대기/xfailed는 현재 코드 판정이 아니다.
이 기준의 Git-only 회귀는 1,418 passed / 6 deselected / 0 xfailed였으며,
execution 감사 범위는 9,450조합 / 62,886 checkpoint다. 새 PR의 통과 수와 합산하지 않는다.
RES-1 one-pass retry와 CLK-1 advance 호출열 정책은 유지한다.

- PR #15: 원본 보호 handle 기반 격리 복제 **합성 lab**, 미병합. 최종 보고 기준
  `257e8f1ea0e54e926466e8094d69865e9c6c4d37`, 1,454 passed / 6 deselected.
  운영 복제·sidecar 정리 승인이 아니다. 해당 PR의 변경은 아래 PR에 포함하지 않는다.
- PR #16은 master `adf370ec...`로 병합됐다. 최종 CI #185에서 teardown/수집 집중 69 passed,
  전체 1,459 passed / 6 deselected를 decoded 로그로 확인했다. `--explicit-ocx-teardown`은 기본 꺼짐이다.
- PR #17: [Qt 폴링·기존 FID 제한 표본 진단](docs/COLLECTOR_TELEMETRY.md).
  PR #16 병합 뒤 base를 master로 옮겼다. `--capture-telemetry`는 기본 꺼짐이며 해제 옵션과 독립이다.
  독립 감사의 high finding인 flush-lock 경합 중 close 포기를 수정해 active flush가 close 요청을
  finally에서 인계하도록 했고, 호출부는 pending phase를 남겨 후속 정리에서 다시 확인한다.
  poll age의 음수값은 수치로 발행하지 않고, 표본 slot이 성공이 아닌 선택 시도 기준임을 문서·회귀로 고정했다.
  CI #156의 flush 예외 close 누락과 #162의 일회성 raw startup 실패 이력은 지우지 않는다.
  startup 실패에는 ready/state/error/writer_done을 남기고 기본 시작·종료 회귀를 4회 반복한다.
  master 재동기화 전 CI #183은 telemetry/통합 57 passed, teardown 69 passed,
  전체 1,516 passed / 6 deselected였다. 최종 master-base HEAD/run/job는 다시 확인한다.

두 PR은 실제 32비트 키움/Qt/보안 모듈 동작, native 오류 재발 방지, 수신 처리량을 인증하지 않는다.
저부하는 설계 목표이며 실측 완료가 아니다. 로컬 설치·동기화·수집 실행·운영 적용은 하지 않았다.

## 2026-09-22 수집 장애 — 첨부 사본 및 사용자 관측

session_id: `39b5af8b45024458be9a3ae2a2259685`.
수집 revision: `6a6d6076649befc767e5d8d59151cbcfb2f27c34`, PID 11788, Python 3.10.11 32비트.
raw: `sampledata/raw_ticks_v2/20260922/39b5af8b45024458be9a3ae2a2259685.db`.

첨부한 로그/status 사본은 마지막 콜백 10:23:47 KST, 보호 종료 요청 10:33:47,
저장 마무리 보고 10:33:48을 보인다. accepted=committed=11,198,913,
final_seq=11,212,670, writer_closed=true, pending/queued/in-flight/dropped=0,
data_quality=unverified다. 쓰기 실패 전용 필드는 없으므로 0이라고 만들어 적지 않는다.

로컬 에이전트는 17:40 조회 시 프로세스 부재, raw 크기 12,940,107,776바이트,
sidecar 부재를 보고했다. 이는 과거 관측이며 현재 프로세스 상태 인증이 아니다.
ChatGPT는 첨부 텍스트와 원격 코드만 대조했다. 실제 raw 내용·해시·peer journal은 열지 않았다.

**사용자 확인:** 16시경 Runtime Error 창이 이미 떠 있었고 17:07경 직접 닫았다.
17:07 Application Error/WER를 최초 팝업 시각으로 사용하지 않는다.
정확한 최초 표시 시각은 미상이며 10:33:48 이후라는 하한도 입증되지 않았다.
종료 해제 문제와 10:23 수신 중단의 인과관계·동일 원인은 아직 미확정이다.

메모리 상승 후 감소 중에도 콜백이 계속 증가한 패턴은 앞단 버퍼 소진 가설과 양립하지만
그 가설을 입증하지 않는다. 화면의 대기큐는 Python 저장 큐이며 OCX/Qt 앞단 대기량이 아니다.
Python faulthandler 스택에 app.exec_()가 보인다고 native 내부 정상이나 교착을 인증하지 않는다.
C++ 예외 e06d7363와 KERNELBASE.dll 표기만으로 최초 원인 모듈을 특정하지 않는다.

## 2026-09-21 원본과 기존 차단 조건

session_id `6f39117671c048f6b60477ceafbf40b6`, collection revision `4821762fd93230b658339fee084d6c08e3e53ce9`.
raw 크기 50,635,071,488바이트. 최초 사용자 파일 SHA-256 주장은
`E4304FE3C1CAD8A85EC6C297CEB9CFDADCA2D20001E93756303D567EC5077569`다.
payload SHA-256 주장은 `a988d3bf86e36f44a209480658f537088a8910768c68c9fec83349f3de7f1755`다.
두 해시의 대상은 다르며 재검증하지 않았다.

종료 증거의 계수는 callbacks=42,796,226 / final_seq=42,836,791이다.
차이에서 도출한 40,564는 parse_error 예상 단서이지 SQL 집계나 unsigned 거래 확인 수가 아니다.
제한 표본의 unsigned FID15 5건과 대응 parse_error는 품질 문제다. 임의 방향 보정은 없다.
whole-file stream integrity / 품질 이유·시간·종목 분포 및 첫 연구 실행은 미완료다.

실제 -wal 0바이트 / -shm 32,768바이트를 삭제하지 않았다.
원본 보호 해제 → writable SQLite 재연결의 in-place cleanup은 채택하지 않는다.
PR #15 합성 경로도 운영 namespace/동시 접근/실제 출처의 충분 조건 인증이 아니다.
오늘 12.94GB raw와 어제 50.6GB raw의 상태·sidecar를 혼동하지 않는다.

## 다음 작업과 승인 경계

PR #17은 최신 master를 이력 재작성 없이 정상 merge parent로 동기화하고 최종 diff/CI를 확인한 뒤 병합 판단한다.
그 다음 PR #15의 독립 검토와 병합 판단으로 돌아간다. #15와 공통 문서/CI 충돌은 별도로 조정한다.

신규 로그인·재수집은 자동 실행하지 않는다. 실제 적용에는 대상 identity·사전 상태·장외 시각·
동시 접근 배제·경로 보호·free space·I/O/time 예산과 별도 사용자 승인이 필요하다.
합성 통과를 native 오류 해결/운영 승인으로 읽지 않는다.

closed != data quality pass; sample clean != whole-file clean;
stream integrity != research eligibility; parse_error != file corruption;
qualification != strategy validation; backtest != live trading approval.
`Daily_baseline`·`old_data`·운영 raw·operations_state·사용자 변경과 오류 근거를 보존한다.
원본 폐기·시간 절단·방향 보정·venue 인증·FIRST_RESEARCH_CANDIDATE 승격은 승인하지 않는다.
