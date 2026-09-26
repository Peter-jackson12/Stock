# PIPELINE AUDIT research remediation — 2026-09-26

기준 감사는 `docs/pipeline-audit-20260926` 브랜치의
`docs/PIPELINE_AUDIT_20260926.md`다.

이번 변경은 MWFD-05에 들어가기 전에 **MWFD/Fast/exact 연구 신뢰성 경계**만 보강한다.
collector 운영 감시, 작업 제어 UI, managed capture 초기화, legacy feature/KIS 경로는 별도 하드닝 트랙으로 남긴다.

## 이번 범위에서 보강한 항목

### MWFD finalization

- crosscheck가 FAIL이면 exit 0으로 성공 처리하지 않는다.
- 이미 존재하는 FAIL crosscheck/resumecheck도 재호출 시 nonzero로 유지한다.
- combine의 일부 출력만 final 이름으로 게시된 뒤 중단되어도 공통 ledger와 checkpoint digest를 이용해 복구한다.
- summarize는 개별 파일 존재 여부가 아니라 마지막 `summarize.json` 완료 표식을 사용한다.
- summary 산출물은 tmp + replace로 다시 계산 가능하게 하고, 완료 표식에 각 산출물 SHA-256을 기록한다.
- artifact manifest의 `COMPLETE`는 combine / crosscheck / resumecheck / summarize / coverage / gate / unresolved failure를 모두 통과해야만 게시한다.
- 기존 `artifact_manifest.json`의 COMPLETE도 재호출 시 현재 산출물과 validation gate를 다시 대조한다.
- host-limit 예외 실행 시간·driver hash·RSS를 runtime summary에 별도 기록한다.
- final artifact manifest에 finalizer hash, 계산 code revision/provenance, host-limit exception provenance를 보존한다.

### 계산 provenance

Fast sweep가 직접 참조하는 `engine/nxt_tick_engine.py`를 MWFD code-provenance 대상에 추가했다.
기존 완료 MWFD-04 run의 provenance를 소급 변경하지 않으며, 이 변경은 이후 새 run에 적용된다.

### Fast ↔ production exact

- signal/fill/trade/PnL parity와 exact accounting acceptance를 별도 판정한다.
- exact accounting은 completed result, complete input, non-diagnostic result, null error,
  `portfolio_performance_accounting_v1`, supported valuation status,
  cash/position/accounting identity reconciliation, total PnL을 모두 확인한다.
- parity가 PASS여도 accounting이 실패하면 전체 exact acceptance는 REJECTED다.
- Fast pipeline manifest에 `parity_status`, `accounting_acceptance_status`,
  `exact_acceptance_status`를 별도로 기록한다.

### result inspector

- no-fills + open position
- empty/no-selected + order/fill/signal/open position
- open-position + zero fills

같은 상태 모순을 lightweight inspector가 성공 결과로 통과시키지 않는다.

### 문서

`docs/TESTING.md`의 CI 설명을 실제 workflow와 맞췄다.
현재 CI는 작은 PR/push마다 자동 실행되지 않고 주간 schedule + manual workflow_dispatch 방식이다.

## 이번 범위에서 의도적으로 남긴 항목

다음 감사 항목은 MWFD-05 입력의 완결성과 직접 연결되지 않으므로 이번 PR에서 수정하지 않는다.

