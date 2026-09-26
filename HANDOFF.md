# 현재 인계 — 2026-09-26 / MWFD-04 1,286-cell Fast full run

[문서 인덱스](README.md) · [Fast Backtest v1](docs/FAST_BACKTEST_V1.md) ·
[첫 실데이터 체크](BACKTEST_TODO.md) · [파이프라인 지도](docs/PIPELINE_MAP.md#fast-backtest-v1) ·
[수집 의사결정 계약](docs/COLLECTION_RUNBOOK.md#collection-decision) ·
[live 작업 경계](docs/COLLECTION_RUNBOOK.md#collection-live-boundary) ·
[보존본 안내](docs/archive/README.md) ·
[압축 전 인계 보존본](docs/archive/HANDOFF_20260925_PRE_COMPACT.md)

매 작업 시작 시 원격 master·열린 PR·최신 CI와 작업 후보 HEAD/base를 다시 확인한다.
아래 값은 이번 작업에서 확인한 시점의 기록이며 영구 최신값이 아니다.

## TotalStock 경로 정리 — 2026-09-25

main과 linked worktree 8개의 Git 연결을 공식 repair로 복구하고 이전 branch/HEAD/status와 대조했다.
현재 경로 계약·가상환경 제한은 [README](README.md#canonical-workspace)에 둔다.
현재 코드/예시만 정리하며 아래 과거 구현·실행 경로는 당시 provenance로 보존한다.
두 가상환경은 Python 직접 실행이 가능하지만 옛 launcher 경로가 남아 재생성을 권장한다.
기존 dirty worktree 2개, raw/snapshot/과거 결과/migration-backup은 수정하지 않는다.
Fast/production/경로/Operator/문서 합성 회귀 306개 통과(실패 1건 수정 후 해당 3개 재검증). push·PR·merge·Actions 및 추가 실제 연구 실행은 하지 않는다.

## Fast Backtest v1 — IMPLEMENTED

별도 worktree `C:\Projects\_worktrees\Stock\fast-backtest-v1`, branch
`feat/fast-backtest-v1-20260925`, base `a8b9cac23ff1783aca8033626a91270ad79359f4`에서 구현했다.
구현 checkpoint는 `080dd72`다. 시작 시 원격 master는 위 base였고 열린 PR은
#20, #22, #23, #25, #26, #27, #28이었다. 최신 확인 master CI와 Session assessment regressions는
성공 상태였다. push·PR·merge·Actions 수동 실행은 하지 않았다.

구조:

```text
historical EOD metadata
  → cheap universe cut
  → immutable verified OrderedTick cache
  → causal feature cache
  → canonical/deduplicated fast sweep (screening_only)
  → deterministic top-N
  → existing production exact replay
  → parity/accounting 확인
```

주요 진입점은 `research/fast_backtest/`와 `scripts/run_fast_backtest.py`다.
production exact fill/accounting 의미를 수정하거나 복제하지 않았고, fast 결과는 항상
`screening_only=true`다. fast/exact 차이는 `FAST_EXACT_MISMATCH`로 보존한다.
v1 runner는 한 거래일·한 종목을 지원한다. collector/OCX/login/구독/raw writer/native/FID 경로는
수정하거나 실행하지 않았다.

### 기존 실제 benchmark

2026-09-21 `005930=unknown` 77,558건에서 #268 fast/exact parity와 저장된 exact 상위 10개 대조가
PASS였다. 693 source records는 663 unique parameter로 고정했다. 2026-09-18 고정 holdout은 #268이
양쪽 모두 no-trade였다. 상세 수치·artifact 경로는 [Fast Backtest v1](docs/FAST_BACKTEST_V1.md#benchmark-해석)과
[2026-09-25 보존본](docs/archive/HANDOFF_20260925_PRE_COMPACT.md)에 둔다. 이 결과로 후보를 다시 튜닝하지 않는다.

### Historical universe / PIT

`causal_preopen`은 D 이전의 READY/VALID snapshot 중 timezone-aware `available_at`이 cutoff 이하인
최신본만 선택한다. NOT_READY, availability 누락·지연, same-day EOD, 미래 observation은 fail-closed한다.
현재 size filter는 `total_market_cap_proxy`이며 유통시총이 아니다. historical float 요청은 대체값 없이
실패한다. `TODO-FLOAT-001`은 [BACKTEST_TODO](BACKTEST_TODO.md#fast-backtest-v1)에 유지한다.

### MWFD-02 shared market materialization

별도 worktree `C:\Projects\TotalStock\worktrees\mwfd-02-causal-depth`, branch
`feat/mwfd-02-causal-depth-20260925`에서 구현했다. causal/depth 구현 checkpoint는
`e01f2c7b685eb53b40f819bbdd19eccdc29d262c`다. 원래 Fast HEAD `ac7a7a7`에서 분기했고
기존 Fast worktree와 dirty profitability worktree는 수정하지 않았다.

2026-09-21 frozen bounded prefix를 code별 반복 없이 receive-order로 정확히 한 번 스캔했다.
prefix 8,414,461 records, tick 8,400,558, control 13,903, 3,642 unique code×venue cell을
확인해 source가 multi-code임을 입증했다. 모든 venue는 `unknown`이며 causal universe/NXT/whole-stream
적격성으로 승격하지 않는다. source digest는
`93e833dcb34cb6c28d0c40fcce346023636e0ff13c8a917a642748af8c73a712`다.

`fast_backtest_execution_depth_v1` companion cache는 quote 5,117,806행의 source FID 41..80
10단계 ask/bid 가격·잔량 vector, notional, completeness/reason을 보존한다. 전체 raw_fields를 Fast
cache에 복제하지 않았고, 미래 backfill·가격 추정·missing/0 보간·invalid quote 은폐를 하지 않는다.
cache ID는 `71ab319e…b27cd7`, logical digest는 `47305c9f…07156`이며 전체 payload roundtrip을
통과했다. source size/mtime 불변과 전후 sidecar 부재도 확인했다.

cell admission은 eligible 1,286, no opportunity 1,938, insufficient depth 399,
quality disqualified 12, not assessed 7이다. event gate는 PASS 2,175,048 / FAIL 719,298 /
UNKNOWN 388,406(66.2568%), cell-equal 평균 19.0947%다. clock-time pass ratio는 14.8308%,
cell-equal 평균 14.5045%다. 005930의 77,558 events와 PASS 67,217 / UNKNOWN 160 / FAIL 0은
MWFD-01과 일치한다. 단일 pass는 6,135.999824초, tracemalloc peak 37,601,388 bytes,
materialization output 약 1.5865 GB였다.

artifact는
`C:\Projects\TotalStock\_data\mwfd_02\20260925T220712+0900-shared-market`에 create-only로 둔다.
사람용 결론은 `report-ko.md`, 기계 집계는 `summary.json`/`market_inventory.json`, 다음 표본은
`probe_admission.json`에 있다. 최종 Fast/causal/depth/parity/documentation 81개와 실제 cache 전체 roundtrip이
통과했다. push/PR/merge/Actions, 663 sweep, tuning, production exact 대량 실행, OCX/login,
2026-09-18 holdout 신규 탐색은 하지 않았다.

### 검증

- Fast/PIT/cache/feature/sweep/exact bridge/end-to-end와 기존 production 관련 회귀: 173 passed.
- documentation 링크·필수 연결·HANDOFF 크기 계약: 27 passed.
- 새 모듈·세 benchmark CLI compileall: PASS.
- `git diff --check`: PASS.
- 실제 #268 9/21, #268 9/18, fast top-10 saved exact parity: 모두 PASS.

합성 회귀는 future leakage, rolling/timestamp/session 경계, no signal, single round trip,
stop/trailing exit, spread·OBI·buy-ratio·volume·breakout reject, stale quote, no fill,
parameter dedup, deterministic tie, top-N 호출과 mismatch 보존을 포함한다.

## 유지하는 운영 상태와 차단 조건

수집기/native 트랙은 2026-09-28 실제 시장 세션 전까지 코드 freeze다. 실제 수집 판단 전에는
COLLECTION_RUNBOOK의 collection-decision/live-boundary와 최신 master, process/window/lease,
저장공간, fresh execution approval을 다시 확인한다. 자동 kill/restart/relogin, lock 삭제,
추가 OCX 로그인, FID hot-path 실험, raw/operations_state 접근을 Fast Backtest 권한으로 실행하지 않는다.

두 핵심 snapshot `24f657…`, `c43a255…`와 profitability artifact는 KEEP_CORE다.
삭제·이동하지 않는다. 2026-09-21 selected-v2는 bounded strategy-research input일 뿐
strict prefix/whole stream/performance-research/NXT venue/live 적격성은 미승격이다.
`venue=unknown`을 NXT 인증으로 바꾸지 않는다.

Candidate #268은 2026-09-21 in-sample에서 +4,467, 2026-09-18 holdout에서 no-trade다.
두 날짜만으로 수익성·robustness·실전 적격성을 확정하지 않는다. 9/18 결과를 보고 parameter를
변경하거나 같은 holdout에서 다른 후보를 시험하지 않는다.

### MWFD-03 45-cell runtime probe

전용 worktree `C:\Projects\TotalStock\worktrees\mwfd-03-runtime-probe`, branch
`feat/mwfd-03-runtime-probe-20260926`에서 구현했다. 실행 코드 revision은 `220ddca`다.
MWFD-02 `probe_admission.json`의 high/medium/low 각 15셀과 663 후보를 변경 없이 사용했다.
50.6 GB raw는 8,414,461건 접두를 한 번만 순회해 179,123건 이벤트 캐시를 만들었고, 이후 실행은
공유 depth cache와 이 이벤트 캐시만 사용했다.

45셀·29,835 candidate-cell이 중복 없이 완료됐다. coldish 파이프라인 416.402초, 원천 캐시
322.792초, warm smoke 27.661초, peak working set 323,891,200 bytes다. gate는 PASS 33,516 /
FAIL 18,809 / UNKNOWN 6,084이며 MWFD-02 inventory와 일치한다. 45 checkpoint resume은 전부 skip,
결과 파일 무재작성, digest 불변으로 PASS했다.

1,286셀 단일 워커 추정은 15,522.747초(범위 11,833.331–22,400.368초), output 약 7.57 GB다.
최종 판정은 `FULL_RUN_ADMITTED_WITH_CONDITIONS`: 동일 frozen identity, 단일 워커, create-only checkpoint,
free-disk preflight, screening-only를 유지한다. 병렬 실행은 I/O·메모리 경합 probe 전에는 권장하지 않는다.
artifact는 `C:\Projects\TotalStock\_data\mwfd_03\20260926T004154+0900-45-cell-runtime-probe`에 있다.

### MWFD-04 1,286-cell full run

전용 worktree `C:\Projects\TotalStock\worktrees\mwfd-04-full-run`, branch `feat/mwfd-04-full-run-20260926`.
실행 코드 revision은 `6436255`이며, `7246308`은 provenance 밖 파일 2개(finalize, 호스트 한도 예외 드라이버)만
추가했다. 1,286셀·852,618 candidate-cell이 완료됐고 모든 checkpoint의 code_revision은 `6436255` 하나다.
셀 1–441(252 제외)은 Cowork Linux VM, 442–1286과 셀 252 retry는 로컬 Windows에서 실행했다.
runtime은 호스트별로 해석한다. 호스트가 섞인 합계 20,686초는 MWFD-03 추정 범위 안이다.

finalize 6단계, crosscheck(MWFD-03 45/45, 비분할 3/3), resumecheck, 직접 완료 검증 21/21이 PASS다.
gate는 PASS 2,174,745 / FAIL 464,037 / UNKNOWN 177,682이며 MWFD-02 inventory와 일치한다. 미해결 failure는 0건이다.
셀 252 failure 기록은 `failures_resolved/`에 보존했다. 예외 드라이버가 관여한 셀 279·252는 finalize crosscheck
범위 밖이다. 일반 `CellRunner` 경로로 따로 재계산해 digest가 일치했다(279 8/8, 252 독립 3/3·retry 5/5).
셀 252의 Linux 드라이버 중간 산출물은 남아 있지 않아 직접 비교하지 못했고, 최종 결과에는 쓰이지 않았다.

`run_manifest.json`의 `candidate_family.source_path`는 Linux VM 경로로 남아 있다. Windows 재개는 manifest·코드
수정 없이 임시 junction `C:\sessions\rcw-01fdfe2equdn5tenjzpcmaww\mnt\TotalStock` → `C:\Projects\TotalStock`으로
우회했고, 완료 후 제거했다. 이 run root를 다시 실행하려면 같은 junction이 필요하다.
보고서: `C:\Projects\TotalStock\_data\mwfd_04\MWFD-04_final_report_20260926.md`,
검증 증거: 같은 폴더의 `verification\`. 사용자 진행 지시에 따라 이 인계에 반영했다(2026-09-26).

## 다음 권장 작업

MWFD-04 산출물(`factor_dataset_manifest.json`)을 입력으로 하는 MWFD-05는 별도 승인 후 진행한다.
push·PR·merge와 `win_to_local`의 Linux 경로 처리 수정 여부는 컨트롤타워가 판단한다.
threshold·후보 조정, production exact 대량 실행, holdout 재탐색은 여전히 허용 범위 밖이다.

상세 실행 계약·benchmark·재현 명령은 [Fast Backtest v1](docs/FAST_BACKTEST_V1.md),
현재 체크 항목은 [BACKTEST_TODO](BACKTEST_TODO.md), 코드 연결은
[PIPELINE_MAP](docs/PIPELINE_MAP.md#fast-backtest-v1)에 둔다. 이전 운영/PR별 장문 기록은
[2026-09-25 보존본](docs/archive/HANDOFF_20260925_PRE_COMPACT.md)을 필요할 때만 읽는다.
