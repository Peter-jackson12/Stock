# 파이프라인 지도와 구조 점검

[문서 인덱스](../README.md) · [현재 인계](../HANDOFF.md) · [첫 시험 체크리스트](../BACKTEST_TODO.md)

점검 기준: `1ca29221598830d521530a0b2f9bb64961c2086d`, 2026-09-20.
이 문서는 **코드 연결·진입점 차이·구조 개선의 우선순위**를 담당한다. 실행 옵션의 상세 계약은 각 런북을 따른다.
원격 코드/관련 회귀를 대조한 구조 감사이며 모든 모듈의 전수 실행·실피드·로컬 데이터 검증은 아니다.

## 1. 유지할 기본 구조

```text
32bit Qt/OCX 수집 프로세스
  KiwoomUniverseLogger: 로그인·구독·콜백 원문 추출·종료 요청
    → LiveRawCapture: 세션 identity·상태·저널
      → QueuedCapture: 제한 큐·단일 저장 워커
        → CaptureSession: 공통 seq·정규화·품질 기록
          → RawV2Writer: 원문 envelope + 정규화 이벤트 저장

수집 종료 근거 대조 (운영 절차)
  → 제한 표본 대조 (검사 전용)
  → qualify_raw_v2 (원본 비변경 whole-file 구조·품질 분리 진단)
  → run_raw_v2 / run_research
    → TickSimulator + NxtResearchStrategy
    → research_runs/<run-id>/result.json
  → 결과 조회 / 대표 사례 / 재현성 대조
```

raw-v2는 원문만 저장하고 정규화를 훗날 처음 하는 구조가 아니다. 저장 워커가 원문을 보존하면서
정책에 따른 정규화 이벤트와 품질 제어 기록도 함께 남긴다. Qt 콜백에 동기 SQLite 작업을 합치지 않는다.

| 경계 | 구현 근거 | 유지 이유 |
|---|---|---|
| Qt 원문 추출 ↔ 큐 | [logger](../collector/kiwoom/kiwoom_universe_logger.py), [live_capture](../collector/kiwoom/live_capture.py) | OCX 소유 스레드와 저장 I/O 분리 |
| 큐 ↔ 저장 | [queued_capture](../collector/kiwoom/queued_capture.py), [capture_session](../collector/kiwoom/capture_session.py) | 콜백/레코드 계수·순번·drain 경계 유지 |
| 원문 ↔ 정규화 | [tick_normalizer](../collector/kiwoom/tick_normalizer.py), [raw_v2](../collector/raw_v2.py) | 원문·정책·오류 근거를 보존한 재계산 가능성 |
| 화면 ↔ 작업 | [service](../control_tower/service.py), [offline_worker](../control_tower/offline_worker.py) | 화면 종료와 작업 생명주기 분리 |
| 입력 ↔ 전략/가상 체결 | [tick_research_run](../engine/tick_research_run.py), [tick_simulator](../execution/tick_simulator.py) | 입력 실패와 진단 결과, 실제 주문 실행을 구분 |

상태 객체가 여러 개라는 이유만으로 중복이라고 판정하지 않는다. 큐 종료·저널 closed·OS 프로세스 종료·
연구 입력 합격은 서로 다른 증거다. 단순화 대상은 이 증거를 반복 수집하는 사람의 절차이지 증거 자체의 삭제가 아니다.

<a id="entrypoints"></a>
## 2. 진입점에 따라 다른 계약

