# 현재 인계 — 2026-09-25 / Fast Backtest v1 구현

[문서 인덱스](README.md) · [Fast Backtest v1](docs/FAST_BACKTEST_V1.md) ·
[첫 실데이터 체크](BACKTEST_TODO.md) · [파이프라인 지도](docs/PIPELINE_MAP.md#fast-backtest-v1) ·
[수집 의사결정 계약](docs/COLLECTION_RUNBOOK.md#collection-decision) ·
[live 작업 경계](docs/COLLECTION_RUNBOOK.md#collection-live-boundary) ·
[보존본 안내](docs/archive/README.md) ·
[압축 전 인계 보존본](docs/archive/HANDOFF_20260925_PRE_COMPACT.md)

매 작업 시작 시 원격 master·열린 PR·최신 CI와 작업 후보 HEAD/base를 다시 확인한다.
아래 값은 이번 작업에서 확인한 시점의 기록이며 영구 최신값이 아니다.

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

### 실제 benchmark

2026-09-21 `005930=unknown`, 10:00 KST exclusive selected-v2 입력 77,558건을 사용했다.
materialized event SHA-256은
`a6fbcce85c321da1ee07f898f525537e8beb2361cc5769d08174935531741e48`다.

- 고정 #268: cache 2.143초, feature 11.768초, fast 0.093초, 총 16.154초.
  production exact와 entry/exit decision, fills 2, trade 1, buy 264,000, sell 269,000,
  fees 533, net PnL +4,467이 같아 parity `PASS`.
- frozen raw 보호 재검증 benchmark도 parity `PASS`. 50.6 GB raw의 selected event materialization
  1,651.357초, cache 7.026초, feature 44.831초, fast 0.401초, production exact 1,593.937초,
  총 3,316.661초, peak traced memory 213,372,197 bytes였다. `tracemalloc`을 켠 raw 스캔 2회
  비용이므로 materialized-input sweep과 분리한다.
- 기존 693 records를 663 unique parameter로 중복 제거했다.
  cache 2.167초, feature 45.689초, fast sweep 62.924초, 총 113.236초.
  sweep 평균 약 0.095초/candidate, peak working set 266,194,944 bytes,
  output/cache 합계 77,299,018 bytes.
- fast 상위 10개는 보존된 production exact 결과와 모두 parity `PASS`.
  해당 과거 exact elapsed 합은 188.820초다. 새 exact replay를 10회 더 실행하지 않았다.
- 과거 production exact 693회 누적 elapsed는 11,918.286초다.
  fast sweep만의 비율은 약 189배, cache/feature 포함 비율은 약 105배 규모지만,
  과거 값은 개별 run elapsed 합이고 새 값은 한 프로세스 wall-clock이므로 공식 동등 speedup은 아니다.

근거:

- `C:\Projects\_data\Stock\fast_backtest\20260925T190000+0900-reference-268-saved\benchmark.json`
- `C:\Projects\_data\Stock\fast_backtest\20260925T183131+0900-reference-268\benchmark.json`
- `C:\Projects\_data\Stock\fast_backtest\20260925T190100+0900-full-663-top10\benchmark.json`

2026-09-18 holdout도 보존된 30,800 events와 production exact 결과를 대조했다.
#268 fast/exact 모두 signal/fill/trade 0, PnL 0으로 parity `PASS`다.
근거는 `C:\Projects\_data\Stock\fast_backtest\20260925T185500+0900-holdout-268\benchmark.json`이다.
#268을 다시 튜닝하지 않았다.

### Historical universe / PIT

실제 KRX probe source
`C:\Projects\_data\Stock\krx_pit_probe\20260925T173445+0900-01\normalized\20260918.csv`를
adapter로 읽었다. 2026-09-21 `causal_preopen`은 2026-09-18 EOD를 선택했고 2,869종목,
duplicate 0, `005930` 포함을 확인했다. threshold 미지정은 no-op다.

기본 mode는 `causal_preopen`: D보다 앞선 실제 거래일 중 최신 EOD만 사용한다.
`posthoc_same_day`는 명시적 opt-in일 때만 허용하며 `non_causal=true`다.

**현재 historical size filter는 전체 시가총액이며 유통시총이 아니다.**
raw field는 `market_cap_krw`, provenance는 `size_filter_basis=total_market_cap_proxy`다.
float filter 요청 시 데이터가 없으면 명시적으로 실패하며 전체 시가총액으로 대체하지 않는다.

`TODO-FLOAT-001`은 [BACKTEST_TODO](BACKTEST_TODO.md#fast-backtest-v1)에 추적한다.
전종목 Kiwoom opt10001 daily free-float history의 raw observation, retry/rate limit,
completeness/누락 탐지, immutable daily history, 자동 실행·알림과 `float_market_cap` 파생이 범위다.
이번 구현에는 포함하지 않았다.

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

## 다음 권장 작업

Fast Backtest v1의 자연스러운 다음 단계는 **여러 종목 × 여러 날짜 development panel에서
고정 parameter candidate를 평가하는 것**이다. 새 적격 input과 PIT metadata를 먼저 고정하고,
fast screening 뒤 top-N만 production exact로 재실행한다. 대규모 panel은 이번 작업에서 시작하지 않는다.

상세 실행 계약·benchmark·재현 명령은 [Fast Backtest v1](docs/FAST_BACKTEST_V1.md),
현재 체크 항목은 [BACKTEST_TODO](BACKTEST_TODO.md), 코드 연결은
[PIPELINE_MAP](docs/PIPELINE_MAP.md#fast-backtest-v1)에 둔다. 이전 운영/PR별 장문 기록은
[2026-09-25 보존본](docs/archive/HANDOFF_20260925_PRE_COMPACT.md)을 필요할 때만 읽는다.
