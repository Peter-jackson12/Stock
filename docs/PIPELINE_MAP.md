# 파이프라인 지도와 구조 점검

[문서 인덱스](../README.md) · [현재 인계](../HANDOFF.md) · [첫 시험 체크리스트](../BACKTEST_TODO.md)

기존 §1–5 점검 기준: `1ca29221598830d521530a0b2f9bb64961c2086d`, 2026-09-20.
§6은 2026-09-24 공유 계좌 연구 경로의 추가다. 기존 raw/수집 경로를 대체하지 않는다.
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

위 §1–5의 2026-09-20 변경은 문서와 문서 회귀에 한정했다. 이후 추가는 아래에서 구분한다.


<a id="portfolio-research"></a>
## 6. 다종목 공유 계좌 — 별도 opt-in 합성 연구 경로

```text
OrderedTick (단일 source/session의 공통 seq)
  → PortfolioSimulator.on_event: 이전 호가 타이머 → 새 이벤트 → 기존 주문 매칭
  → strategy(TickView, 불변 PortfolioSnapshot)
  → 순서가 명시된 OrderIntent / CancelIntent list 또는 tuple
  → 공유 현금·종목별 수량·예약·RiskLimits·주문 상태 전이
  → 메모리 내 portfolio_research_result_v1 dict
```

구현은 [공유 계좌 엔진](../execution/portfolio_simulator.py),
[주문 의도 드라이버](../engine/portfolio_session.py),
[합성 회귀](../tests/test_portfolio_simulator.py)다.
`ReceiveOrderReplay`, top-of-book 검증, 기존 정확한 현금 산술을 재사용한다.
`execution/account.py`와 `execution/broker.py`의 Phase E stub을 구현 완료로 해석하지 않는다.

**기존 경로와의 차이:** `TickSimulator`의 자원 부족 시 대기/재시도 계약을 바꾸지 않는다.
새 경로는 `portfolio_reservation_v1`로 주문 접수 때 미체결 전량의 ask+fee 또는 매도 수량을
예약한다. 접수 순번으로 자본을 배분하며 종목 사전 순서나 주문 ID 정렬로 우선순위를 바꾸지 않는다.
접수 시 호가가 없거나 유효하지 않으면 명시적 거절이다. 접수 후 호가가 stale/invalid이면 체결을 보류한다.
매칭 직전 새 ask로 잔여 전량의 비용·한도를 재검사하고, 부족하면 잔량만 rejected로 끝낸다.
이미 발생한 체결은 되돌리지 않으며 부분 접수/자금 부족 무기한 재시도 정책과 구분한다.

주문 상태는 created/pending/active/partially_filled/filled/cancel_pending/cancelled/expired/rejected다.
취소 확인 전에는 체결과 예약이 유지된다. 같은 시각의 취소 확인은 모든 종목의 체결보다 먼저다.
종료는 exclusive이며 잔량 만료/예약 해제만 한다. 보유 주식을 마지막 가격으로 가짜 청산하지 않는다.
새 호가 seq는 해당 종목의 표시 잔량을 재설정하지만 체결 tick/타이머는 보충하지 않는다.
chunk는 같은 엔진·전략 상태를 보존하며 경계에 advance/close를 추가하지 않는다.
임의의 advance 세분화 불변성은 주장하지 않는다. 모든 공개 호출 순서도 실험 입력이다.

RiskLimits는 종목별 수량·총 노출·open order 상한과 중복/상충 주문 가드를 제공한다.
총 노출은 fresh ask × (보유량 + 미체결 매수량)의 합이며 수수료를 제외한 매수 대체 원가다.
`gross_exposure_at_ask`는 equity나 실현/미실현 손익이 아니다. 가격이 오래되면 null/unpriced다.
총 노출 제한이 활성일 때 관련 보유/매수 종목의 호가가 평가 불가이면 매수를 거절한다.
매도는 이 노출 한도로 막지 않는다. 미체결 매도는 보유량 한도에서 미리 차감하지 않는다.
한도 수치는 호출자가 지정하며 실전 투자금/손절률/검증된 증권사 비용을 기본값으로 넣지 않는다.

`run_portfolio(..., simulator_config=..., close_ns=..., strategy=..., strategy_id=...)`는
입력/설정/코드 5파일/주문 의도와 결과를 기록한 JSON-native dict를 반환한다.
파일을 쓰거나 DB를 열지 않는다. 실패는 `PortfolioRunFailed.report`에 부분 체결과 시도한 의도를
`failed/diagnostics_only`로 보존한다. 실제 raw identity/품질 합격은 인증하지 않는다.
`strategy_id`는 호출자 라벨이며 전략 소스 해시가 아니다. 같은 초기 전략 상태와 결정적인 callback을
사용해야 반복 재현된다. `realized_pnl`, `unrealized_pnl`, `equity`는 아직 null이다.

**아직 연결하지 않은 것:** 실제 raw/기존 CLI/화면 입력, 원가/PnL/equity 평가,
Paper/Mock/Live 주문. NXT 단일 전략의 공유계좌 adapter와 JSON 결과 저장은 아래 §7 후보에서 분리한다.
현재 구현은 작은 합성 trace용이며 주문/체결 이력과 snapshot 비용이 커지는 대용량 성능은 검증하지 않았다.
기존 `run_research`/`run_raw_v2`의 입력 정책과 결과 형식은 그대로다. 합성 성공을
FIRST_RESEARCH_CANDIDATE 승격이나 실제 데이터 실행 승인으로 해석하지 않는다.


