# 현재 인계 — 2026-09-23 / native backlog 조사와 컨트롤타워 상태 모델 재구축

[문서 인덱스](README.md) · [컨트롤타워](CONTROL_TOWER.md) ·
[파이프라인 지도](docs/PIPELINE_MAP.md) · [첫 시험 체크리스트](BACKTEST_TODO.md) ·
[보존본 안내](docs/archive/README.md)

직전 활성 인계는 `5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70:HANDOFF.md`에 보존돼 있다.
매 작업 시작 시 원격 master/PR/CI를 다시 확인한다. 아래 값은 현재 작업 기준이며 영구 최신값이 아니다.

## 원격 기준과 열린 진단 트랙

현재 master 기준: `5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70`.

- PR #18: 수집 시각·KRX/NXT 판단 계약 감사. open/draft/unmerged.
- PR #19: 과거 collector revision 고정 입력 비교. open/draft/unmerged.
- PR #20: 전종목 FID read A-B-A 진단 준비. open/draft/unmerged, HEAD
  `c263003da6be82cefe740d6b22896babfcc89cbc`. 실제 시장 실행 전까지 병합 보류.
- 현재 컨트롤타워 재구축 branch:
  `refactor/control-tower-evidence-model-20260923`. collector/native 실행 코드는 건드리지 않는다.

GitHub CI·합성 검증은 실제 32비트 QAx/OCX/native 동작이나 feed 정확성을 인증하지 않는다.

## 2026-09-23 실제 장애 — 현재 확인된 사실

대상 session: `7a35b11eddff4dcea88c98acdc8b37df`.
collection revision: `5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70`.
CPython 3.10.11 x86, collector PID 13572.

- 09:00 이후 FID20/FID21 clock difference가 점진적으로 증가했다.
  종류별 저빈도 telemetry에서 source progression ratio는 초기 약 0.60, memory peak 이후 약 0.80이었다.
  lag slope도 각각 약 0.35초/초, 약 0.17초/초로 같은 방향이다.
- memory recorded peak는 10:08:19.624 KST:
  commit 약 1767.7 MiB, working set 약 1747.7 MiB.
  10:11:22부터 memory 감소가 관측됐지만 lag는 약 1700초까지 계속 증가했다.
- callback-entry→queue-submit processing은 장애 구간에도 대체로 sub-ms 수준이었다.
  Qt/control poll은 callback 종료 부근까지 진입/복귀했고 GetConnectState 관측은 1이었다.
  poll 정상은 feed 건강 인증이 아니다.
- telemetry 마지막 sample은 10:36:18~19, status last_commit은 10:36:21.189,
  reception_stall은 10:38:21.937, silence_stop/shutdown은 10:46:21대다.
  telemetry 표본은 전수 callback이 아니므로 정확한 마지막 callback 시각은 미확정이다.
- first confirmed Runtime popup 관측은 10:46:36.764~10:47:08.204.
  last confirmed absent가 없어 popup 최초 시각과 callback stop의 인과 순서는 미확정이다.
- dump는 약 11:30:43에 생성된 사후 minidump이며 first-failure dump가 아니다.

### native dump 재감사

기존 x86 CDB와 로컬 image/symbol로 읽기 전용 감사했다.

- `OPComms → operator new → malloc NULL → AfxNewHandler → AfxThrowMemoryException`
  경로를 stack/register/disassembly로 대조했다.
- 실패 요청 크기: 정확히 `0x3e14 = 15,892 bytes`.
- 실제 python.exe는 Large Address Aware OFF.
- dump-time VA metadata: commit 약 199.6 MiB, reserve 약 1757.4 MiB,
  free 약 91.0 MiB, largest free region 약 60.3 MiB.
- dump가 failure/10:08 peak보다 훨씬 뒤이므로 이 VA 지도를 failure 순간으로 소급하지 않는다.
  native allocation failure는 SUPPORTED지만 x86 VA exhaustion/fragmentation 원인은 여전히 UNRESOLVED.
- heap metadata는 불충분해 heap corruption/fragmentation 직접 분석은 unavailable.
- allocation failure가 10:36 callback stop의 최초 원인이라는 인과도 미확정이다.

## 규모별 canary와 offline 분리 결과

### 1종목 / 10종목

1종목 60초 mock/live와 10종목 60초 ABBA는 모두:
저장 clean close, drop 0, queue/pending 0, PID/관련 창 종료, lease 해제.
FID clock difference는 대체로 1~3초였고 지속 증가가 없었다.

### 전종목 3,757개 / 60초

