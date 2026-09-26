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

- collector trade/quote silence recovery 및 session 종료 경계
- queue backlog 기본 임계치/시간 창
- 오래된 활성 job의 운영 화면 접근성 → 아래 [Operator P2 후속](#operator-p2)
- managed capture 초기화 실패 전달 → 아래 [Operator P2 후속](#operator-p2)
- legacy feature coverage manifest 누적/strict publish 순서
- legacy KIS daemon의 후처리 완료 판정

이들은 별도 운영/legacy hardening 작업으로 다룬다.

## 검증 원칙

- 실제 MWFD-04 결과를 다시 계산하거나 수정하지 않는다.
- 기존 MWFD-04 run은 이미 고정된 revision/provenance와 직접 완료 검증 결과를 따른다.
- 이번 변경은 **향후 run과 MWFD-05 이후 exact validation 경계**를 강화한다.
- focused tests와 Git-only 전체 회귀가 통과하기 전에는 master 병합 완료로 간주하지 않는다.

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
