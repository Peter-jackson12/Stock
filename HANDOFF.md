# 현재 인계 — 2026-09-25 / selected-v2 actual accounting regression

[9/26 설계 점검·조치 보류](docs/PIPELINE_AUDIT_20260926.md)

[문서 인덱스](README.md) · [첫 실데이터 체크](BACKTEST_TODO.md) ·
[파이프라인 지도](docs/PIPELINE_MAP.md#portfolio-research) ·
[수집 의사결정 계약](docs/COLLECTION_RUNBOOK.md#collection-decision) ·
[live 작업 경계](docs/COLLECTION_RUNBOOK.md#collection-live-boundary) ·
[세션 근거 표시](docs/SESSION_ASSESSMENT.md) · [보존본 안내](docs/archive/README.md)

매 작업 시작 시 원격 master·열린 PR·최신 CI와 작업 후보 HEAD/base를 다시 확인한다.
아래 값은 현재 인계이며 영구 최신값이 아니다. 2026-09-24~25 상세 실행·수치·경로 원문은
[압축 전 인계 보존본](docs/archive/HANDOFF_20260925_PRE_COMPACT.md)에 그대로 남긴다.

## TotalStock 경로 정리 — 2026-09-25

Git repair 후 9개 checkout의 이전 상태를 확인했다. 현재 경로·가상환경 제한은
[README](README.md#canonical-workspace)를 따른다. 아래 과거 실행 경로는 보존한다.
경로·문서 합성 회귀 40개 통과. push·PR·Actions·실데이터 실행 없음.

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


운영 화면의 읽기 전용 preflight는 기존 admission·32-bit/OCX 점검을 재사용한다.
실행 revision·CLI·시장·승인은 UNVERIFIED이며 시작 버튼과 연결하지 않는다.
설치·로그인·구독·수집·프로세스 제어·raw DB 접근은 하지 않는다.
원격 PR 사실을 운영 PC의 현재 상태로 승격하지 않는다. 상세는 [CONTROL_TOWER](CONTROL_TOWER.md).

Operator preflight/Run Plan 화면 확인은 완료했다. 현재 후보는 사용자 범위의
`Stock Operator.lnk` 생성·제거 기능이며 `stock.cmd ui`를 호출한다.
실제 .lnk 생성 확인은 후보 병합 후 남는다. 관리자 권한·레지스트리·영구 실행 정책 변경과
exe 패키징은 없다. [상세](START_HERE.md)

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
한 왕복 결과를 보고 threshold/exit/parameter를 조정하지 않는다.

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

## 다음 research 단계 — Independent input candidate 01 사전등록

성과를 본 뒤 날짜·종목·cutoff를 고르는 것을 막기 위해 다음 후보를 **실행 전에 고정**한다.

- source date/session: `2026-09-18 / 21f8c124e64e421893275ccdc83818ad`
- historical raw path: `sampledata/raw_ticks_v2/20260918/21f8c124e64e421893275ccdc83818ad.db`
- historical state: 07:55경 시작, 정상 종료 근거 보존. 말미에는 방향 미확인 8 trade + 대응 parse_error 8건이 있어 whole-file 연구 적격성은 계속 차단한다.
- bounded cutoff: **10:00:00 KST exclusive**
- selected instrument: **`005930=unknown`**
- selected policy: **`unknown_direction_recent_window_quarantine_v0`**
- smoke settings: 2026-09-21 selected-v2 PASS와 동일한 quantity 1 / cash 1,000,000 / fee 0.001 per-side / buy·sell·cancel latency 각 1초 / max quote age 2초 / cooldown 10초 / fixed exit

2026-09-17 정상 종료 세션 `cf18cb437b9a4f6ba2abf0fdadbbfe57`은 12:35경 시작했으므로
같은 10:00 bounded comparison의 후보에서 제외한다. 이는 결과를 본 뒤의 성과 선택이 아니라
고정 cutoff를 만족하지 못하는 시간 범위 제외다.

실행 순서는 고정한다.

1. 원본 존재·identity·종료 근거·프로세스 부재·sidecar 상태를 먼저 메타데이터/운영 근거로 확인한다.
   원본을 writable SQLite로 열거나 sidecar를 삭제하지 않는다.
2. 필요 시 기존 raw-v2 frozen snapshot acquisition 경로로 원본을 보존한 별도 working copy를 만든다.
   보호 조건을 만족하지 못하면 **INCONCLUSIVE로 중단**하고 다른 날짜/종목으로 자동 대체하지 않는다.
3. working copy에서 strict 10:00 prefix qualification을 정확히 1회 수행한다.
4. strict 구조 재검증이 가능할 때만 `005930=unknown` selected-v2 overlay를 동일 policy로 정확히 1회 수행한다.
5. selected gate가 true일 때만 위 고정 설정으로 selected-v2 smoke/accounting replay를 정확히 1회 수행한다.
6. FAIL/INCONCLUSIVE에서 cutoff·instrument·policy·parameter를 바꿔 재시도하지 않는다.
   PASS에서도 parameter tuning이나 performance-research 승격을 하지 않는다.

이 후보의 목적은 **두 번째 날짜의 독립 actual regression fixture 확보**다.
전략 수익성 비교, NXT venue 인증, whole raw 승인, live trading readiness가 아니다.

## Independent input candidate 01 — 첫 시도 환경 제약, Windows 실행 대기

사전등록 후 첫 실행 시도는 **`INCONCLUSIVE_SOURCE_PROTECTION`**으로 종료했다.
이는 데이터 품질 실패가 아니라 실행 환경이 보호 계약을 수행할 수 없었던 결과다.

첫 시도 환경은 사용자 Windows PC의 프로젝트를 FUSE로 마운트한 Linux VM이었다.
따라서 Windows local NTFS와 kernel32 sharing semantics를 요구하는
`raw_v2_frozen_snapshot_v1` acquisition을 실행할 수 없었고,
Windows 쪽 collector/Python process·reader/writer 부재도 독립 확인할 수 없었다.
Linux `cp`, 직접 SQLite open 등 비등록 우회는 하지 않았다.
snapshot/strict prefix/selected-v2/smoke/accounting은 모두 **미실행**이며 result도 생성되지 않았다.

원본에 대해 SQLite를 열지 않고 관측한 기준선:
- DB size: `51,394,355,200` bytes
- WAL: `0` bytes
- SHM: `32,768` bytes
- journal: 없음
- historical status: closed / writer_closed=true / final_seq=committed_seq=`43,218,720` / dropped=0
- historical payload SHA-256 claim: `7e82ddf0…c508`
- 시작 시각: 약 07:55 KST / 종료 사유: 장 마감(15:35)
- 관측 전후 DB/WAL/SHM size·mtime·inode 불변
- DB ctime이 2026-09-24 08:47Z로 변경된 원인은 미확인. 내용 불변을 이 ctime만으로 인증하지 않는다.

따라서 candidate 01은 소진되거나 거부된 것이 아니다.
**동일 2026-09-18 / 10:00 KST exclusive / 005930=unknown / 동일 policy·settings를
Windows 로컬 보호 경로에서 처음부터 이어서 실행해야 한다.**
Windows 실행 전에는 현재 process/window/lease와 source identity/sidecar를 새로 확인한다.
source 보호 조건이 맞지 않으면 그대로 INCONCLUSIVE로 남기며 다른 날짜·종목·cutoff로 대체하지 않는다.

첫 VM 시도 중 Git 조회가 만든 `.git/index.lock`, `.git/objects/maintenance.lock` 0-byte 파일은
Windows Git 작업 차단을 피하기 위해 해당 두 파일만 삭제했다는 보고다.
raw/sidecar 및 다른 파일은 변경하지 않았고 이후 VM에서 Git 명령을 추가 실행하지 않았다.

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

CI는 주간/필요 시 수동 실행한다. 합성 검증과 실데이터·OCX 검증을 구분하고 중복 건수는 합산하지 않는다.
raw/dump/operations_state/가상환경/Daily_baseline/old_data/사용자 변경을 보존한다. live 중 병합은 보류한다.

현재 상세 체크와 historical candidate 근거는 [BACKTEST_TODO](BACKTEST_TODO.md),
코드 연결·진입점 차이는 [PIPELINE_MAP](docs/PIPELINE_MAP.md),
이 압축 전의 상세 수치·파일 경로·PR별 기록은
[2026-09-25 보존본](docs/archive/HANDOFF_20260925_PRE_COMPACT.md)에서 찾는다.