<a id="nxt-single-strategy-portfolio"></a>
## 7. NXT 한 전략 우선 — 공유계좌 adapter와 결과 저장

현재 우선순위는 여러 전략을 동시에 운용하는 프레임워크가 아니다. 먼저 기존
[단일종목 NXT 규칙](../strategies/nxt_breakout/tick_research.py)을 기준선으로 유지하면서
[portfolio adapter](../strategies/nxt_breakout/portfolio_adapter.py)가 종목별 상태를 분리하고,
[공유계좌 runner](../engine/nxt_portfolio_research.py)가 `PortfolioSimulator`에 연결한다.

adapter는 종목마다 기존 `NxtResearchStrategy` 인스턴스를 하나씩 가지지만 전략 종류는 하나다.
전략이 보는 symbol-scoped port는 해당 종목의 live order/fill과 현재 시각만 노출하고,
`submit/cancel` 호출을 전역 고유 `OrderIntent/CancelIntent`로 변환한다.
현금·보유량·예약·risk mutation과 실제 fill 생성은 계속 execution 계층만 소유한다.
종목별 serial이 같아도 전역 order id에는 code prefix가 붙어 충돌하지 않는다.

`run_nxt_portfolio()`는 normalized in-memory event에만 쓰는 opt-in 연구 진입점이다.
새 UUID 디렉토리의 `result.json`을 사용해 기존 결과를 덮어쓰지 않는다.
전략 quantity/exit/cooldown/params와 adapter·기존 NXT 규칙 코드 hash, 전략 signals를
reproducibility key에 포함한다. dataset label은 원본 인증이 아니며
`raw_identity_verified=false`를 유지한다. 실패가 event iteration 뒤 발생하면
`diagnostics_only` 부분 결과를 저장한 뒤 예외를 다시 낸다.

이 단계에서 평가 PnL을 만들지 않는다. 결과의 cash/position/fee/fill은 실행 원장이지만
`realized_pnl/unrealized_pnl/equity`는 계속 null이다. 다음 묶음에서 평균단가·실현손익,
open position mark/equity, MDD의 가격/비용 정의를 먼저 고정한 뒤 추가한다.
검증된 실제 raw가 없으므로 전략 파라미터 최적화나 좋은 종목/시간 사후선택도 시작하지 않는다.

다중 전략 registry/arbitration/전략별 자본 배분은 첫 전략을 평가·안정화한 뒤 실제 요구가 생길 때 추가한다.


<a id="bounded-prefix-qualification"></a>
## 8. 오전 bounded prefix — 전체 세션과 별도 적격성

[bounded prefix qualifier](../collector/raw_v2_prefix_qualification.py)와
[CLI](../scripts/qualify_raw_v2_prefix.py)는 전체 raw-v2 qualification을 완화하거나 대체하지 않는다.
목표는 **사전에 고정한 오전 종료 시각 이전의 prefix만 별도로 검증**하는 것이다.

계약은 seq=1부터 시작한다. `--end-market-second 36000`은 manifest의 market_date 기준
10:00:00 KST exclusive boundary다. 모든 prefix record의 공통 seq/source/session/received_ns와
UTC receipt ordering, event envelope를 검증한다. 그리고 cutoff 시각 이상에서 구조적으로 유효한
첫 record를 boundary sentinel로 반드시 읽는다. 이 sentinel이 없으면 수집이 해당 시각까지 도달했다는
근거가 없으므로 prefix도 실패한다.

prefix 내부 품질은 기존 qualification의 `_Diagnostics` 계약을 재사용한다.
`session_start/session_note` 외 control, parse_error, normalized issue는 현재 research 계약에 따라
prefix를 부적격으로 만든다. 반대로 sentinel 뒤의 tail은 의도적으로 읽지 않는다. 따라서 10:36 장애가
있더라도 10:00 이전 prefix 자체가 깨끗하고 10:00 이후 sentinel이 존재하면 prefix만 합격할 수 있다.
결과에는 반드시 다음을 분리해 남긴다.

- `prefix_structure_verified`
- `smoke_backtest_eligible` — 실행 파이프라인 smoke용이며 전략 성과 적격성은 아님
- `performance_research_eligibility.assessed=false`
- `prefix_event_sha256` — 이번 실행이 읽은 prefix bytes의 식별자이며 producer-stored checksum이 아님
- `scope.tail_scanned=false`
- `scope.whole_stream_assessed=false`
- `scope.whole_stream_research_eligible=null`
- boundary sentinel의 seq/수신시각

closed raw의 전체 event_count/payload_sha256는 이 경로에서 검증하지 않는다.
frozen incomplete manifest도 외부 종료 근거와 boundary sentinel이 있으면 prefix 판정 자체는 가능하지만,
그 결과가 전체 세션 완료/무결성을 의미하지 않는다.

파일 획득 안전성은 기존 sealed qualification과 동일하게 유지한다. Windows local NTFS,
write/delete handle 배제, source stat 불변, **모든 SQLite sidecar 부재**가 필요하다.
현재 보고된 운영 원본의 `-wal 0 / -shm 32768` 상태를 이 도구가 정리하거나 무시하지 않는다.
실제 50GB 적용 전에는 원본 비변경의 별도 frozen snapshot 확보 절차가 필요하다.

bounded scan은 producer의 whole-stream payload checksum을 검증하지 않으므로 성과 연구 적격성은 별도로 미평가다. 실제 성과 주장 전에는 전체 checksum 또는 사전에 기록된 immutable file-hash anchor 등 추가 provenance 계약이 필요하다.