- collector trade/quote silence recovery 및 session 종료 경계 → 아래 [collector P2 후속](#collector-p2)
- queue backlog 기본 임계치/시간 창 → 아래 [collector P2 후속](#collector-p2)
- 오래된 활성 job의 운영 화면 접근성 → 아래 [Operator P2 후속](#operator-p2)
- managed capture 초기화 실패 전달 → 아래 [Operator P2 후속](#operator-p2)
- legacy feature coverage manifest 누적/strict publish 순서 → 아래 [legacy P2 후속](#legacy-p2)
- legacy KIS daemon의 후처리 완료 판정 → 아래 [legacy P2 후속](#legacy-p2)

이들은 별도 운영/legacy hardening 작업으로 다룬다.

## 검증 원칙

- 실제 MWFD-04 결과를 다시 계산하거나 수정하지 않는다.
- 기존 MWFD-04 run은 이미 고정된 revision/provenance와 직접 완료 검증 결과를 따른다.
- 이번 변경은 **향후 run과 MWFD-05 이후 exact validation 경계**를 강화한다.
- focused tests와 Git-only 전체 회귀가 통과하기 전에는 master 병합 완료로 간주하지 않는다.

<a id="collector-p2"></a>
## collector P2 후속 — session silence / queue backlog

MWFD-05와 별개 트랙이다. 실제 로그인·수집·raw 접근 없이 합성 회귀
`tests/test_session_monitor_p2_contract.py`로 고정했다. 현행 계약 원문은
[COLLECTION_RUNBOOK](COLLECTION_RUNBOOK.md#collection-silence-profile)에 둔다.

| 감사 발견 | 조치 |
|---|---|
| 프로필 경로의 체결 침묵이 공통 `last_event_ts`(호가 포함)로 회복됐다 | 종류별 시계로만 회복한다. 종료 권고도 체결 수신으로만 풀린다. |
| 명시적 프로필을 써도 종료 판정이 09:00~15:30 고정 창이었다 | 종료 보고가 경고와 같은 구간/임계/시계로 판정한다. |
| 기본 적체 바닥 20,000이 raw-v2 capacity 8,192보다 커서 도달 불가였다 | capacity 절반으로 도출하고 도달 불가 바닥은 거부한다. raw-v1 무한 큐는 기존값 유지. |
| 창 이전 표본을 모두 버려 불규칙 간격에서 경보가 빠졌다 | `above_floor_since`와 경계 직전 기준점으로 판정한다. |

변경하지 않은 것: legacy `sessions=None`의 any-event 감시, 핫패스(`on_trade`/`on_quote`),
큐 넘침 fail-closed, 수집·저장 차단 여부. 임계값은 결과를 보고 조정하지 않았다.

<a id="operator-p2"></a>
## Operator / managed capture P2 후속

collector/native hot path 와 별개인 control-plane 변경이다. 현행 계약 원문은
[CONTROL_TOWER](../CONTROL_TOWER.md)의 작업 이력·managed capture 절에 두고,
합성 회귀는 `tests/test_operator_p2_contract.py`에 둔다.

| 감사 발견 | 조치 |
|---|---|
| 작업 화면이 최근 30개만 읽어 오래된 미완료 작업이 사라졌다 | `JobStore.active()`(미완료, 오래된 순, 상한 100)와 `finished()`(종료, 최신 30)를 분리했다. |
| dedup 이 30개 밖 작업 ID를 돌려주면 화면에서 찾기 어려웠다 | 미완료 목록에서 항상 보이고 `get()`으로 조회한다. |
| claim 전 자식 종료가 `launching`에 남아 다음 launch 를 막았다 | spawn identity 를 고정하고 소멸이 확인되면 `failed`로 수렴한다. |

`recent()`, 취소/claim/complete, 소유 프로세스 reconcile, Popen 예외의 `unknown`,
단일 활성 launch 제약, DB user_version 은 바꾸지 않았다. collector 코드는 수정하지 않았다.

<a id="legacy-p2"></a>
## legacy feature 저장 / KIS daily daemon P2 후속

legacy 경로만 다룬다. MWFD/Fast Backtest, Kiwoom 운영 수집기, raw-v2 경로는 바꾸지 않았고
이 변경이 그 경로를 검증하지도 않는다. 합성 회귀는 `tests/test_legacy_feature_kis_p2_contract.py`이며
실제 KIS API·websocket·LOB/raw DB·Daily CSV 운영 검증이 아니다.

| 감사 발견 | 조치 |
|---|---|
| `FeatureStore.write()`가 매니페스트를 통째로 다시 써 다른 날짜 coverage가 사라졌다 | 기존 매니페스트에 병합한다. 다른 날짜 coverage와 기존 피처 선언은 보존하고, 같은 날짜 재기록은 그 날짜 coverage만 교체한다(coverage 없이 재기록하면 낡은 값을 지운다). 매니페스트는 임시 파일 + fsync + `os.replace`로 바꾼다. |
| `--strict` 커버리지 실패 전에 parquet/매니페스트가 이미 게시됐다 | `build_day`가 커버리지를 게시 **전에** 재고 strict 실패면 아무것도 쓰지 않고 멈춘다. parquet도 임시 파일(`.<date>.<id>.tmp`, `*.parquet` glob 밖)에 쓴 뒤 교체한다. non-strict 저커버리지는 기존처럼 경고 후 게시하며 그 coverage를 함께 기록한다. |
| KIS 데몬이 LOB·일봉 결과를 보지 않고 무조건 "완벽히 끝났다"고 출력했다 | `collector/daily_daemon_result.py`가 raw 수집·LOB 변환·일봉 갱신을 각 함수의 기존 반환 계약으로 판정한다. 셋 모두 성공일 때만 완료 문구와 종료값 0을 낸다. 일부 성공/무데이터/실패/결과 미확인은 1이고, 예외는 요약을 남긴 뒤 전파한다. |

단계 판정 근거: `resample_raw_to_lob`의 `None`(원본 없음)과 `failures`는 실패, 변환 0종목/0행은 무데이터다.
`FastDailyCollector.collect()`는 이제 journal 종료 이벤트(`no_data`/`completed`)와 같은 사실을 반환한다.
`completed`여도 가격을 받은 종목이 요청 종목보다 적으면 일부 성공이다. raw 적재 0건은 무데이터다.
저장 데이터 삭제·rollback·재시도·자동 복구는 추가하지 않았다. 기존 단계 순서(예외 시 뒤 단계 미실행)는 유지했다.
