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

[파이프라인 지도 §6](docs/PIPELINE_MAP.md#portfolio-research)에 공유 계좌·NXT 포트폴리오 전략·
bounded prefix·smoke 경로의 코드 연결과 계약을 둔다. `StrategyAccount`/`BacktestBroker`의
Phase E 핵심은 stub이며 기존 `TickSimulator` 경로와 구분한다. `run_nxt_prefix_smoke()`는
strict prefix report의 `smoke_backtest_eligible=true`와 동일 raw의 sealed 재검증을 요구한다.
합성 테스트 통과·prefix 품질·전략 성과·운영 계좌 상태는 각각 별개의 근거다.

**미연결/미완료:** 평균단가·원가·실현/미실현 PnL·equity와 표준 Trade 변환,
Paper/Mock/Live 주문 어댑터, 대용량 성능, 다중 전략 arbitration. 현행 회계는 현금·보유수량·수수료·체결 원장까지다.
`gross_exposure_at_ask`는 한도용 매수 대체 원가이지 손익이 아니다.

## 다음 행동

PR #41 direction-window와 PR #42 NXT strategy opt-in은 master에 통합됐다.
기본 `strict`는 `is_buy=None`을 거부한다. opt-in `unknown_direction_recent_window_quarantine_v0`은
unknown trade를 보존하고 recent window가 깨끗해질 때까지 신규 entry를 차단한다.
기존 NXT smoke runner와 strict prefix gate는 여전히 strict다.

과거 실제 selected overlay 결과는 **v1 역사 근거**로 보존한다.
`005930=unknown`은 v1에서 selected tick 77,558 / clean 77,557 / selected direction blocker 1건으로 FAIL했고,
그 blocker는 tick seq 711052 / control 711053, FID15 repr `' 237016'`,
normalized price 264000 / volume 237016 / is_buy=null의 explicitly unsigned FID15였다.
기존 strict `smoke_backtest_eligible=false`, whole-stream 미평가, NXT smoke 미실행 상태는 그대로다.

PR #43 selected-prefix v2 gate는 focused tests 117 passed 후 master에 통합됐다.
기본은 `strict`; opt-in에서는 exact mirrored parse_error pair, 관측된 Kiwoom trade 형태,
정상 market_second·양의 price/volume·`is_buy=None`, 무부호 양의 FID15와 normalized volume 일치를
모두 요구한다. 세부 계약은 [파이프라인 지도 §6](docs/PIPELINE_MAP.md#portfolio-research)에 둔다.

2026-09-24 지정 working snapshot·strict 10:00 KST report로 `005930=unknown` v2 overlay를
`unknown_direction_recent_window_quarantine_v0` policy로 **정확히 1회** 실행했다.
결과: `C:\StockSnapshots\raw_v2_snapshot_24f657163264432da7af3ed533656eac\selected_prefix_overlay\a59dbf1f37b64648bf791258ea0f5783\result.json`.
schema `raw_v2_selected_prefix_qualification_v2`, `status=completed`, 소요 448.756161초,
구조 검증·strict 재검증 5항목 모두 true다. 선택 tick 77,558 / clean 77,557,
unknown-direction quarantine pair 1 / zero-quote 0 / selected disqualifying 0,
비선택 exact ignored pair 13,901이며 unapproved·unpaired·unsafe 0이다.
**`005930 selected-prefix v2 quarantine quality: PASS`**, selected gate true.
기존 strict `smoke_backtest_eligible=false`, `whole_stream_assessed=false`,
`whole_prefix_quality_upgraded=false`는 유지됐다. 즉시 entry permission도 false다.
새 report에는 policy ID, strict report SHA, direction-window·tick-research SHA가 있다.
NXT smoke·GitHub Actions·추가 DB 조회는 실행하지 않았다.

현재 작업 branch `feat/selected-prefix-v2-smoke-runner-20260924`는
**selected-prefix v2 전용 NXT smoke runner** 후보를 추가한다.
기존 strict `run_nxt_prefix_smoke()`는 수정하지 않는다.

새 candidate `run_nxt_selected_prefix_smoke()`는:
- schema `raw_v2_selected_prefix_qualification_v2` + selected quality true만 받음
- explicit policy `unknown_direction_recent_window_quarantine_v0`를 강제
- selected report가 가리키는 strict report SHA/run id/scope/digest를 다시 확인
- selected report의 policy/strategy code provenance가 현재 코드와 정확히 같은지 확인
- 같은 sidecar-free raw prefix를 sealed/immutable로 **한 번만 다시 읽음**
- strict digest/count/sentinel/diagnostics를 다시 계산
- selected policy result도 같은 stream에서 다시 계산해 v2 report와 완전 일치 요구
- selected clean tick은 전략에 전달
- selected unsigned-direction exact pair는 parse_error pair 확인 후 원 trade를 `is_buy=None` 그대로 전략에 전달
- selected one-sided zero-quote exact pair는 전략 입력에서 제외
- 그 외 selected/global issue는 fail-closed
- 현재는 selected instrument 정확히 1개만 허용

runner provenance에는 selected/strict report SHA와 run id, policy ID,
expected forwarded event count, zero-quote withheld count, unknown-direction forwarded count,
adapter code SHA를 기록한다.
전략에는 동일 quarantine policy를 강제로 전달한다.

이번 candidate가 통과해도 전략 성과·whole raw·NXT venue·live 적격성을 인증하지 않는다.
다음 단계는 새 runner + 기존 strict smoke + PR #42 strategy path의 focused local regression이다.
통과 전에는 merge, 실제 8.4M selected smoke 실행, GitHub Actions를 하지 않는다.

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
