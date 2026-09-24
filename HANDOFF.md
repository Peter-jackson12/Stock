# 현재 인계 — 2026-09-25 / Operator preflight + selected-v2 accounting regression

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

**research/backtest/execution 트랙의 다음 한 단계는
`005930 selected-v2 actual accounting regression` 1회다.**
다중 전략 플랫폼, parameter tuning, 실주문은 현재 목표가 아니다.

Operator UX는 PR #45/#49/#50까지 master에 통합됐다.
`stock.cmd/stock.ps1`의 help/doctor/status/ui와 운영 화면의 상태·환경 요약은 사용 편의 계층이며,
수집 준비·OCX 준비·시장 상태·데이터 품질·실행 승인을 만들지 않는다.

운영 화면에는 환경 목록과 분리된 **수집 전 읽기 전용 preflight**가 추가됐다.
기존 collector admission의 process/window·lease·disk reader와 로그인 없는 공식 32-bit/OCX
preflight만 재사용한다. 각 축을 PASS/WARN/BLOCKED/UNVERIFIED와 다음 확인으로 표시하지만,
실행 대상 revision·CLI 계약과 시장 날짜·장 구간·실행 승인은 UNVERIFIED다. 시작 버튼과 연결하지 않으며
설치·수정·로그인·구독·수집·kill/restart·lock 삭제·시장 조회·raw DB 접근은 하지 않는다.
GitHub/PR 사실을 운영 PC의 현재 process/window/lease 상태로 승격하지 않는다.

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

## accounting 현재 상태와 다음 실행

PR #46~#48로 다음이 master에 들어왔다.
- fee-inclusive weighted-average cost와 exact realized accounting
- cash/accounting reconciliation
- NXT result의 `performance_accounting_v1`
- final fresh-valid-bid mark provenance
- stale/missing/locked-crossed/invalid quote의 fail-closed unpriced 처리

다음 실행은 기존 frozen working DB + selected-v2 report + 기존 smoke 설정으로 **정확히 1회**만 한다.
새 accounting/mark 코드 때문에 code SHA와 reproducibility key가 과거 run과 달라지는 것은 정상이다.
비교 대상은 event SHA/count, signals, intents, fills, transitions, final account 등 execution trace다.

flat 왕복의 기대 accounting:
- realized/total: `-2039.5`
- unrealized: `0`
- equity: `997960.5`
- cash/position/accounting reconciliation: true
- flat이므로 final bid-mark provenance records: 빈 목록

이 실행은 accounting 기능 회귀다. 전략 수익성이나 performance-research 승격이 아니다.
자동 재시도·추가 성과 탐색·사후 cutoff 변경은 하지 않는다.

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