prefix 시간대는 성과를 본 뒤 고르는 파라미터가 아니다. 예를 들어 09:00~10:00 전략을 연구한다면
10:00 cutoff를 성과 확인 전에 고정한다. 이후 tail 장애를 이유로 유리한 종료 시각을 사후 선택하지 않는다.


<a id="nxt-prefix-smoke"></a>
## 9. qualified prefix → NXT portfolio smoke

[smoke adapter](../engine/nxt_prefix_smoke.py)와
[CLI](../scripts/run_nxt_prefix_smoke.py)는 §8의 `smoke_backtest_eligible=true` 결과만 받는다.
목적은 **실제 raw-v2 형태의 bounded prefix가 NXT 공유계좌 경로에서 재현 가능하게 실행되는지** 확인하는 것이다.
전략 수익성·전체 세션 품질·실거래 준비를 판정하지 않는다.

실행은 prefix report에 기록된 raw path와 호출 경로가 정확히 같을 때만 허용한다.
같은 source를 sealed/no-sidecar 상태로 다시 열어 seq=1부터 boundary sentinel까지 재생하면서
manifest, `prefix_event_sha256`, consumed record count, prefix counts/quality diagnostics, sentinel을
qualification report와 다시 대조한다. qualifier 뒤 raw bytes가 달라졌거나 다른 복사본을 report에
끼워 넣으면 실행을 정상 완료로 인정하지 않는다.

검증을 통과한 prefix 안에서도 strategy에는 호출자가 명시한 `code=venue` 종목 tick만 전달한다.
control과 미선택 종목은 strategy 입력에서 빠지지만 prefix 재검증에는 계속 포함된다.
결과는 기존 `portfolio_research_result_v1`을 사용하며 input provenance에
`raw_v2_prefix_smoke_v1`, prefix report SHA-256/run id/digest/cutoff/sentinel,
선택 종목과 adapter code hash를 기록한다. 이 provenance도 외부 서명이나 raw identity 인증이 아니다.

결과의 `raw_identity_verified=false`, `realized_pnl/unrealized_pnl/equity=null`을 유지한다.
`purpose=smoke_backtest_only`, `whole_stream_assessed=false`,
`performance_research_assessed=false`가 핵심 해석 경계다.
같은 snapshot/report/settings의 재실행은 같은 reproducibility key와 fill/order 결과를 내야 한다.

현재 운영 50GB 원본에 보고된 sidecar가 하나라도 남아 있으면 이 경로도 시작하지 않는다.
§8 qualifier와 동일하게 **sidecar-free frozen snapshot**이 선행 조건이다.


<a id="frozen-snapshot-acquisition"></a>
## 10. raw-v2 frozen snapshot acquisition — residue 원본 비변경 후보

[acquisition library](../collector/raw_v2_snapshot.py)와
[CLI](../scripts/acquire_raw_v2_snapshot.py)는 기존
[synthetic clone lab](../scripts/lab_raw_v2_clone.py)의 보호 계약을 실제 입력용으로 좁게 옮긴다.
lab 자체는 계속 외부 DB 인자를 거부하며 production 실행에 사용하지 않는다.

현재 production 후보가 허용하는 source shape는 명시적
`zero-wal-32768-shm-v1` 하나뿐이다.

- main: single-link regular file, 최대 64 GiB
- `-wal`: 존재 + 정확히 0 bytes
- `-shm`: 존재 + 정확히 32,768 bytes
- `-journal`: 부재
- source/output: Windows local NTFS, reparse 경로 거부
- output free space: source file-set 총량의 2배 + 2 GiB 이상

다른 WAL 크기, rollback journal, SHM 크기, hardlink/alias는 cleanup 후보가 아니라 즉시 거부한다.

acquisition 동안 source main/WAL/SHM은 모두 sharing=0 read handle로 동시에 잡는다.
이미 열린 reader/writer가 있으면 WinError 32로 fail-closed한다. source parent와 output/run/evidence/working
directory identity도 pin하며, 후발 writer가 기존 source member를 다시 여는 것을 막는다.
directory pin이 모든 새 child name 생성을 원천 배제한다고 주장하지 않으므로 acquisition 끝에서
source file-set/stat을 다시 확인한다.

source SQLite는 열지 않는다. 각 held source member를 한 번 읽으며 SHA-256을 계산하고 같은 block을
fresh `evidence/`와 `working/` 두 파일에 동시에 쓴 뒤 fsync한다.
source main/WAL/SHM의 exclusive handle은 working cleanup·hash·최종 source 재검증이 끝날 때까지 유지한다.
그 상태에서 별도 working main만 SQLite로 열어 metadata 한 페이지를 읽고 명시적으로 닫는다.
이는 copied WAL/SHM의 managed cleanup 후보이며 source SQLite에는 적용되지 않는다.

snapshot ready 조건:

1. expected session id와 working manifest 일치
2. working copy의 WAL/SHM/journal 모두 부재
3. working main 전체 readback SHA-256 == sealed source stream main SHA-256
4. evidence WAL/SHM 독립 readback hash == sealed source stream hash
5. source stat/identity가 exclusive acquisition 전후 동일

50 GiB evidence main은 추가 전체 readback을 하지 않는다. 그 hash는 sealed source stream을 읽으면서
두 destination에 동시에 기록한 digest다. 실제 연구 입력은 evidence가 아니라 독립 readback을 마친 working main이다.

