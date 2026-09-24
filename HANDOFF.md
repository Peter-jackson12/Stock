# 현재 인계 — 2026-09-24 / 다종목 공유 계좌 합성 연구 후보

[문서 인덱스](README.md) · [공유 계좌 연결/계약](docs/PIPELINE_MAP.md#portfolio-research) ·
[첫 실제 연구 체크](BACKTEST_TODO.md) · [기존 틱 연구 런북](TICK_RESEARCH_RUNBOOK.md) ·
[수집 의사결정 계약](docs/COLLECTION_RUNBOOK.md#collection-decision) ·
[live 작업 경계](docs/COLLECTION_RUNBOOK.md#collection-live-boundary) ·
[세션 근거 표시](docs/SESSION_ASSESSMENT.md) · [보존본 안내](docs/archive/README.md)

매 작업 시작 시 원격 master·열린 PR·최신 CI와 작업 후보의 정확한 HEAD/base를 다시 확인한다.
아래 값은 이번 개발의 시작 기준이며 영구 최신값이 아니다.

## 현재 두 트랙

**수집기/native 트랙은 다음 실제 시장 세션까지 의도적으로 보류한다.**
그때의 별도 승인된 Mock A-B-A 1회가 다음 실질 단계이며, 이번 개발에서 그 실행 준비를 늘리지 않는다.
FID hot path·admission/run-plan·OCX/QAx·queue/teardown/telemetry·실제 수집·PID/창/lease 관측·
#327/#162 원인 실험·historical benchmark 재측정·역사 PR 정리를 진행하지 않는다.

**현재 개발은 research/backtest/execution 트랙이다.** 작은 합성 다종목 tick stream에서
공유 현금·종목별 보유량·예약·RiskLimits·명시적 주문 상태·순수 주문 의도 경계를 검증한다.
실제 raw, 전략 수익률 최적화, 실주문은 이번 후보의 입력/목표가 아니다.

## 원격 시작 기준

- master `78e0e74679877ec7e36f22b126f2ba9da606a5ac`: PR #19 병합 commit.
- 이 HEAD의 일반 CI #336(run `35946603390`, job `107465825995`)와
  Session assessment #6(run `35946603430`)는 원격 completed/success다.
  기존 인계의 전체 수치는 1,900 passed / 6 deselected, 표시 집중 127 passed다.
  집중/전체를 합산하지 않으며 deselected는 통과가 아니다.
- 열린 #20/#22/#23/#25/#26/#27/#28은 수집기 개발 이력으로 유지한다. 임의 close/merge/retarget하지 않는다.
- 이번 branch: `feat/portfolio-engine-20260924`. 최종 HEAD/PR/CI는 branch와 checks에서 확인한다.
  이 문서의 후보 구현 설명은 master 병합·로컬 배포·운영 승인을 뜻하지 않는다.

## 이번 구현 후보와 확인한 실제 gap

`ReceiveOrderReplay`는 이미 종목별 호가와 공통 seq를 지원한다. 기존 `TickSimulator`는
단일 종목의 cash/position을 소유하고, cash/holdings 부족 시 명시적 reject가 아니라 대기한다.
`StrategyAccount`/`BacktestBroker`의 핵심 Phase E 메서드는 stub이다. 새 경로를 그 완료로 오인하지 않는다.

[PortfolioSimulator](execution/portfolio_simulator.py)는 기존 재생·호가 검증·정확한 금액 산술을
재사용하며 공유 계좌/예약/상태 전이를 추가한다. 종목당 venue 하나, source/session 하나다.
[portfolio_session](engine/portfolio_session.py)은 전략에 불변 snapshot만 주고 ordered intent를 실행한다.
`portfolio_research_result_v1`은 메모리 내 JSON-native dict이고 기존 `tick_research_result_v1`과 다르다.
새 정책의 정의·순서·기존 경로와의 차이는 [파이프라인 지도 §6](docs/PIPELINE_MAP.md#portfolio-research)에만 둔다.

[합성 회귀](tests/test_portfolio_simulator.py)는 현금/보유량 경쟁, 부분체결·취소·만료·거절,
노출/수량/open-order 한도, 동일 시각, 미래 suffix/chunk/반복, 실패 진단, 독립 Fraction 원장과
기존 단일 종목의 충분한 자금 사례를 대조한다. 격리된 Python 3.13 집중 검증은 62 passed다.
비직렬화 주문 의도가 실패 보고서까지 숨기던 경계도 회귀로 고정했다.
원격 Windows/Python 3.14 전체 결과는 최종 PR HEAD의 완료 로그로 별도 확인한다.

**보존:** 기존 TickSimulator·NxtResearchStrategy·run_research/run_raw_v2·실제 CLI·입력 정책·
collector·workflows·기존 테스트는 변경하지 않는다. 합성 상태 snapshot은 운영 계좌 조회가 아니다.

**미연결/미완료:** NXT 다종목 전략 어댑터, 결과 영속 저장/조회 연결, 원가·실현/미실현 PnL·equity,
실제 raw 입력, Paper/Mock/Live 주문 어댑터, 대용량 성능. 새 결과의 PnL/equity는 null이다.
`gross_exposure_at_ask`는 위험 한도용 매수 대체 원가이지 손익이 아니다.

## 다음 행동

1. 최종 PR diff가 execution/engine/tests와 담당 문서에만 한정되는지 확인한다.
   집중 합성 결과와 정확한 PR HEAD/base의 전체 CI를 구분해서 보고한다.
2. 추가 감사에서 의미적 문제가 없고 최신 base 결합 CI가 성공하면 기존 자율권 범위에서 병합을 판단한다.
   병합 시 master SHA와 그 push CI까지 확인하며, 역사 PR/운영 PC는 그대로 둔다.
3. 다음 구현은 기존 NXT 규칙의 종목별 상태를 순수 intent 경계에 연결하고 새 결과의 저장/조회 경로를
   좁게 연결하는 순서가 후보이다. 원가/PnL 평가 계약은 그 뒤 명시적으로 설계한다.
   합성 엔진 성공만으로 실데이터 첫 연구를 열지 않는다.

## 유지하는 운영/실데이터 차단 조건

2026-09-21 원본: session `6f39117671c048f6b60477ceafbf40b6`,
revision `4821762fd93230b658339fee084d6c08e3e53ce9`, raw `50,635,071,488 bytes`.
보고된 -wal 0 / -shm 32,768 bytes를 보존한다. unsigned FID15/parse_error 전체 조사,
sidecar 출처·whole-file stream integrity·전체 품질·FIRST_RESEARCH_CANDIDATE 승격·첫 실제 연구는 미완료다.
파일/payload 해시 주장, callbacks/final_seq 차이와 과거 세션 수치의 원문은 아래 고정 인계에 보존한다.
이번 개발에서 직접 raw/evidence/운영 폴더/프로세스를 관측하지 않았다.

원본/sidecar 처리와 qualification은 대상 identity·출처·외부 reader/writer 및 namespace 격리·
장외 시각·collector 부재·free space·I/O/time 예산·실패 보존 설계와 별도 승인이 필요하다.
writable in-place cleanup, unsigned 방향 임의 보정, 유리한 종목/시간 사후 선택을 하지 않는다.
closed != data quality pass; sample clean != whole-file clean; stream integrity != research eligibility;
qualification != strategy validation; backtest != live trading approval.

#327/#162 최초 startup 원인과 native 장애 인과는 미확정이다. 이후 성공 CI가 원인 해결을 뜻하지 않는다.
#29 진단 raw의 연구 배제, #18 수집 결정, #21 근거 표시 계약을 유지한다.
#19 benchmark는 역사적 비교이며 완료된 작은 canary/오프라인 실험을 반복하지 않는다.
`.venv32`/운영 raw/dump/operations_state/Daily_baseline/old_data/오류 근거/사용자 변경을 보존한다.
자동 kill/restart/relogin, Runtime 창 닫기, lock 삭제, LAA 변경, queue 확대, 기본 FID 축소,
force push/history rewrite/destructive cleanup은 하지 않는다. live 중 master 병합 보류 규칙도 유지한다.

## 원문 이력 — 현재 운영 관측으로 해석하지 않음

[이번 개발 전 master 인계 전체](https://github.com/Peter-jackson12/Stock/blob/78e0e74679877ec7e36f22b126f2ba9da606a5ac/HANDOFF.md)에
#19 통합 근거, #29/#18/#21 역할, FID 실행 한계, #321/#324/#327과 이전 CI 실패 보존,
2026-09-22/23 장애 보고, 09-21 원본/해시/sidecar 및 이전 인계 링크를 그대로 남긴다.
그 기록의 PID/창/저장 상태를 현재 사실로 사용하지 않는다. 원문 blob과 역사 commit은 변경하지 않는다.
