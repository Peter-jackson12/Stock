# 현재 인계 — 2026-09-24 / 실제 raw 오전 prefix 품질 차단

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

**현재 개발은 research/backtest/execution 트랙이다.** 공유계좌·주문 생명주기·RiskLimits 기반은 PR #30으로 master에 통합됐다.
이후 우선순위는 다중 전략 플랫폼이 아니라 **NXT 전략 하나를 새 실행 경계에 연결해 평가 가능한 연구 경로를 만드는 것**이다.
같은 전략을 여러 종목에 적용할 수는 있지만 전략 간 arbitration/자본 배분은 나중 필요할 때 추가한다.
이번 실데이터 실행은 파이프라인 점검이며 전략 파라미터 최적화와 실주문은 목표가 아니다.

## 원격 시작 기준과 현재 변경

- 실행 기준 master: `bf78e6abba1548965603211bf2baa6063f1cddb1` — 시작 clean checkout을 원격에 fast-forward했다.
- PR #34는 frozen snapshot acquisition 기반을 통합했고, PR #35는 source main/WAL/SHM exclusive seal을 working cleanup·hash·최종 source 재검증까지 유지하도록 보강했다.
- PR #34 후보 HEAD `463a71cc636431a02ca0e254e1ff71e3013564a3`에서 전체 Git-only 회귀 `2,012 passed / 6 deselected`를 확인했다. 이후 작은 변경마다 Actions를 반복하지 않는 정책으로 전환했다.
- GitHub Actions는 자동 PR/push 실행을 중단했다. 일반 CI는 매주 일요일 09:00 KST, Session assessment는 09:15 KST 정기 실행이며 필요할 때만 수동 실행한다.
- 작은 코드/문서 변경마다 Actions를 돌리지 않는다. 큰 기능 묶음, 배포/운영 전, 또는 주간 회귀에서만 전체 CI를 사용한다.
- #20/#22/#23/#25/#26/#27/#28은 수집기/FID 개발 이력이다. 임의 close/merge/retarget하지 않는다.
- PR #31~#33의 NXT 공유계좌·bounded prefix·prefix→NXT smoke 경로도 master에 통합돼 있다.

## 2026-09-24 실제 raw 실행 결과

대상: `sampledata/raw_ticks_v2/20260921/6f39117671c048f6b60477ceafbf40b6.db`.
실행 직전 filesystem metadata: main 50,635,071,488 bytes, WAL 0 bytes, SHM 32,768 bytes,
journal 부재, main 단일 링크. 원본 SQLite 접속·sidecar 정리는 하지 않았다.

- Frozen snapshot: `C:\StockSnapshots\raw_v2_snapshot_24f657163264432da7af3ed533656eac\result.json`.
  `snapshot_ready=true`, 원본 main/WAL/SHM exclusive 획득과 최종 source metadata 불변,
  evidence 최종 봉인, working sidecar 전부 부재. source stream/working readback main SHA-256은 모두
  `e4304fe3c1cad8a85ec6c297ceb9cfdadca2d20001e93756303d567ec5077569`.
- 고정 10:00 KST prefix: 같은 run의 `prefix_qualification\4076abdc94bc46588bbb7b0f334e023f\result.json`.
  `status=completed`, 구조 검증 성공, `smoke_backtest_eligible=false`.
  sentinel seq 8,414,462 (`2026-09-21T01:00:00.000269+00:00`), sentinel까지 8,414,462건 소비;
  prefix 8,414,461건 중 tick 8,400,558, trade 3,282,752, quote 5,117,806,
  control 13,903(session_start 1, parse_error 13,902).
  품질 원인: out_of_range_fid_41 6,819, out_of_range_fid_51 3,868,
  trade_direction_unverified 3,233. `stream_error=null`, `tail_scanned=false`, 전체 raw·전략 성과 미평가.
- 지정된 실패 정책에 따라 NXT smoke는 실행하지 않았다. 원본/evidence/working/result는 보존하고
  cutoff 변경·자동 재시도·whole-file 검사는 하지 않는다.
- 후속 bounded 조사: 위 result의 문제 tick 30개·직후 parse_error 30개·session_start 1개만
  working DB의 INTEGER PRIMARY KEY exact lookup으로 읽었다(61/61 row).
  FID41·51 각 10개 모두 raw `-0`, 해당 쪽 top3 가격 `-0`·잔량 0, 반대편 최우선 호가 유효.
  FID15 예시 10개는 선행 공백이 있는 무부호 양수로 volume은 보존됐으나 `is_buy=null`.
  30개 pair의 seq/시각/code/issues가 모두 일치했다. 두 호가 오류 동시 발생 18건은 집계상 추론이며
  bounded 예시에는 없다. `005930`은 top20/예시에 없고 전체 affected code 목록은 결과에 없다.

## 실제 구현과 보존한 경계

`ReceiveOrderReplay`는 이미 종목별 호가와 공통 seq를 지원한다. 기존 `TickSimulator`는
단일 종목의 cash/position을 소유하고, cash/holdings 부족 시 명시적 reject가 아니라 대기한다.
`StrategyAccount`/`BacktestBroker`의 핵심 Phase E 메서드는 stub이다. 새 경로를 그 완료로 오인하지 않는다.

