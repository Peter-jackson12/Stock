# 현재 인계 — 2026-09-25 / execution NXT close preflight

[문서 인덱스](README.md) · [첫 실데이터 체크](BACKTEST_TODO.md) ·
[파이프라인 지도](docs/PIPELINE_MAP.md#portfolio-research) ·
[수집 의사결정 계약](docs/COLLECTION_RUNBOOK.md#collection-decision) ·
[live 작업 경계](docs/COLLECTION_RUNBOOK.md#collection-live-boundary) ·
[세션 근거 표시](docs/SESSION_ASSESSMENT.md) · [보존본 안내](docs/archive/README.md)

매 작업 시작 시 원격 master·열린 PR·최신 CI와 작업 후보 HEAD/base를 다시 확인한다.
아래 값은 현재 인계이며 영구 최신값이 아니다. 2026-09-24~25 상세 실행·수치·경로 원문은
[압축 전 인계 보존본](docs/archive/HANDOFF_20260925_PRE_COMPACT.md)에 그대로 남긴다.

## 현재 두 트랙

**수집기/native 트랙은 2026-09-28 실제 시장 세션까지 코드 freeze 상태다.**
월요일 기본 경로는 현재 수집 계약에 따라 장전/NXT 보존을 먼저 고려하면서 최신 정상 collector로
실제 수집을 우선한다. 별도 Mock FID read A-B-A는 정상 수집의 선행 필수 절차가 아니다.
과거와 유사하게 프로세스가 살아 있는 동안 callback/accepted 진행이 멈추거나 silence 한도를 넘는
feed 정지, Runtime/OpenAPI/native 오류가 재발하면 해당 세션 evidence를 보존하고 원인을 단정하지 않은 채
기존 admission/run-plan/A-B-A 절차를 진단 fallback으로 사용한다. 정상 수집이 유지되면 A-B-A는 실행하지 않는다.
실행 직전에는 공식 시장 운영, 최신 master, HANDOFF/COLLECTION_RUNBOOK, preflight, process/window/lease,
저장공간과 fresh execution approval을 다시 확인한다. 그 전에는 FID hot path·새 진단 계층·OCX/QAx·
queue/teardown/telemetry·#327/#162 원인 실험을 늘리지 않는다. 자동 kill/restart/relogin, Runtime 창 닫기,
lock 삭제, LAA 변경, queue 확대, 기본 FID 축소도 하지 않는다.

**research/backtest/execution 트랙의
`005930 selected-v2 actual accounting regression` 1회는 PASS로 완료했다.**
추가 rerun·parameter tuning·실주문은 현재 목표가 아니다.

**execution/NXT portfolio runner의 `close_ns` 사전 검증 보강은 개발 후보에서 완료했다.**
generic runner와 같은 positive exact-int 조건과 예외 메시지를 출력 생성 전에 적용했다.
`None/False/True/0/-1/20.0/"20"/NaN/Infinity`가 입력 iteration과 output root 생성 전에
거부되는 회귀를 추가했고, 지정한 adapter/simulator/accounting focused 검사는 101건 통과했다.
실데이터·qualification·로그인·수집·주문·parameter tuning은 수행하지 않았다.

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

지정 commit `6d878d93329c136157fbaaed61c4f950dddaea10`에서 frozen working DB와 고정 설정으로
정확히 1회 실행했다. 과거 기준과 event/settings 및 모든 경제적 결과가 동일했고 77,558건 처리,
final cash `997960.500`, position/open order/reject 0을 확인했다. `performance_accounting`은
`flat_complete`, fill 2, realized/total `-4079/2`, equity `1995921/2`, reconciliation 모두 true다.
**`005930 selected-v2 actual accounting regression: PASS`**.
근거 anchor는 result run `ed5e78db61da4646ad186ac943c1c5f4`, reproducibility key
`677bac5071d2ce07468ad909e6d6760764bf1f9a6e454ba30bc12f65a471780a`, valuation time
`10877071329700`이다. 당시 전체 경로·mark provenance 문맥은 직전 master `6fa2ccdcbf0ea2431b81c52d3ed6fb0deec39fac`의 HANDOFF 원문에 남아 있다.
이는 bounded execution/accounting 확인일 뿐 수익성·NXT venue·whole raw·live 적격성 승격이 아니다.

## Independent actual input candidate 01 — PASS_NO_TRADE

사전등록한 두 번째 날짜 actual fixture를 Windows 네이티브 환경에서 완료했다.

고정 scope:
- source date/session: `2026-09-18 / 21f8c124e64e421893275ccdc83818ad`
- bounded cutoff: `10:00:00 KST exclusive`
- selected instrument: `005930=unknown`
- policy: `unknown_direction_recent_window_quarantine_v0`
- smoke settings: quantity 1 / cash 1,000,000 / fee 0.001 per-side / buy·sell·cancel latency 각 1초 / max quote age 2초 / cooldown 10초 / fixed exit
- execution revision: `8e969ceafe3d296c834e9b365757bf616266a1e4`

Windows source protection과 frozen snapshot acquisition 1회, strict 10:00 prefix 구조 검증,
selected-v2 overlay가 모두 PASS했다. 원본 identity·size·mtime은 전후 불변이며 source/working SHA가
일치했다. `005930=unknown` 입력 30,800건 중 clean 30,799, unknown-direction quarantine 1쌍,
zero-quote/disqualifying 0이었다. 실제 replay는 정확히 1회 `completed_no_fills`로 끝나
signals/intents/fills/rejects/transitions 0, cash 1,000,000, position/open order 0을 기록했다.
`performance_accounting`은 `flat_complete`, 모든 PnL 0, reconciliation 모두 true다.
근거 anchor는 snapshot run `c43a255f907245eaa2f02124fadd6a0c`, source/working SHA-256
`86e81bca2071545ff130d1e515ea6c0ae4bbf47169e256cefa8502a6e352cf29`, event SHA
`ac1657b41dbaa7f1adde7f900b3c9ac9a78764f8041f7426b9ff08f43520537a`다.

**최종 판정: `Independent actual input candidate 01: PASS_NO_TRADE`.**

별개 날짜에서 전체 경로가 다시 성립했지만 체결·PnL 사례는 추가되지 않았다.
성과·robustness·performance-research·NXT venue·whole raw·live 적격성은 미승격이다.
과거 cutoff/종목을 바꾸지 않으며 candidate 02는 새 적격 source/session 전까지 자동 선택하지 않는다.

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