| 경로 | 실제 진입점/소유자 | 주의할 차이 |
|---|---|---|
| 독립 기본 수집 | `collector/kiwoom/kiwoom_universe_logger.py --storage raw-v2` | 기존 유니버스·기본 15:35 종료 요청. 사용자 승인 필요. 실제 서버 관측은 하지만 `--server` 옵션/요청 서버 불일치 가드는 없음 |
| 관리 화면 수집 | [managed_capture](../control_tower/managed_capture.py)와 logger의 managed 경로 | 새 소규모 계획의 서버/종목/시간으로 기동. 실제 서버와 계획이 다르면 거부. 외부 CLI 세션을 자동 인수하지 않음 |
| NXT/애프터마켓 실험 | logger의 명시적 구독 계획/전환 옵션 | 기본 경로와 별개. 전환·제한 시간 관련 코드가 존재하지만 실피드 coverage/venue 인증과 같지 않음. 첫 기본 수집에 자동 적용하지 않음 |
| 제한 표본 | [inspect_raw_v2_sample](../scripts/inspect_raw_v2_sample.py) | 닫힌 파일의 작은 seq 구간만 검사. 저장 당시 정책을 재적용. 전략 실행·전체 checksum 없음 |
| whole-file qualification | [qualify_raw_v2](../scripts/qualify_raw_v2.py) | Windows 로컬 NTFS·sidecar 없음·write/delete handle 배제를 전제로 전체 구조/checksum과 품질 적합성을 분리. 전략 실행 없음 |
| sidecar 합성 lab | [lab_raw_v2_sidecars](../scripts/lab_raw_v2_sidecars.py) | 외부 DB 인자를 받지 않고 UUID/marker가 있는 임시 NTFS fixture만 생성·실측. 운영 cleanup 도구가 아니며 결과도 운영 승인이 아님 |
| 화면 연구 계획 | `service.plan_replay()` | 헤더만 읽어 계획 저장. 등록 성공은 입력 승인/실행 성공 아님 |
| 화면 장외 실행 | `offline_worker.run_replay_job()` | 평일 시간/수집 잠금/상태 가드와 크기·건수 제한. 전체 reader 소진 후 연구 실행 |
| 직접 연구 CLI | [run_tick_research](../scripts/run_tick_research.py) → `run_raw_v2()` | 화면의 시간·수집 잠금·크기 제한을 자동 상속하지 않음. 장외 실행은 운영자가 보장. 검증과 전략 재생이 함께 진행 |
| 결과 조회 | [inspect_tick_research](../scripts/inspect_tick_research.py) 또는 조회 워커 | 결과 JSON만 검사. 조회 완료 ≠ 연구 성공 ≠ 원본 검증 |

**기본 15:35 종료 요청은 Windows 로컬 시각을 사용한다.** KST 시계/시간대와 당일 시장 운영 조건을 실행 전에 확인한다.
실제 종료는 drain 뒤 완료되며 시각 도달만으로 closed를 선언하지 않는다.
공식 preflight는 환경/OCX 등록 확인이지 시장 일정·서버 승인·전체 운영 준비의 통합 판정기가 아니다.

### 직접 CLI의 whole-file 검증을 읽는 법

[reader](../collector/raw_v2.py)의 checksum 대조는 iterator를 끝까지 소비했을 때 완료된다.
[run_raw_v2](../engine/tick_research_run.py)는 읽는 도중 선택 이벤트를 전략에 넘기고,
`session_start`/`session_note` 외 품질 제어 기록을 만나면 종목 필터 전에 실패한다.
따라서 성공 종료는 전체 읽기 완료를 요구하지만, 품질 오류로 조기 종료한 실행은 전체 checksum 완료를 뜻하지 않는다.
끝에서 오류를 발견해도 해당 런 전체가 failed/진단 전용이 된다. 중간 주문·체결을 정상 성과로 쓰지 않는다.

화면 워커는 이와 달리 먼저 전체 reader를 소진한다. 다만 그 선행 무결성 스캔 자체가
품질 제어 기록의 연구 적합성까지 승인하는 것은 아니다. 품질 거부는 이후 연구 경로에서도 적용된다.