성공 report도 `whole_stream_assessed=false`, `research_eligible=false`,
`performance_research_eligible=false`를 유지한다. 의미는 오직
`snapshot_ready_for_prefix_qualification=true`다.
그 다음 단계가 [bounded prefix qualification](#bounded-prefix-qualification)이며,
그 다음이 NXT prefix smoke다. snapshot 성공을 FIRST_RESEARCH_CANDIDATE나 전략 성과로 해석하지 않는다.

실패/중단 시 partial evidence/working/result를 삭제하지 않는다. source 자동 cleanup, checkpoint,
재시도, 원본 교체, sidecar unlink는 제공하지 않는다.


<a id="zero-quote-policy-experiment"></a>
## 11. one-sided zero quote synthetic policy experiment

[experimental classifier](../collector/zero_quote_policy_experiment.py)는 2026-09-21 오전 prefix의 bounded evidence에서
관측한 한쪽 zero quote 패턴을 **dataset corruption이 아닌 missing/non-executable market-state 후보로 표현할 수 있는지**만 합성 검증한다.
기존 `tick_normalizer`, `raw_v2_prefix_qualification`, `run_nxt_prefix_smoke`의 동작과 eligibility는 바꾸지 않는다.

candidate는 다음 조건을 모두 만족할 때만 성립한다.

- event kind = quote
- `normalization=kiwoom_fids_prototype_1`
- `price_policy=signed_magnitude`
- issue는 정확히 하나: `out_of_range_fid_41` 또는 `out_of_range_fid_51`
- 문제 side의 top3 raw price가 모두 정확히 `-0`
- 같은 side top3 raw size가 모두 numeric zero
- normalized top1 price는 null, top1/top3 size는 zero
- 반대편 raw top1 price와 normalized top1 price/size는 positive
- 바로 다음 parse_error를 pair로 볼 때 seq+1, received_ns, code, issues가 정확히 일치

양쪽 zero, 다른 issue 혼합, 잔량 양수, 반대편 book 부재, 다른 price policy는 모두 disqualifying이다.
`trade_direction_unverified`도 이 실험의 대상이 아니며 계속 disqualifying이다.

중요하게, candidate quote는 execution eligibility가 아니다.
[quote validation](../execution/quote_validation.py)은 normalized bid/ask 중 한쪽이 null인 candidate를 계속
`invalid_bid` 또는 `invalid_ask`로 거부한다. 따라서 이 실험이 향후 quality classification 완화를 지지하더라도
체결 가격 fallback, one-sided fill, fake liquidity를 허용하지 않는다.

이 v0는 provider 문서상 `-0` 의미를 확정하지 않고, 실제 prefix 전체에 적용하지 않으며,
`smoke_backtest_eligible`를 변경하지 않는다. 다음 단계가 있다면 별도 opt-in smoke-quality policy에서
zero-quote pair와 진짜 disqualifying issue를 분리하는 설계를 먼저 검증해야 한다.


<a id="zero-quote-smoke-policy"></a>
## 12. opt-in zero-quote smoke-quality evaluator candidate

[policy evaluator](../collector/zero_quote_smoke_policy.py)는 §11 classifier를 이용해 ordered envelope stream을
smoke-quality 관점에서만 분류하는 후보 구현이다. 아직 raw-v2 prefix reader나 NXT smoke에 연결되지 않았다.

정확히 다음 두 record가 연속일 때만 quarantine한다.

1. §11의 strict one-sided zero-quote candidate tick
2. 바로 다음 seq의 mirrored `parse_error` — same received_ns/code/issues

quarantine은 quality classification의 후보일 뿐 execution permission이 아니다.
기존 quote validation은 해당 quote를 계속 `invalid_ask` / `invalid_bid`로 거부한다.

다음은 모두 disqualifying 상태로 남긴다.

- `trade_direction_unverified`
- zero-quote candidate 뒤 exact mirrored parse_error 부재
- mismatched seq/received_ns/code/issues
- extra normalized issue
- callback/disconnect/queue overflow 등 unsafe control
- classifier가 인정하지 않는 nearby zero pattern

policy result의 `smoke_quality_eligible`는 이 synthetic evaluator에 입력된 stream에 대해서만 의미한다.
현재 `raw_v2_prefix_qualification_v1.smoke_backtest_eligible`이나 실제 NXT smoke gate를 바꾸지 않는다.
실데이터 prefix에 연결하기 전 focused local regression을 먼저 통과해야 한다.


<a id="selected-instrument-smoke-quality"></a>
## 13. selected-instrument smoke-quality pure evaluator candidate

[policy evaluator](../collector/selected_instrument_smoke_policy.py)는 단일 전략/선택 종목 smoke를 위해
**whole-prefix research quality와 selected-strategy input quality를 분리**하는 합성 정책 후보다.
아직 raw-v2 reader, prefix report schema, NXT smoke gate에는 연결하지 않는다.

전체 ordered stream을 모두 관측하며 다음 구조 계약을 자체 확인한다.

- common seq는 1부터 contiguous
- received_ns는 nondecreasing
- source/session identity는 하나
- unsafe control은 종목과 관계없이 전체 차단

선택 종목은 호출자가 명시한 `CODE=VENUE` mapping으로 판정한다.

선택 종목:
- clean tick 허용
- §11의 strict one-sided zero-quote tick + exact mirrored parse_error pair만 quarantine
- `trade_direction_unverified` 및 다른 normalized issue는 exact pair여도 disqualifying

비선택 종목:
- 실제 2026-09-21 오전 prefix에서 관측된 세 issue
  `out_of_range_fid_41`, `out_of_range_fid_51`, `trade_direction_unverified`
  에 한해 exact mirrored parse_error pair이면 selected-strategy smoke에는 비영향으로 분리한다.
- unknown issue는 exact pair여도 fail-closed다.
- unpaired/mismatched issue는 항상 fail-closed다.

이 evaluator의 `selected_smoke_quality_eligible`는 선택 종목 pipeline smoke 목적에만 쓰는 후보 값이다.
`whole_prefix_research_quality_upgraded=false`, `strict_prefix_qualification_unchanged=true`,
`nxt_smoke_gate_unchanged=true`를 명시한다.

zero-quote quarantine은 execution 허가가 아니다. 기존 quote validation은 한쪽 price=null인 book을 계속
`invalid_ask` / `invalid_bid`로 거부한다.

실데이터에 연결하기 전 focused local regression을 먼저 통과해야 하며,
이 단계에서는 실제 50GB prefix 재스캔이나 selected instrument 결과를 생성하지 않는다.


<a id="selected-prefix-overlay"></a>
## 14. strict prefix 위 selected-instrument quality overlay candidate

[selected prefix overlay](../collector/raw_v2_selected_prefix_qualification.py)와
[CLI](../scripts/qualify_raw_v2_selected_prefix.py)는 기존
`raw_v2_prefix_qualification_v1` 결과를 수정하거나 대체하지 않는다.

입력은 다음 세 가지다.

- strict prefix report
- 그 report가 가리키는 정확히 같은 raw path
- 명시적 `CODE=VENUE` selected instrument mapping

overlay는 strict report가 이미 `prefix_structure_verified=true`인 경우에만 시작한다.
strict report의 `smoke_backtest_eligible`은 true일 필요가 없다. 즉 whole-prefix quality가
엄격 정책에서 실패했더라도 구조적으로 검증된 prefix라면 selected-policy를 별도로 평가할 수 있다.

overlay는 같은 raw를 Windows local NTFS + no-sidecar + write/delete exclusion 상태로 다시 읽고
prefix 전체에 대해 기존 strict diagnostics를 재계산한다. 다음 값이 strict report와 모두 같아야 한다.

- manifest
- prefix_event_sha256
- records_consumed_through_sentinel
- boundary sentinel
- counts
- quality_diagnostics

하나라도 다르면 selected overlay 자체가 실패한다.

그 동일 ordered prefix에 §13의 `SelectedInstrumentSmokePolicy`를 병렬 적용한다.
따라서 비선택 종목의 whitelisted exact pair를 selected-strategy 관점에서 분리하더라도
strict report의 원래 quality failure는 그대로 보존된다.

새 report는 다음을 명시한다.

- schema = `raw_v2_selected_prefix_qualification_v1`
- `selected_prefix_structure_verified`
- `selected_smoke_quality_eligible`
- selected policy result
- strict report path / SHA-256 / run id
- strict revalidation 결과
- `whole_prefix_research_quality_upgraded=false`
- `strict_prefix_qualification_unchanged=true`
- `nxt_smoke_gate_unchanged=true`
- `performance_research_eligibility.assessed=false`

strict report SHA-256은 content backreference이지 외부 서명이나 파일 immutability 증명은 아니다.
selected overlay가 true여도 기존 `run_nxt_prefix_smoke.py`는 여전히 strict
`smoke_backtest_eligible=true` report만 받는다. selected overlay용 smoke runner는 아직 없다.

실제 50GB에 적용하기 전 Windows synthetic focused regression을 먼저 통과해야 한다.


<a id="selected-quality-bounded-examples"></a>
## 15. selected disqualifying bounded examples candidate

[SelectedInstrumentSmokePolicy](../collector/selected_instrument_smoke_policy.py)는 selected issue의 eligibility를
바꾸지 않고, selected disqualifying record를 최대 10건까지 diagnostic example로 보존하는 후보 변경을 둔다.

example에는 다음만 기록한다.

- tick seq / paired control seq
- received_ns / received_at_utc / exchange_ts_raw
- code / venue / kind / market_second
- issue list / 진단 reason
- 관련 FID subset: 10, 14, 15, 20, 21, 27, 28, 41, 51
- kind별 relevant normalized fields: trade는 price/volume/is_buy, quote는 bid/ask/bid_size/ask_size
- paired_parse_error 여부

이 필드는 selected smoke-quality 판정에 사용되지 않는다.
`MAX_SELECTED_DISQUALIFYING_EXAMPLES=10`으로 bounded하며 raw 전체 FID dict나 payload를 복제하지 않는다.
목적은 실제 selected-prefix overlay에서 소수 selected blocker를 다시 50GB 전체 검색 없이 식별하는 것이다.


<a id="direction-window-quarantine-policy"></a>
## 16. unknown-direction recent-window quarantine policy candidate

[direction feature helper](../strategies/nxt_breakout/direction_window.py)는
실제 005930 blocker에서 확인된 `is_buy=None`을 buy/sell로 추론하거나 drop하지 않고,
NXT의 최근 trade 방향 feature가 언제 다시 사용 가능한지만 정의하는 pure policy 후보다.

정책 `unknown_direction_recent_window_quarantine_v0`:

- recent window에는 모든 trade의 `(volume, is_buy)`를 관측 순서대로 보존한다.
- `is_buy=None`도 volume과 함께 window에 남는다.
- unknown direction이 하나라도 window 안에 있으면:
  - total_volume은 관측값으로 유지
  - buy_volume은 `None`
  - buy_ratio는 `None`
  - `entry_direction_eligible=false`
- unknown이 deque에서 자연스럽게 빠진 뒤에만 fully-known window에서 volume-weighted buy ratio를 다시 계산한다.
- unknown을 0, buy, sell로 치환하거나 window에서 즉시 제거하지 않는다.

현재 전략의 `recent_ticks=15`에서 unknown trade가 막 들어온 경우,
14개의 후속 trade까지는 unknown이 window에 남고 15번째 후속 trade가 들어온 직후 밀려난다.
이 정책은 15개가 완전히 찰 때까지 기다리는 새 warm-up 규칙을 추가하지 않는다.
현재 전략처럼 관측 tick 수가 1~14개라도 모두 known이면 ratio 계산은 가능하다.

이 helper는 아직 `NxtResearchStrategy`에 연결되지 않았다.
기존 strategy의 unknown-direction strict rejection, selected-prefix quality gate,
NXT smoke runner는 모두 그대로다. integration은 별도 후속 단계다.


<a id="nxt-direction-quarantine-integration"></a>
## 17. NXT strategy opt-in unknown-direction quarantine integration candidate

[NXT research strategy](../strategies/nxt_breakout/tick_research.py)는 기존 strict 동작을 default로 유지하면서
§16의 direction-window policy를 **명시적 opt-in**으로만 사용할 수 있는 후보 integration을 둔다.

새 strategy 설정은 `unknown_direction_policy`다.

- default: `strict`
  - 기존과 동일하게 trade의 `is_buy`는 반드시 bool
  - `None`이면 즉시 입력 오류
- opt-in: `unknown_direction_recent_window_quarantine_v0`
  - valid price + positive volume + `is_buy=None`을 관측으로 수용
  - price/volume 기반 open, premarket volume, breakout price window, held-position peak는 그대로 갱신
  - recent trade deque에도 `(volume, None)`을 그대로 보존
  - §16 DirectionFeatureWindow에도 같은 관측을 넣음
  - unknown이 window 안에 있으면 entry 방향 feature unavailable이므로 신규 entry만 차단
  - unknown이 자연스럽게 window 밖으로 밀린 뒤 buy ratio 계산 재개

이 모드는 unknown trade를 삭제하거나 buy/sell로 추론하지 않는다.
exit logic도 unknown 자체로 새 방향 정보를 만들지 않는다.

[portfolio adapter](../strategies/nxt_breakout/portfolio_adapter.py)와
[portfolio runner](../engine/nxt_portfolio_research.py)는 같은 `unknown_direction_policy`를 전달하고,
strategy settings에 값을 보존한다. `direction_window.py`도 strategy code SHA-256 집합에 포함한다.
따라서 strict/quarantine이 우연히 같은 intent를 내더라도 설정과 reproducibility key로 구분된다.

**아직 연결하지 않는 경계:**
- selected-prefix quality gate는 `trade_direction_unverified`를 계속 disqualifying
- strict raw-v2 prefix qualification은 변경 없음
- 기존 `run_nxt_prefix_smoke()`는 strict prefix gate를 계속 요구
- selected overlay를 소비하는 smoke runner는 아직 없음

따라서 이 integration이 통과해도 실제 005930 smoke 허가는 아니다.


<a id="selected-direction-quarantine-gate-v2"></a>
## 18. selected-prefix unknown-direction quarantine gate v2 candidate

PR #41의 `unknown_direction_recent_window_quarantine_v0`와 PR #42의 strategy integration은
기존 strict 동작을 default로 유지한 채 master에 통합됐다.
이번 v2 gate 후보는 [selected policy](../collector/selected_instrument_smoke_policy.py)와
[selected prefix overlay](../collector/raw_v2_selected_prefix_qualification.py)가
**같은 explicit policy ID**를 사용하도록 정렬한다.

### Default strict

`unknown_direction_policy="strict"`가 default다.
기존 selected `trade_direction_unverified` exact pair는 계속 disqualifying이다.
따라서 과거 v1 실제 결과를 새 정책으로 재해석하지 않는다.

### Explicit quarantine candidate

`unknown_direction_policy=unknown_direction_recent_window_quarantine_v0`일 때도
selected direction issue를 무조건 격리하지 않는다.
다음을 모두 만족해야 한다.

- issue가 정확히 `trade_direction_unverified` 하나
- event kind = trade
- normalized price가 finite positive
- normalized volume이 positive int
- normalized `is_buy=None`
- exact mirrored parse_error: seq+1 / same received_ns / same code / same issues
- `normalization=kiwoom_fids_prototype_1`
- `real_type=주식체결`
- `price_policy=signed_magnitude`
- `direction_policy=signed_volume`
- raw FID15가 명시적 `+`/`-`가 없는 양의 정수 문자열
- FID15 정수값 == normalized volume

signed FID15, raw/normalized volume mismatch, 다른 normalization/real_type/price/direction policy는
opt-in이어도 fail-closed다.

candidate pair는 `selected_unknown_direction_pairs`로 별도 계수한다.
이는 해당 trade 직후 entry permission이 아니다.
report는 `unknown_direction_immediate_entry_permission_granted=false`와
`selected_unknown_direction_requires_strategy_window_quarantine=true`를 명시하며,
실제 신규 진입 차단/재개 시점은 PR #42 strategy의 recent-window 정책이 소유한다.

### Overlay schema/provenance

selected overlay 의미가 policy에 따라 달라지므로 새 결과 schema 후보는
`raw_v2_selected_prefix_qualification_v2`다.

v2 report는:
- explicit `unknown_direction_policy`
- strict report path/SHA/run id
- strict prefix 재검증 결과
- selected policy result
- direction-window helper SHA
- NXT consumer strategy `tick_research.py` SHA
를 기록한다.

기존 strict `raw_v2_prefix_qualification_v1` 결과는 수정하지 않는다.
기존 실제 selected overlay v1 결과도 역사 근거로 유지한다.
v2 selected quality가 true여도 기존 `run_nxt_prefix_smoke()`는 여전히 strict report만 받으므로
실제 NXT smoke는 별도 selected-overlay consumer가 생기기 전까지 열리지 않는다.


<a id="selected-prefix-v2-smoke-runner"></a>
## 19. selected-prefix v2 NXT smoke runner candidate

[new selected smoke runner](../engine/nxt_selected_prefix_smoke.py)와
[CLI](../scripts/run_nxt_selected_prefix_smoke.py)는
§18의 eligible `raw_v2_selected_prefix_qualification_v2`를 소비하는 별도 pipeline-smoke 후보 경로다.

기존 [strict prefix smoke](../engine/nxt_prefix_smoke.py)는 수정하지 않는다.
strict runner는 계속 `raw_v2_prefix_qualification_v1.smoke_backtest_eligible=true`만 받는다.

### Input gate

selected runner는 다음을 모두 요구한다.

- selected report schema = `raw_v2_selected_prefix_qualification_v2`
- status completed / stream_error null
- selected prefix structure verified
- selected smoke quality eligible
- policy = `unknown_direction_recent_window_quarantine_v0`
- performance research assessed=false / eligible=null
- selected overlay strict-preservation contracts 유지
- selected quarantine의 immediate entry permission=false
- strategy-window quarantine required=true
- selected report의 current policy/strategy code provenance가 현재 코드와 정확히 일치
- selected report가 가리키는 strict report 파일 SHA/run id/scope/digest가 그대로 유지
- raw path와 simulator instrument mapping이 report와 정확히 일치
- 현재는 정확히 한 selected instrument만 허용

### One bounded raw scan

runner는 output strategy replay와 동시에 같은 raw prefix를 한 번 다시 읽는다.

전체 prefix에서는:
- seq/source/session/receipt ordering
- manifest
- strict diagnostics
- prefix SHA-256
- consumed-through-sentinel count
- boundary sentinel
- selected policy result
를 재계산한다.

EOF에서 strict evidence와 selected policy result가 report와 완전히 같아야 한다.
raw bytes, strict report, selected report policy result가 뒤늦게 바뀌면 정상 smoke가 아니라 failed diagnostics로 끝난다.

### Selected strategy input transformation

selected clean tick:
- 그대로 strategy input으로 전달

selected unknown-direction quarantine pair:
- issue tick은 즉시 전달하지 않고 바로 다음 exact mirrored parse_error를 확인
- v2 policy가 `selected_unknown_direction_pairs`로 인정한 경우 원 trade를 원 seq/received_ns,
  `is_buy=None` 그대로 strategy input으로 전달
- PR #42 strategy recent-window quarantine이 entry 차단/재개를 담당

selected one-sided zero-quote quarantine pair:
- exact mirrored pair를 확인한 뒤 strategy input에서는 제외
- execution permission을 부여하지 않으며 이전 valid quote를 fake replacement로 생성하지 않음

다른 selected issue, unapproved issue, unpaired/mismatch, unsafe/global issue:
- fail-closed

### Provenance / limits

result의 `input_provenance.kind`는 `raw_v2_selected_prefix_smoke_v1`이다.
다음을 보존한다.

- selected report path/SHA/run id/schema
- strict report path/SHA/run id
- strict smoke eligibility 원값
- prefix digest/cutoff/sentinel
- selected instruments
- unknown-direction policy
- selected policy result
- expected selected strategy-input event count
- withheld selected zero-quote pair count
- forwarded selected unknown-direction pair count
- selected smoke adapter code SHA

이 경로도 `whole_stream_assessed=false`, `performance_research_assessed=false`,
`raw_identity_verified=false`를 유지한다.
성공은 pipeline smoke 실행 가능성일 뿐 수익성, NXT venue 인증, whole raw 품질, live trading 승인이 아니다.


<a id="selected-v2-research-input-gate"></a>
## 20. selected-v2 bounded research-input gate decision

2026-09-24 실제 selected-v2 pipeline smoke, 대표 거래 raw audit, 동일 설정 재현성 rerun까지 완료한 뒤
다음 scope를 **bounded selected strategy-research input**으로 승인했다.

`2026-09-21 / 10:00 KST exclusive / 005930=unknown /
raw_v2_selected_prefix_qualification_v2 /
unknown_direction_recent_window_quarantine_v0`

이 승격은 `performance_research`가 아니다.

허용되는 것은 입력/정규화/policy/strategy/execution path의 회귀,
신호→주문→체결 causal analysis, 향후 accounting 구현의 실제 bounded fixture,
명시적으로 bounded라고 표시한 behavior exploration이다.

다음은 계속 금지한다.

- 이 한 사례의 수익성/우수성/robustness 주장
- 결과를 본 뒤의 parameter/threshold/exit 최적화
- whole-file FIRST_RESEARCH_CANDIDATE 승격
- NXT venue 인증
- whole raw 품질 승인
- live trading 승인

performance-research를 아직 열지 않는 핵심 이유는
whole stream 미평가, venue unknown, 단일 날짜/종목/왕복 사례,
평가용 PnL/equity 계약 부재, 학습/평가 분리 및 민감도/다시장 검증 부재다.

따라서 다음 구현 단계는 actual result의 현금 차이를 성과로 해석하는 것이 아니라,
**실현 PnL·미실현 PnL·equity와 marking 규칙을 합성 trace에서 먼저 고정하는 performance-accounting contract**다.


<a id="portfolio-performance-accounting"></a>
## 21. portfolio performance-accounting pure contract candidate

[accounting helper](../execution/portfolio_accounting.py)는
기존 `PortfolioSimulator`의 현금·수량·fill 원장을 바꾸지 않고,
`PortfolioFill` 시퀀스에서 performance accounting을 **순수 계산**하는 후보 계층이다.

### Cost basis / realized PnL

v0 cost-basis method는
`weighted_average_fee_inclusive_v0`다.

- long-only만 허용
- buy gross + 실제 buy fee를 position cost basis에 포함
- sell 때 기존 cost basis를 보유수량 비율로 정확히 배분
- realized PnL = sell gross − 실제 sell fee − released cost basis
- sell quantity > held quantity는 fail-closed
- fill cash delta도 독립적으로 재계산

평균원가는 수량 가중 계산 중 비종결소수가 나올 수 있으므로
내부 산술은 binary float/ambient Decimal rounding이 아니라 `fractions.Fraction`으로 exact하게 유지한다.

### Marking / unrealized / equity

v0 marking policy는
`explicit_fresh_valid_bid_gross_of_hypothetical_exit_fee_v0`다.

helper가 market data freshness를 스스로 추론하지 않는다.
상위 integration이 fresh + valid quote임을 확인한 **explicit bid mark**만 전달해야 한다.

- open position market value = bid mark × quantity
- unrealized PnL = market value − remaining fee-inclusive cost basis
- equity = current cash + marked open-position value
- total PnL = realized + unrealized
- mark에는 hypothetical future sell fee를 빼지 않음
- 실제 sell fee는 실제 sell fill에만 반영

open position 중 하나라도 mark가 missing/None이면:
- `unrealized_pnl=None`
- `total_pnl=None`
- `equity=None`
- unpriced code를 명시

last trade, stale quote, zero, fabricated close를 fallback으로 쓰지 않는다.

### Reconciliation

helper는 fill cashflow로
`expected_cash_from_fills = initial_cash + cash_delta`를 계산한다.

caller가 준 current cash와 다르면 수정하지 않고
`cash_reconciled=false`로 남긴다.

모든 mark가 있을 때는
`realized + unrealized == equity - initial_cash`도 별도 accounting identity로 확인한다.

이번 단계는 pure contract + synthetic regression만 추가한다.
`PortfolioSimulator.snapshot()`, portfolio result schema, selected-v2 actual result의
`realized_pnl/unrealized_pnl/equity`는 아직 변경하지 않는다.


<a id="portfolio-accounting-result-integration"></a>
## 22. NXT result performance-accounting subrecord integration candidate

PR #46의 pure accounting contract를 기존 execution semantics와 분리한 채
[NXT portfolio runner](../engine/nxt_portfolio_research.py)의 **result finalization에만** 연결하는 후보 단계다.

기존 `PortfolioSimulator`는 수정하지 않는다.
주문 admission, reservation, matching, latency, liquidity, cancellation, fill 생성과 cash mutation은 그대로다.

finalizer는 저장된 account fill ledger를 `PortfolioFill`로 재구성하고
`account_portfolio_fills()`로 cost basis/realized/cashflow를 독립 계산한다.
그 결과와 snapshot의 final cash/positions를 비교해:

- `cash_reconciled`
- `position_reconciled`
- `accounting_identity_reconciled`

를 report에 보존한다. 차이가 있어도 숫자를 repair하지 않는다.

새 subrecord schema 후보:

`portfolio_performance_accounting_v1`

### Flat result

final position이 전부 0이면 final market mark가 필요하지 않으므로:

- status = `flat_complete`
- realized PnL 확정
- unrealized = 0
- total PnL = realized
- equity = current cash
- accounting identity 확인

이 가능하다.

### Open position

현재 `run_portfolio` result에는 final quote freshness/validity를 인증한 bid mark가 별도 provenance로 남지 않는다.
따라서 open position에서는 mark를 추론하지 않는다.

- status = `open_unpriced`
- mark_integration = `not_connected`
- realized PnL은 fill ledger에서 계산 가능
- unrealized/total/equity = null
- `unpriced_codes` 기록

last trade, stale quote, final observed quote를 자동 mark로 재사용하지 않는다.

### Legacy fields / reproducibility

기존 top-level:

- `realized_pnl`
- `unrealized_pnl`
- `equity`

는 이번 단계에서도 null을 유지한다.
기존 schema 의미를 조용히 바꾸지 않고 accounting subrecord를 먼저 검증하기 위함이다.

`execution/portfolio_accounting.py` SHA와 `performance_accounting` subrecord는
NXT result reproducibility identity에 포함한다.

실패 run도 partial execution report를 유지한 채 accounting subrecord를 추가할 수 있지만,
`status=failed` / `diagnostics_only=true` 의미를 바꾸지 않는다.

이번 단계가 통과해도 actual bounded result를 performance-certified 결과로 승격하지 않는다.
open-position fresh-bid mark integration과 legacy/top-level schema migration은 별도 후속 단계다.
