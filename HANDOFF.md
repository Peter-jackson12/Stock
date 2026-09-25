# 현재 인계 — 2026-09-25 / selected-v2 actual accounting regression

[문서 인덱스](README.md) · [첫 실데이터 체크](BACKTEST_TODO.md) ·
[파이프라인 지도](docs/PIPELINE_MAP.md#portfolio-research) ·
[수집 의사결정 계약](docs/COLLECTION_RUNBOOK.md#collection-decision) ·
[live 작업 경계](docs/COLLECTION_RUNBOOK.md#collection-live-boundary) ·
[세션 근거 표시](docs/SESSION_ASSESSMENT.md) · [보존본 안내](docs/archive/README.md)

매 작업 시작 시 원격 master·열린 PR·최신 CI와 작업 후보 HEAD/base를 다시 확인한다.
아래 값은 현재 인계이며 영구 최신값이 아니다. 2026-09-24~25 상세 실행·수치·경로 원문은
[압축 전 인계 보존본](docs/archive/HANDOFF_20260925_PRE_COMPACT.md)에 그대로 남긴다.

## 현재 두 트랙

**수집기/native 트랙은 다음 실제 시장 세션까지 보류한다.**
별도 승인된 Mock A-B-A 1회가 다음 실질 단계다. 그 전에는 FID hot path·admission/run-plan·
OCX/QAx·queue/teardown/telemetry·실제 수집·PID/창/lease 관측·#327/#162 원인 실험을 늘리지 않는다.
자동 kill/restart/relogin, Runtime 창 닫기, lock 삭제, LAA 변경, queue 확대, 기본 FID 축소도 하지 않는다.

**research/backtest/execution 트랙의
`005930 selected-v2 actual accounting regression` 1회는 PASS로 완료했다.**
추가 rerun·parameter tuning·실주문은 현재 목표가 아니다.

Operator UX는 PR #45/#49/#50/#52까지 master에 통합됐다.
`stock.cmd/stock.ps1`의 help/doctor/status/ui와 운영 화면의 상태·환경 요약은 사용 편의 계층이며,
수집 준비·OCX 준비·시장 상태·데이터 품질·실행 승인을 만들지 않는다.


운영 화면에는 환경 목록과 분리된 **수집 전 읽기 전용 preflight**가 추가됐다.
기존 collector admission의 process/window·lease·disk reader와 로그인 없는 공식 32-bit/OCX
preflight만 재사용한다. 각 축을 PASS/WARN/BLOCKED/UNVERIFIED와 다음 확인으로 표시하지만,
실행 대상 revision·CLI 계약과 시장 날짜·장 구간·실행 승인은 UNVERIFIED다. 시작 버튼과 연결하지 않으며
설치·수정·로그인·구독·수집·kill/restart·lock 삭제·시장 조회·raw DB 접근은 하지 않는다.
GitHub/PR 사실을 운영 PC의 현재 process/window/lease 상태로 승격하지 않는다.

Operator UX의 preflight/Run Plan은 Windows 화면 확인까지 완료했다. 이번 후속은 운영 화면에서
현재 사용자 바탕화면·시작 메뉴의 `Stock Operator.lnk`를 명시적 클릭으로 만들고 제거하는 진입점을 추가한다.
바로가기는 현재 저장소 `stock.cmd ui`만 호출하며 관리자 권한·레지스트리·영구 실행 정책 변경이 없다.
실제 Windows .lnk 생성 확인은 이 후보 병합 후 남는다. exe 패키징은 하지 않는다. [상세](START_HERE.md)

## selected-v2에서 이미 확인한 것

고정 scope:
- source session `6f39117671c048f6b60477ceafbf40b6`
- frozen snapshot run `24f657163264432da7af3ed533656eac`
- 10:00:00 KST exclusive prefix
- `005930=unknown`
- schema `raw_v2_selected_prefix_qualification_v2`
- policy `unknown_direction_recent_window_quarantine_v0`
- strategy input 77,558 events

이 범위에서 pipeline smoke, 대표 왕복 raw exact-seq audit, 동일 input/settings/code 재실행 재현성은 PASS다.
따라서 **bounded selected strategy-research input**으로는 승인했다.
하지만 strict prefix failure와 whole-stream 미평가를 유지한다.
`performance_research`, whole-file FIRST_RESEARCH_CANDIDATE, NXT venue, whole raw, live 적격성은 미승격이다.
기존 회귀/검증 트랙에서는 한 왕복 결과를 보고 threshold/exit/parameter를 조정하지 않는다. 단, 아래 명시한 2026-09-21 전용 exploratory profitability track에서는 in-sample 탐색임을 표시하고 entry/exit parameter search를 허용한다.

## actual accounting regression 결과

PR #46~#48로 다음이 master에 들어왔다.
- fee-inclusive weighted-average cost와 exact realized accounting
- cash/accounting reconciliation
- NXT result의 `performance_accounting_v1`
- final fresh-valid-bid mark provenance
- stale/missing/locked-crossed/invalid quote의 fail-closed unpriced 처리

2026-09-25 지정 commit `6d878d93329c136157fbaaed61c4f950dddaea10` detached clean worktree에서
기존 frozen working DB + selected-v2 report + 고정 smoke 설정으로 정확히 1회 실행했다.
새 결과는
`C:\StockSnapshots\raw_v2_snapshot_24f657163264432da7af3ed533656eac\selected_prefix_smoke\ed5e78db61da4646ad186ac943c1c5f4\result.json`이다.
실행 전후 working DB의 WAL/SHM/journal은 모두 부재했고 추가 rerun·GitHub Actions는 실행하지 않았다.

과거 비교 기준 `f37533fb67884b3f9033894befaed153`과 event SHA/count, settings,
signals, intents, fills, transitions, final cash/positions/orders, rejects, execution status가 JSON/value 기준
모두 동일하다. 77,558건을 모두 처리했고 final cash `997960.500`, position/open order/reject는 모두 0이다.
새 reproducibility key는 `677bac5071d2ce07468ad909e6d6760764bf1f9a6e454ba30bc12f65a471780a`다.

`performance_accounting`은 `flat_complete`, fill 2건, realized/total `-4079/2`,
unrealized `0/1`, equity/current/expected cash `1995921/2`이며 cash/position/accounting reconciliation은
모두 true다. `final_bid_mark_provenance`와 내부 provenance는 같은
`portfolio_final_bid_mark_provenance_v1`, valuation time `10877071329700`, records 빈 목록이다.
legacy top-level realized/unrealized/equity는 계속 null이다.

selected/strict report SHA, unknown-direction policy, expected 77,558건, unknown 1건 전달,
zero-quote 0건 격리와 `raw_identity_verified=false`, `whole_stream_assessed=false`,
`performance_research_assessed=false`를 유지했다.
**`005930 selected-v2 actual accounting regression: PASS`**.

이 PASS는 bounded replay의 execution 불변성과 accounting/marking subrecord만 확인한다.
전략 수익성·performance-research·여러 날짜 일반화·NXT venue·whole raw·live 적격성을 뜻하지 않는다.
다음 단계는 자동 재실행이 아니라 현재 result를 보존하고, 별도 근거와 승인 아래 미완료 gate를 다루는 것이다.

## Independent actual input candidate 01 — PASS_NO_TRADE

사전등록한 두 번째 날짜 actual fixture를 Windows 네이티브 환경에서 완료했다.

고정 scope:
- source date/session: `2026-09-18 / 21f8c124e64e421893275ccdc83818ad`
- bounded cutoff: `10:00:00 KST exclusive`
- selected instrument: `005930=unknown`
- policy: `unknown_direction_recent_window_quarantine_v0`
- smoke settings: quantity 1 / cash 1,000,000 / fee 0.001 per-side / buy·sell·cancel latency 각 1초 / max quote age 2초 / cooldown 10초 / fixed exit
- execution revision: `8e969ceafe3d296c834e9b365757bf616266a1e4`

Windows source protection은 PASS했다. 원본은 51,394,355,200 bytes, WAL 0 bytes,
SHM 32,768 bytes, journal 없음이었고 collector 관련 process/window 부재와 free lease를 확인했다.
snapshot acquisition은 정확히 1회 수행해 run
`c43a255f907245eaa2f02124fadd6a0c`를 만들었다.
source stream과 working readback SHA-256은 모두
`86e81bca2071545ff130d1e515ea6c0ae4bbf47169e256cefa8502a6e352cf29`로 일치했다.
원본 DB/WAL/SHM identity·size·mtime은 실행 전후 불변이었다.

strict 10:00 prefix는 구조 검증 PASS, `stream_error=null`이며
sentinel 포함 8,241,797건을 소비했다. prefix 이전 raw 8,241,796건 중 tick 8,225,687,
trade 3,081,776, quote 5,143,911, control 16,109건이다.
품질 영향 tick과 paired parse-error는 각각 16,108건이고
`smoke_backtest_eligible=false`, tail/whole-stream/performance-research 미평가를 유지한다.

selected-v2 overlay는 PASS했다.
`005930=unknown` selected tick 30,800 / clean 30,799,
unknown-direction quarantine 1쌍 / zero-quote 0 / disqualifying 0이며
strict 재검증 5항목 모두 true다.

actual replay는 정확히 1회 실행해 `completed_no_fills`로 끝났다.
expected/actual/processed event는 모두 30,800건이며 signals/intents/fills/rejects/transitions가
모두 0이다. 최종 cash 1,000,000, position/open order 0이다.
event SHA는
`ac1657b41dbaa7f1adde7f900b3c9ac9a78764f8041f7426b9ff08f43520537a`다.

`performance_accounting`은 `flat_complete`, fill_count 0,
realized/unrealized/total PnL 모두 0, equity 1,000,000이며
cash/position/accounting identity reconciliation은 모두 true다.
final bid-mark provenance records는 빈 목록이고 내부/최상위 provenance가 일치한다.
legacy top-level realized/unrealized/equity는 계속 null이다.

**최종 판정: `Independent actual input candidate 01: PASS_NO_TRADE`.**

이 결과로 2026-09-21의 PASS_WITH_TRADE와 별개 날짜에서
source protection → frozen snapshot → strict bounded prefix → selected-v2 →
shared simulator/accounting/mark provenance 경로가 다시 성립함을 확인했다.
다만 두 번째 날짜에는 거래가 없으므로 실제 체결·PnL 사례는 추가되지 않았다.
성과 우수성·robustness·performance-research·NXT venue·whole raw·live 적격성은 미승격이다.

다음 단계는 이 과거 데이터에서 cutoff/종목을 바꿔 거래를 만들려는 것이 아니다.
**다음 독립 실제 세션을 결과 확인 전에 같은 방식으로 사전등록하고 품질확인 입력을 축적한다.**
candidate 02는 적격한 새 source/session이 생기기 전까지 자동 선택하지 않는다.

## Exploratory profitability track — 2026-09-21 전용 in-sample 개발

사용자 목표에 따라 별도 **탐색용 트랙**을 연다.
목표는 현재 사용 가능한 bounded actual 입력 하나에서라도 비용·지연을 포함해
`performance_accounting.total_pnl > 0`인 전략 파라미터 조합을 찾는 것이다.

개발 입력은 오직 기존 검증이 끝난
`2026-09-21 / 10:00 KST exclusive / 005930=unknown / selected-v2`
fixture로 고정한다. 이 날짜는 이제 **in-sample development set**으로 취급한다.
2026-09-18 candidate 01과 이후 새 실제 날짜는 이 탐색에 사용하지 않는다.

고정할 실행 가정:
- quantity 1
- initial cash 1,000,000
- fee 0.001 per-side
- buy/sell/cancel latency 각 1초
- max quote age 2초
- unknown-direction policy `unknown_direction_recent_window_quarantine_v0`
- cutoff 10:00 KST exclusive
- source/session/instrument 변경 없음

탐색 가능한 것은 NXT breakout의 **entry/exit 파라미터**다.
cutoff·날짜·종목·비용·지연·direction policy를 수익을 만들기 위해 바꾸지 않는다.

1차 성공 기준은:
- run status가 정상 완료
- `performance_accounting.status=flat_complete`
- fill_count >= 2
- `total_pnl > 0`
- cash/position/accounting reconciliation 모두 true

이 성공은 **in-sample profitable candidate 발견**만 뜻한다.
수익성·robustness·out-of-sample 성과·실전 적격성 주장이 아니다.
탐색한 모든 조합과 결과를 보존하고, 성공 조합만 숨겨서 보고하지 않는다.
후속 검증은 탐색에 사용하지 않은 새 날짜에서 별도로 한다.

## 유지하는 차단 조건

2026-09-21 원본은 약 50.6 GB이며 당시 보고된 0-byte WAL + 32 KiB SHM residue를 보존한다.
whole-file stream integrity·전체 품질·FIRST_RESEARCH_CANDIDATE는 미완료다.
원본/sidecar 처리와 qualification은 대상 identity·외부 reader/writer·namespace 격리·장외 시각·
collector 부재·free space·I/O/time 예산·실패 보존 설계와 별도 승인이 필요하다.
writable in-place cleanup, unsigned 방향 임의 보정, 유리한 종목/시간 사후 선택을 하지 않는다.

`venue=unknown`을 NXT 인증으로 바꾸지 않는다.
Paper/Mock/Live 주문 어댑터와 live trading approval은 별도 미완료다.
closed != data quality pass; sample clean != whole-file clean; stream integrity != research eligibility;
qualification != strategy validation; backtest != live trading approval.

## 개발/검증 원칙

GitHub Actions는 작은 PR/커밋마다 자동 실행하지 않는다.
일반 CI는 주간 정기 실행과 필요 시 수동 실행이며, focused/local 합성 검증과 실제 데이터·OCX 검증을 구분한다.
통과 건수에 deselected/skipped나 중복 focused test를 더하지 않는다.
운영 raw/dump/operations_state/`.venv32`/Daily_baseline/old_data/사용자 변경을 보존한다.
live 중에는 master 병합 보류 규칙을 유지한다.

현재 상세 체크와 historical candidate 근거는 [BACKTEST_TODO](BACKTEST_TODO.md),
코드 연결·진입점 차이는 [PIPELINE_MAP](docs/PIPELINE_MAP.md),
이 압축 전의 상세 수치·파일 경로·PR별 기록은
[2026-09-25 보존본](docs/archive/HANDOFF_20260925_PRE_COMPACT.md)에서 찾는다.