Mock 약 1,602 callbacks/s, Live 약 1,533 callbacks/s.
양쪽 모두 FID clock difference가 약 2초에서 16~20초로 증가했다.
Python 저장 queue sampled/reported peak는 71~82 수준이었고 accepted=committed, drop 0.
즉 전종목 규모가 lag 현상과 강하게 연관되지만 backlog 위치는 provider/OCX/COM/Qt 중 미확정이다.

### x86 offline raw-v2 replay

Qt/COM/Kiwoom 없이 CPython 3.10.11 x86에서 1,600 callbacks/s × 60초,
총 96,000 callback을 deterministic pacing했다.

- producer schedule 유지, final drift 약 -1ms
- queue sampled peak 32, pending peak 56
- accepted=committed, drop 0
- drain 약 0.016초
- producer 구간 memory 증가 약 3 MiB

따라서 Python raw-v2/SQLite writer가 실제 upstream lag의 주 병목이라는 가설은 상당히 약해졌다.
이 시험은 Qt/COM/GetCommRealData 처리율을 검증한 것이 아니다.

## PR #20 — 다음 정규장 진단

PR #20은 동일 전종목 구독을 유지한 한 Mock 세션에서:

`FULL 30초 → ESSENTIAL 30초 → FULL 30초`

로 callback 내부 GetCommRealData 호출 수만 바꾸는 diagnostic-only 모드다.

- FULL: trade 6 FID / quote 41 FID.
- ESSENTIAL: trade 3 FID / quote 23 FID.
- skip key는 raw dict에 남기되 값은 None. stale 값을 만들지 않는다.
- SetRealReg, REAL_FIDS, screen, 종목 수는 phase에서 바꾸지 않는다.
- Mock-only guard, 90초 고정, telemetry 필수.
- diagnostic feed_scope/sidecar로 research eligible이 아님을 명시.
- 실제 정규장 실행 전까지 merge/실행하지 않는다.

## 컨트롤타워 재구축 — 현재 작업

2026-09-23 장애에서 다음 상태들이 서로 독립임이 확인됐다.

- storage finalization / writer close
- Qt event-loop return
- lease release
- OS process exit
- Runtime/OCX native window absence
- callback activity
- source FID freshness
- research eligibility

특히 과거 장애에서는 storage closed + main finally/lease release 뒤에도 PID/Runtime popup/OCX가 남았다.
따라서 lease availability나 storage close를 process termination으로 승격하면 안 된다.
Python queue 0도 OCX/Qt/provider 앞단 backlog 부재를 뜻하지 않는다.

현재 branch는 `control_tower/session_assessment.py`를 추가해 다음 축을 독립적으로 유지한다.

- storage
- process
- native UI
- lease
- callback activity
- source freshness
- research eligibility

`verified_exited`는 process absent + native UI clear일 때만 파생한다.
lease state는 termination 계산에 사용하지 않는다.
dashboard는 bounded 기존 status/log에서 storage와 callback activity만 읽고, source freshness/process/native/lease/research는 별도 근거가 없으면 unverified로 둔다.
새 process/window scan, lease probe, raw 검사, qualification을 이 1단계에서 자동 실행하지 않는다.

## 현재 금지/보존 경계

- 운영 raw, dump, operations_state 기존 evidence를 수정하지 않는다.
- 현재 장애 원인 확정 전 collector 기본 FID, queue, LAA, Python bitness, teardown 기본값을 바꾸지 않는다.
- PR #18/#19/#20을 자동 merge하지 않는다.
- diagnostic raw를 연구 입력으로 승격하지 않는다.
- closed != process exited; lease free != process exited; queue 0 != upstream healthy; recent heartbeat != source freshness.
- stream integrity != research eligibility; qualification != strategy validation.
- 자동 PID kill/restart/relogin을 컨트롤타워 재구축에 추가하지 않는다.

## 바로 다음 행동

1. 컨트롤타워 상태 축 branch의 pure reducer/dashboard/docs 회귀와 전체 Git-only CI를 확인한다.
2. 이 branch는 collector/native 실행부를 건드리지 않은 상태로 draft PR에서 독립 리뷰한다.
3. 휴장 기간에는 read-only 상태 모델·dashboard·계약 테스트를 계속 개선할 수 있다.
4. 다음 정규장에는 별도 승인 후 PR #20 A-B-A Mock 한 세션으로 FID read 병목 가설을 검증한다.
5. 그 결과 전까지 collector 제어/자동 복구/기본 FID 정책은 동결한다.