첫 시험의 "검사 완료 후 전략 실행"에서 검사 전용 진입점은 `qualify_raw_v2.py`다.
현행 연구 CLI와 달리 품질 오류를 집계하면서 끝까지 읽고, 구조 무결성과 연구 적합성을 별도 필드로 남긴다.
다만 실제 대용량 실행의 장외 시각·I/O 예산·중단 기준은 대상별로 확정해야 하며, sidecar가 있거나 활성
writer를 배제할 수 없으면 읽지 않는다. 직접 연구 CLI를 검사 도구로 이름만 바꾸거나 작은 화면/압축
시제품의 상한을 풀어 대체하지 않는다. 담당 체크 항목은
[BACKTEST_TODO §3](../BACKTEST_TODO.md#3-장외-실행-전-준비)에 있다.

## 3. 기본 경로가 아닌 것

| 구분 | 유지 위치 | 기본 틱 연구와의 관계 |
|---|---|---|
| raw-v1·LOB/초봉·기존 성과 런 | `collector/build_lob_db.py`, `engine/main.py`, `runs/` | 레거시 회귀/비교용. 삭제 대상도, raw-v2 연구 필수 선행 작업도 아님 |
| KIS daemon | `collector/run_daily_daemon.py` | 키움 전체 파이프라인 관리자가 아닌 별도 프로그램 |
| 일봉·opt10001 메타데이터·fchart | `collector/daily_collector.py`, `collector/kiwoom/run_meta_batch.py` | 별도 공급자/시점/실제가 계약. 수집 중 추가 로그인·자동 실행 금지 |
| raw 압축·복원 | `collector/raw_archive.py`, `scripts/archive_raw_v2.py` | 별도 장외 작은 파일 시제품. 원본 자동 삭제·대용량 준비 완료가 아님 |
| 제어 이력·IPC·예약·외부 인증 | `control_tower/`, [제어 설계](../CONTROL_TOWER.md) | 운영 요구에 따른 기능. 한 세션 수집을 위해 전부 새로 설치할 필요는 없음 |

레거시/확장 코드의 사용 빈도와 실제 import/CLI 호출을 확인하지 않고 삭제하지 않는다.
ARCHITECTURE_V2와 REFACTORING_PLAN에는 아직 참조되는 계약·스키마도 있으므로 이번에 통째로 archive로 옮기지 않았다.

## 4. 문서 감사 결과와 이번 조치

| 발견 | 조치 |
|---|---|
| HANDOFF에 현재 인계와 과거 인계 111,744바이트가 혼재 | 원문 blob 보존 후 현재 행동/차단 조건 중심으로 분리. 크기 회귀 검사 추가 |
| BACKTEST_TODO의 긴 후보 조사와 뒤의 최신 결정이 중복 | 원문 보존 후 판정표·현재 체크 항목만 유지 |
| 표본 안내가 최초 100건과 각 300건으로 갈림 | 표본 상세 계약은 연구 런북 한 곳. 체크리스트는 최초 100건으로 연결 |
| 연구 런북 말미의 OCX 미적용/계획만 가능 표현 | 현재 운영 연결·화면 실행 존재와 원천 검증 미완료를 분리해 수정 |
| 화면과 직접 CLI를 같은 선행 검증 절차로 읽을 위험 | 경로별 가드·검증 완료 시점·품질 실패의 범위를 명시 |
| 과거 설계의 무유실/is_buy 판정 주장을 현재로 읽을 위험 | 현재/상세/역사적 자료의 읽기 경로와 담당 문서를 README에서 구분 |

상세 수집·제어 문서에는 여전히 단계별 구현 경과가 남아 있다. 이번에는 고유 계약을 대량 삭제하지 않고
현재 진입점 지도로 길을 분리했다. 향후 절 단위 정리는 각각의 옵션·호출부와 회귀를 함께 읽고 수행한다.
활성 링크 검사 통과는 그 문서 전체의 사실 정확성을 보장하지 않는다.

## 5. 후속 단순화 — 이번에 구현하지 않음

**첫 연구 전:** 구현된 qualification 경로의 Windows 합성 회귀와 별개로, 실제 대상의 sidecar를 보존한
해결 절차와 대용량 실행 예산을 확정한다. 입력 identity·전체 소진 여부·품질 분포·코드 해시·검증 범위를
남기고, 스캔 중단을 합격으로 바꾸지 않는다. 이미 확인한 종료/표본 근거를 다시 스캔해 대체하지 않는다.
신규 수집 시작 자체를 이 개발의 완료에 묶지 않는다.

**이후 작은 변경:** 독립 CLI의 요청 서버 검증 가드, 종료 증거 읽기 전용 요약 도구,
공통 설정 해석의 재사용을 검토한다. 각각 합성 반례와 이전 동작 대조 후 따로 적용한다.

**나중에만:** 필요성이 확인되면 기존 함수를 호출하는 얇은 통합 CLI를 검토한다.
새 상태 머신·자동 재로그인·대형 범용 오케스트레이터를 먼저 만들지 않는다.
현재 없는 통합 명령을 사용 안내에 실행 가능한 것으로 싣지 않는다.

이번 변경은 문서와 문서 회귀에 한정한다. 큐/정규화/저장/수집 종료/연구 실행의 런타임 코드는 유지한다.