[PortfolioSimulator](execution/portfolio_simulator.py)는 기존 재생·호가 검증·정확한 금액 산술을
재사용하며 공유 계좌/예약/상태 전이를 추가한다. 종목당 venue 하나, source/session 하나다.
[portfolio_session](engine/portfolio_session.py)은 전략에 불변 snapshot만 주고 ordered intent를 실행한다.
`portfolio_research_result_v1`은 메모리 내 JSON-native dict이고 기존 `tick_research_result_v1`과 다르다.
새 정책의 정의·순서·기존 경로와의 차이는 [파이프라인 지도 §6](docs/PIPELINE_MAP.md#portfolio-research)에 둔다.

[합성 회귀](tests/test_portfolio_simulator.py)는 현금/보유량 경쟁, 부분체결·취소·만료·거절,
노출/수량/open-order 한도, 동일 시각, 미래 suffix/chunk/반복, 실패 진단, 독립 Fraction 원장과
기존 단일 종목의 충분한 자금 사례를 대조한다. 비직렬화 주문 의도가 실패 보고서까지 숨기던 경계는
`5513432`에서 보강했다. 최초 59 / 보강 후 62 집중 통과는 이전 작업의 격리 Python 3.13 보고이며,
원격 전체 통과와 혼동하지 않는다.

**보존된 CI 이력:** [#338](https://github.com/Peter-jackson12/Stock/actions/runs/35953047208)의
Decimal sticky flag 테스트 실패는 테스트 수정 후 해결했다. 기존 실패 run은 유지한다.

**보존:** 기존 TickSimulator·NxtResearchStrategy·run_research/run_raw_v2·실제 CLI·입력 정책·
collector·workflows·기존 테스트는 변경하지 않는다. 합성 상태 snapshot은 운영 계좌 조회가 아니다.

**PR #31 통합:** 기존 `NxtResearchStrategy`를 종목별 상태로 재사용하는 `NxtPortfolioStrategy`와
`run_nxt_portfolio()`가 master에 들어갔다. 전략에는 mutable account를 주지 않고 symbol-scoped port가 순수 intent만 만든다.
한 전략을 여러 종목에 적용해도 공유 cash/risk는 PortfolioSimulator가 소유한다. 결과는 새 UUID 디렉토리의
`portfolio_research_result_v1` JSON으로 보존하고 전략 설정·코드 hash·signals를 reproducibility key에 포함한다.

**PR #32 통합:** `raw_v2_prefix_qualification_v1`은 seq=1부터 사전에 정한 KST exclusive cutoff까지의
구간만 검증한다. cutoff 시각 이상에서 구조적으로 유효한 다음 record를 sentinel로 요구해 실제 수집이 경계까지
도달했음을 확인한다. tail은 의도적으로 읽지 않고 `whole_stream_assessed=false`를 기록한다. 합격 명칭은
`smoke_backtest_eligible`이며 전체 raw 승격·전략 성과 연구 적격성과 분리된다.

**통합된 smoke 경로:** `run_nxt_prefix_smoke()`는 합격한 prefix report와 정확히 같은 raw 경로만 받는다.
실행 직전에 sealed/no-sidecar 상태에서 prefix를 다시 읽어 manifest·digest·record count·quality diagnostics·sentinel을
qualification report와 대조하고, 모두 같을 때만 명시한 종목 tick을 `run_nxt_portfolio()`로 스트리밍한다.
결과 provenance는 `purpose=smoke_backtest_only`, `whole_stream_assessed=false`, `performance_research_assessed=false`,
`raw_identity_verified=false`를 보존한다. qualifier 이후 prefix bytes가 달라지면 정상 성과가 아니라 failed diagnostics로 끝낸다.

**미연결/미완료:** 평균단가·원가·실현/미실현 PnL·equity와 표준 Trade 변환,
Paper/Mock/Live 주문 어댑터, 대용량 성능, 다중 전략 arbitration. 현행 회계는 현금·보유수량·수수료·체결 원장까지다.
`gross_exposure_at_ask`는 한도용 매수 대체 원가이지 손익이 아니다.

## 다음 행동

PR #36으로 통합된 `collector/zero_quote_policy_experiment.py`는 실제 prefix에서 관측한 **한쪽 top3 가격 모두 `-0` + 같은 쪽 top3 잔량 0 + 반대편 top1 양수** 패턴만
`missing_non_executable_quote_candidate`로 분류하는 synthetic-only 실험이다.
기존 normalizer/qualification/smoke eligibility는 변경하지 않는다.

실험의 핵심 안전 조건:
- 정확히 하나의 `out_of_range_fid_41` 또는 `out_of_range_fid_51`만 허용
- signed_magnitude + Kiwoom prototype normalization만 허용
- 양쪽 zero, 추가 issue, 같은 쪽 양수 잔량, 반대편 비정상은 계속 disqualifying
- 후보 quote도 `check_ordered_quote`에서 계속 invalid_bid/invalid_ask로 실행 불가
- 정확한 mirrored parse_error pair만 candidate로 묶음
- `trade_direction_unverified`는 절대 완화하지 않음

다음 실제 단계는 이 합성 실험을 **focused local test**로 확인하는 것이다. 통과하면 그 다음에야 **zero-quote pair만 smoke-quality quarantine 후보로 취급하는 별도 opt-in prefix policy**를 설계할지 결정한다.
실제 50GB prefix 재실행·smoke·정책 변경은 아직 하지 않는다. GitHub Actions도 이 작은 단계에서는 실행하지 않는다.

## 유지하는 운영/실데이터 차단 조건

2026-09-21 원본: session `6f39117671c048f6b60477ceafbf40b6`,
revision `4821762fd93230b658339fee084d6c08e3e53ce9`, raw `50,635,071,488 bytes`.
보고된 -wal 0 / -shm 32,768 bytes를 보존한다. unsigned FID15/parse_error 전체 조사,
sidecar 출처·whole-file stream integrity·전체 품질·FIRST_RESEARCH_CANDIDATE 승격·첫 실제 연구는 미완료다.
파일/payload 해시 주장, callbacks/final_seq 차이와 과거 세션 수치의 원문은 아래 고정 인계에 보존한다.
이번 실행에서 원본은 filesystem metadata와 snapshot 도구의 sealed stream으로만 확인했다.

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
