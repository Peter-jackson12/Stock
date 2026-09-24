# 첫 실데이터 시험 백테스트 체크리스트

기준일: 2026-09-21. 목표는 **검증 가능한 실제 입력 하나로 원본 틱 → 신호 → 주문 → 체결 → 결과를 대조**하는 것이다.
전략 수익성 입증과 실거래 운영 승인은 별도다. 원본 틱 재생이 주 경로이며 LOB/초봉 변환은 선행 조건이 아니다.

이 문서는 현재 체크 항목과 후보 판정만 유지한다. 실행·결과 계약은 [틱 연구 런북](TICK_RESEARCH_RUNBOOK.md),
저장 종료는 [수집 런북](docs/COLLECTION_RUNBOOK.md), 작업 인계는 [HANDOFF](HANDOFF.md)를 따른다.
과거 파일 5개의 상세 조사와 의사결정 원문은 [보존본 안내](docs/archive/README.md)에 있다.

## 1. 이미 준비된 것

- [x] 닫힌 raw v2 읽기·순번/체크섬 검증·종목/venue 하나의 연구 실행과 JSON 저장.
  [연구 코드](engine/tick_research_run.py), [CLI](scripts/run_tick_research.py), [합성 회귀](tests/test_raw_v2.py).
- [x] 실패·미청산·체결 없음의 구분과 완료 근거 조회.
  [조회 코드](scripts/inspect_tick_research.py), [결과 계약](TICK_RESEARCH_RUNBOOK.md#결과-읽기).
- [x] 9월 18일 세션의 종료 근거와 시작/중간/말미 총 300 records 표본 대조.
  비교 대상 291 ticks의 재계산 불일치 0건이나, 말미 방향 미확인 8 trades와 대응 parse_error 8건이 있다.
  근거: `f64c37c`, `operations_state/raw_sample_checks/a25fe6721db94d8cb2724ee2798a8ad7/`.
  같은 표본을 이유 없이 반복하지 않는다. 전체 무결성·무누락·연구 합격은 미확인이다.
- [x] 2026-09-20 결정: 새 clean closed 세션을 기본 경로 A로 선택, 기존 raw 파생 경로 B는 예비.
  근거: [정리 전 기록](docs/archive/README.md). 9월 21일 경로 A 실행 후에도 품질 문제가 재관측됐다.
  다음 행동은 무조건 재수집이 아니라 검사 전용 진단 준비다. 경로 B의 자동 승인은 아니다.

## 2. 가장 먼저 할 일 — 시험 입력 결정

**whole-file strict 연구 입력으로 바로 사용할 수 있다고 판정한 실제 raw 후보는 아직 없다.**
다만 2026-09-21 세션의 frozen working snapshot에서 10:00 KST bounded prefix + `005930=unknown`에 한정한
**selected-v2 pipeline-smoke 입력 경로**는 별도 증거 체계로 검증됐다.

이 selected-v2 경로는 whole raw 승격이 아니다. strict whole/prefix quality failure를 유지한 채
명시적 `unknown_direction_recent_window_quarantine_v0` 정책, selected v2 overlay,
전용 smoke runner가 같은 evidence를 다시 검증하며 선택 종목만 pipeline smoke에 사용한다.

아래 기존 whole-file 후보 표는 그대로 유지한다.

| session_id | 확인된 제한 | 현재 처리 |
|---|---|---|
| `cf18cb437b9a4f6ba2abf0fdadbbfe57` (09-17) | direction_policy=unknown. 제한 조사 10,384건 중 구 방향 판정과 FID15 기반 방향이 891건(8.6%) 불일치 | 첫 기본 입력 제외. 최초 100건 검사를 필수 작업으로 재등록하지 않음 |
| `21f8c124e64e421893275ccdc83818ad` (09-18) | signed_volume 수집이나 말미 표본의 방향 미확인 8 trades + 대응 품질 기록 8건 | 현행 엄격 whole-file 연구 경로 차단. 종목 선택으로 우회하지 않음 |
| `6f39117671c048f6b60477ceafbf40b6` (09-21) | 종료 증거 일관, 말미 표본에 방향 미확인 5 trades + 대응 parse_error 5건 | FIRST_RESEARCH_CANDIDATE 미승격. 원본 보존·검사 전용 진단 준비 |
| 나머지 과거 3개 | 사실상 빈 파일 또는 정상 종료 미확인 | 후보 제외. 세부 session/상태는 보존본 유지 |

8.6%는 해당 제한 조사에만 해당하며 전체 오류율·성과 영향률이 아니다.
문제 tick과 대응 parse_error를 독립 체결 오류로 이중 집계하지 않는다. 표본 밖 문제 부재를 주장하지 않는다.
표본 검사기는 저장 당시 정책을 재적용하므로 unknown 파일의 sample_consistent가 현 signed_volume 적합성을 인증하지 않는다.

### 2026-09-21 신규 세션의 제한 조사

대상 identity·파일 크기/해시·종료/sidecar 근거는 [현재 인계](HANDOFF.md)에 둔다.
수집 code_revision과 보고 당시 HEAD/origin/master는 `4821762fd93230b658339fee084d6c08e3e53ce9`다.

| 표본 | 실제 seq | records / ticks | 결과 / exit |
|---|---|---|---|
| 최초 | 1–100 | 100 / 99 | mismatch 0, quality issue 0 / 0 |
| 중간 | 21,418,396–21,418,495 | 100 / 100 | mismatch 0, quality issue 0 / 0 |
| 말미 | 42,836,692–42,836,791 | 100 / 95 | mismatch 0, 방향 미확인 5 + 대응 parse_error 5 / 2 |

세 실행 모두 full_integrity_verified/feed_accuracy_verified/replay_performed=false다.

| trade seq / 대응 parse_error seq | 종목 | 원문 FID15 | 원문 FID20 | 수신 KST |
|---|---|---|---|---|
| 42,836,748 / 42,836,749 | 009730 | `" 1701"` | `"153210"` | 15:32:12.785501 |
| 42,836,772 / 42,836,773 | 131290 | `" 3855"` | `"153230"` | 15:32:32.788804 |
| 42,836,774 / 42,836,775 | 104040 | `" 412"` | `"153230"` | 15:32:32.788804 |
| 42,836,782 / 42,836,783 | 099220 | `" 611"` | `"153233"` | 15:32:35.775518 |
| 42,836,789 / 42,836,790 | 078860 | `" 10339"` | `"153239"` | 15:32:41.783954 |

추가 원문/주변 조회 범위는 42,836,745–751, 42,836,769–777, 42,836,779–792다.
주변에 비교 가능한 다른 trade는 없었다는 보고이며 가격 FID 부호를 FID15 방향 근거로 쓰지 않는다.
FID20도 15:32여서 “15:30 체결의 2분 지연 수신”을 지지하지 않는다.
로컬 명세에 무부호 FID15 예외/보정 근거가 없다는 보고다. 시장 경계와 시간상 인접하지만
말미 선택 표본이므로 전체 발생 분포/원인을 추론하지 않는다. 체결 유형은 분류 미확정이다.

**계수 대조 — 전체 SQL 집계가 아닌 코드 기반 예상:**

```text
42,836,791 final_seq - 42,796,226 committed_callbacks = 40,565
40,565 - 1 session_start = 40,564
```

[CaptureSession](collector/kiwoom/capture_session.py)은 시작 제어 기록 1건, 정상 콜백별 tick 1건,
issues가 있는 tick별 parse_error 1건을 쓴다. [QueuedCapture](collector/kiwoom/queued_capture.py)는
raw seq와 별도로 콜백 단위 committed 수를 늘리며 중단 경로는 정상 closed로 만들지 않는다.
[운영 backend](collector/kiwoom/live_capture.py)가 이 경로를 사용한다.
보고된 정상 실행과 코드 계약이 그대로라면 parse_error는 40,564건으로 예상된다.
이는 DB 집계가 아니며 다른 추가 제어 기록/계수 불일치 여부도 전체 검사에서 대조해야 한다.
40,564건 전부가 무부호 trade라는 뜻도 아니다. quote FID/다른 품질 사유일 수 있다.
“전체 문제는 말미 5건뿐” 또는 “15:30 절단으로 합격”이라고 가정하지 않는다.

별도 진단 파일을 만들지 않았다는 로컬 보고다. 후속 evidence 보존 시 전달 보고와 실제 원시 파일을 구분한다.

### 2026-09-21 selected-v2 bounded 경로 — 실제 pipeline smoke 완료

whole-file strict candidate와 구분하는 별도 bounded 경로다.

- Frozen working snapshot과 10:00 KST exclusive prefix 사용.
- strict prefix는 `smoke_backtest_eligible=false`, whole-stream 미평가 상태를 유지한다.
- selected v2 overlay:
  - instrument `005930=unknown`
  - policy `unknown_direction_recent_window_quarantine_v0`
  - selected tick 77,558 / clean 77,557
  - unsigned-direction quarantine pair 1
  - selected disqualifying 0
  - selected smoke quality eligible=true
- 실제 selected-v2 NXT pipeline smoke:
  - 설정: quantity 1 / cash 1,000,000 / fee 0.001 per-side /
    buy·sell·cancel latency 각 1초 / max quote age 2초 / cooldown 10초 / fixed exit
  - expected / actual / processed event 77,558 / 77,558 / 77,558
  - signal buy 1 / sell 1
  - order intents 2 / fills 2 / rejects 0
  - completed_flat / final position 0 / open orders 0
  - result:
    `C:\StockSnapshots\raw_v2_snapshot_24f657163264432da7af3ed533656eac\selected_prefix_smoke\9165453f3f85416bbecdc16946237e78\result.json`
  - reproducibility key:
    `f3ad6b6095891460033f2e1c784d28e19ba0cf6e78055092b43eeab7307692dd`

이 PASS는 bounded selected input → strategy → portfolio simulator 연결 완료만 뜻한다.
strategy performance, NXT venue, whole raw, live readiness를 승인하지 않는다.

**다음 증거:** result의 실제 buy/sell/order/fill을 원본 exact seq lookup으로 대표 사례 대조한 뒤,
같은 input/settings/code의 명시적 두 번째 smoke 1회로 reproducibility를 확인한다.
그 전에는 이 경로를 performance-research input으로 승격하지 않는다.

### 기본 경로 A — 9월 21일 실행 결과

- [x] **수집 승인·당일 사전 점검 수행 보고:** 사용자 live 수집 승인 및 preflight 근거는 HANDOFF 참조.
  새 세션 로그인·예약을 계속 승인하는 체크가 아니다.
- [x] **신규 세션 수집과 종료 근거 대조 보고:** 동일 세션의 입력 중단·drain·writer_closed·finalization·
  오류/드롭·콜백/순번·보고/저널·로그·OS 프로세스 부재·메타데이터를 로컬에서 대조했다는 보고다.
  저장 종료와 공급자 무누락/whole-file 품질 합격은 구분한다.
- [x] **제한 표본 수행:** 최초/중간/말미 총 300 records. 말미 품질 실패로 확대 재생 중단.
  수행 완료이지 표본 합격이 아니다. 같은 구간을 이유 없이 반복 검사하지 않는다.
- [ ] **후보 지정/표본 합격:** 9월 21일 세션은 위 문제로 미승격. 원본을 보존한다.
- [ ] **시험 범위:** 종목 하나·venue·청산 규칙 하나·독립 계좌 하나·수량·비용·지연·호가 나이를 명시한다.
  venue=unknown을 NXT 인증으로 바꾸지 않는다. 실행 가능성과 진입에 충분한 입력인지는 구분한다.

### 예비 경로 B — 현재 구현하지 않음

품질 문제가 재관측됐지만 clean 세션이 불가능하다고 확정한 것은 아니다. 진단 결과를 바탕으로만 재검토한다.
최신 날짜·크기만으로 raw를 고르지 않는다. 채택 시 원본을 수정하지 않고 별도 dataset identity,
원본 session_id/seq 추적, 사용·제외 범위와 사유, 원문 역추적, 정규화 코드 hash/정책,
전략 선행 구간·시가, 원본과 파생 결과의 재현 관계를 남겨야 한다.
오류 기록의 조용한 삭제, 무부호 값의 임의 방향 보정, 결과를 보고 유리한 종목/시간을 고르는 우회는 승인하지 않는다.

## 3. 장외 실행 전 준비

- [x] **원본 비변경 검사 경로 구현·합성 검증:** [qualification 코드](collector/raw_v2_qualification.py)는
  Windows 로컬 NTFS, sidecar 전무, 활성 write/delete handle 배제를 확인한 동안만 immutable reader를 연다.
  일반 reader의 SHM 생성 가능성과 qualification 비변경, non-empty WAL·sidecar·활성 쓰기 핸들 거부를
  작은 Windows fixture로 확인했다. 현재 운영 sidecar 삭제·checkpoint 절차를 구현한 것은 아니다.
- [x] **검사 전용 품질 진단 구현·합성 검증:** [CLI](scripts/qualify_raw_v2.py)는 전략 실행 없이
  stream integrity와 research eligibility를 분리한다. 구조가 유효한 parse_error를 끝까지 집계하고,
  대응 tick issue와 제어 기록을 logical issue로 이중 계산하지 않는다. bounded category/example만 보존한다.
  합성 회귀는 [tests/test_raw_v2_qualification.py](tests/test_raw_v2_qualification.py)에 있다.
- [ ] **실행 계획:** 입력·장외 시간·허용 I/O/시간·중단 기준·새 출력 경로·여유 공간을 확정한다.
  수집과 겹치지 않게 한다. 종목 하나 선택도 원본 전체 읽기를 줄이지 않는다.
- [x] **검사 경로:** 화면의 32 MiB·100,000 raw 상한과 별도로 대용량 streaming CLI가 존재한다.
  직접 연구 CLI와 달리 전략 재생을 하지 않으며 전체 순번/count/checksum과 품질 진단을 함께 수행한다.
  실제 대용량 실행 계획과 현재 sidecar의 안전한 해결은 여전히 별도 미완료다.
- [ ] **입력 전체 검증:** 승인된 장외 범위에서 순번·checksum·품질 제어 기록·whole-file 입력 계약을 검사하고 근거를 보존한다.
  조기 중단은 전체 checksum 완료가 아니다. 실패를 합격 처리하거나 표본·closed·CI로 대체하지 않는다.

### Selected-v2 bounded pipeline 진행 상태

아래는 기존 whole-file 연구 입력 체크와 별개의 bounded selected smoke 증거다.

- [x] **실제 pipeline smoke 1회:** input/settings/code/result/provenance를 HANDOFF와 result에 기록했다.
- [x] **결과 상태:** 77,558/77,558 event 처리, diagnostics_only=false, completed_flat, fills 2, rejects 0.
- [x] **대표 사례 대조:** buy/sell signal·order intent·fill을 working DB의 INTEGER PRIMARY KEY exact lookup 4건으로
  원본까지 연결했다. ask/bid fill 가격, 1초 latency, fee, lifecycle, 최종 cash ledger가 모두 result와 일치했다.
  상세 근거는 HANDOFF의 `005930 selected-v2 representative trade audit: PASS` 기록을 따른다.
- [x] **재현성:** 같은 input/settings/code로 smoke를 정확히 1회 재실행했다.
  reproducibility key·event SHA·settings·code SHA·signals·order intents·fills·transitions·final account·
  provenance가 동일했고 started_at/finished_at만 달랐다.
- [x] **research-input 승격 결정:** 이 경로를
  **bounded selected strategy-research input**으로만 승인한다.
  범위는 `2026-09-21 / 10:00 KST exclusive / 005930=unknown / selected-v2 /
  unknown_direction_recent_window_quarantine_v0`로 고정한다.
  이는 input/replay/execution-behavior 연구와 회귀의 근거로 사용할 수 있지만
  `performance_research` 입력 승격은 아니다.

### Selected-v2 research-input gate decision — 2026-09-24

**결정: bounded selected strategy-research input으로 승인. performance-research는 미승격.**

승인 scope:

- source session: `6f39117671c048f6b60477ceafbf40b6`
- frozen snapshot run: `24f657163264432da7af3ed533656eac`
- bounded prefix: 10:00:00 KST exclusive
- selected instrument: `005930=unknown`
- selected schema: `raw_v2_selected_prefix_qualification_v2`
- unknown-direction policy: `unknown_direction_recent_window_quarantine_v0`
- selected strategy input events: 77,558
- validated pipeline-smoke reproducibility key:
  `f3ad6b6095891460033f2e1c784d28e19ba0cf6e78055092b43eeab7307692dd`

허용 용도:

- 입력/정규화/selected policy/strategy/execution 경로 회귀
- 신호·주문·체결 causal trace
- accounting/PnL/equity 기능의 **구현 검증용 실제 bounded fixture**
- 명시적 exploratory behavior analysis. 결과는 단일 bounded 사례로만 표현

금지 용도:

- 전략 수익성·우수성·robustness 주장
- parameter optimization 또는 이 결과를 본 뒤 threshold/exit tuning
- out-of-sample/일반화 주장
- whole-file `FIRST_RESEARCH_CANDIDATE` 승격
- NXT venue 인증
- whole raw quality 승인
- live trading readiness/실주문 승인

performance-research 미승격 이유:

1. strict prefix는 계속 `smoke_backtest_eligible=false`; whole stream은 미평가다.
2. `venue=unknown`이며 NXT 원천 인증이 아니다.
3. 실제 검증 입력이 한 날짜·한 종목·10:00 bounded prefix 하나다.
4. 실제 왕복 사례는 1건뿐이며 성과 표본으로 해석할 수 없다.
5. accounting pure contract와 NXT `performance_accounting` subrecord는 구현됐지만,
   legacy top-level PnL/equity는 null이고 open-position final fresh-bid provenance 및
   실제 bounded accounting regression은 아직 완료 전이다.
6. 학습/조정 구간과 평가 구간 분리, 비용/지연 민감도, 여러 시장 상황 검증이 아직 없다.

따라서 다음 개발은 이 데이터의 성과를 더 캐는 것이 아니라
**평가 회계(PnL/equity/marking) 계약을 합성부터 구현하고, 실제 데이터는 회귀 fixture로만 사용**한다.

## 4. 첫 시험 실행과 결과 대조

- [ ] **시험 실행 1회:** 합격 입력·설정·코드 버전·출처·결과 경로를 기록한다.
- [ ] **결과 상태:** 전체 입력 처리·오류·체결 없음·미청산을 구분한다. failed 중간 결과는 진단 전용이다.
- [ ] **대표 사례 대조:** 원본 이벤트 → 신호 → 주문 → 체결 가격/수량을 연결한다.
  사례가 없으면 원인을 남기고 해당 경로의 실데이터 검증은 미완료로 둔다.
- [ ] **재현성:** 같은 입력·설정·코드의 재실행에서 주문·체결·경제적 결과를 대조한다.
  실행 ID/생성 시각은 분리하며 재실행도 장외 I/O 계획에 포함한다.
- [ ] **종료 기록:** 확인 범위·성공/실패 이유·한계를 HANDOFF에 기록한다. 전략 수익성으로 확대하지 않는다.

### Performance-accounting 구현 단계

selected-v2 bounded input은 strategy-research fixture로 승인됐지만 performance-research는 아직 미승격이다.
그 다음 선행 구현은 fill ledger 기반 performance accounting contract다.

- [x] pure weighted-average cost / realized PnL contract focused regression — PR #46, 97 passed
- [x] explicit fresh-valid-bid mark / unrealized PnL / equity contract focused regression — PR #46
- [x] fill cashflow ↔ simulator cash reconciliation contract — PR #46
- [x] NXT result finalization에 `performance_accounting` subrecord integration focused regression — PR #47, 119 passed
- [x] open-position final fresh-bid mark provenance integration focused regression — PR #48, latest-master combined 179 passed
- [ ] actual selected-v2 bounded smoke를 새 accounting/mark 코드로 정확히 1회 재실행해
  execution trace 불변 + performance_accounting identity를 검증
- [ ] accounting subrecord 실제 bounded 회귀까지 확인 후 legacy top-level PnL/equity schema migration 필요성 검토
- [ ] actual selected-v2 result는 회귀 fixture로만 사용하며 parameter tuning에 사용하지 않음

## 5. 첫 시험 이후 — 전략 평가

- [ ] 여러 날짜·시장 상황·종목에서 품질 확인된 입력과 진입 사례를 축적한다.
- [ ] 학습/조정 구간과 평가 구간을 분리하고 비용·지연·체결 가정 민감도를 비교한다.
- [ ] 거래 수·손실 구간·미체결·미청산·데이터 제외 내역을 검토한다. 용량으로 데이터 충분성을 선언하지 않는다.

## 별도 진행 — 첫 시험의 필수 선행 조건이 아닌 것

- [x] 작은 합성 raw 압축·복원 시제품과 바이트 검증: `97e3777`, `6e1029f`, [회귀](tests/test_raw_archive.py).
- [ ] 실제 대용량 압축·복원의 용량/속도/내구성 검증. 원본 자동 삭제는 범위 밖이다.
- [ ] 실피드 장시간 부하·장애 복구·OCX/NXT 전환 운영 실측. 연구에 필요한 원천 확인은 별도로 충족한다.
- [ ] 실거래 주문 연결과 운영 승인. 첫 시험 완료와 별개다.

운영 raw·Daily_baseline·old_data·사용자 변경·오류 근거를 보존한다.
체크 시 확인 날짜와 근거를 남기고 상세 계약을 여러 문서에 복제해 늘리지 않는다.
