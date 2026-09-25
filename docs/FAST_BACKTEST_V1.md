# Fast Backtest v1

Fast Backtest v1은 production exact backtest를 대체하지 않는 **research screening pipeline**이다.
historical cheap universe와 검증된 normalized tick 입력을 한 번 고정한 뒤 causal feature를 한 번 계산하고,
중복을 제거한 parameter candidate를 가볍게 순위화한다. 최종 판단은 상위 N개를 기존 production exact
engine으로 다시 실행한 결과가 담당한다.

```text
historical EOD metadata
  → cheap universe cut
  → verified OrderedTick cache
  → causal feature cache
  → deduplicated fast sweep (screening_only)
  → deterministic top-N
  → production exact replay
  → parity/accounting 확인
```

## 역할과 안전 경계

- fast 결과는 항상 `screening_only=true`다.
- production exact의 fill·accounting·reconciliation 의미를 복제하거나 바꾸지 않는다.
- fast 결과와 exact 결과가 다르면 `FAST_EXACT_MISMATCH`를 보존한다. exact 값으로 fast 값을 덮지 않는다.
- collector, OCX, 로그인, 구독, raw writer, telemetry, native/FID 진단 경로를 수정하거나 호출하지 않는다.
- v1 runner는 한 run에 한 거래일·한 종목을 받는다. 여러 종목×여러 날짜 panel은 다음 단계다.
- 입력 적격성은 caller provenance에서 상속한다. cache 생성이 whole-stream 또는 performance-research 적격성을 만들지 않는다.

## Historical universe와 PIT 계약

기본 `universe_mode`는 `causal_preopen`이다. 거래일 D에는 `as_of_date < D`인 실제 metadata
snapshot 중 가장 최근 날짜를 쓴다. 달력상 전날을 임의 생성하지 않고, D 당일 또는 미래 EOD를 사용하지 않는다.

`posthoc_same_day`는 `posthoc_same_day_opt_in=true`일 때만 허용하며 결과에
`non_causal=true`, `screening_only=true`를 기록한다. 장 시작 전에 선택 가능했던 universe로 해석하지 않는다.

지원하는 cheap filter는 시장, 종가, 거래대금, 전체 시가총액이다. threshold를 지정하지 않으면 no-op다.

**현재 historical backtest의 size filter는 전체 시가총액이다. 유통시총 데이터로 가장하지 않는다.**

manifest의 `size_filter_basis`는 `total_market_cap_proxy`, 원시 값은 `market_cap_krw`다.
`min_float_market_cap` 또는 `max_float_market_cap`이 요청됐는데 historical float field가 없으면
명시적으로 실패한다. 전체 시가총액으로 조용히 대체하지 않는다.

## TODO-FLOAT-001

**전종목 Kiwoom opt10001 daily free-float history pipeline**은 별도 후속이다. 이번 v1에는 구현하지 않았다.

필요 범위:

- 매 거래일 전 종목 유통비율·유통주식수·전체 시가총액 수집
- source/provider, `captured_at`, raw observation 보존
- retry, rate limit, completeness check, 누락 종목 탐지
- 날짜별 immutable history, 자동 실행, 실패 알림·운영 상태
- `float_market_cap` 파생

**향후 Kiwoom opt10001 daily free-float history가 충분히 쌓이면 동일 기간에서
total-market-cap filter와 float-market-cap filter를 비교한다.** 현재 값을 과거 날짜로 backfill하거나
legacy `float=60`을 사용하지 않는다.

## Plan contract

`fast_backtest_plan_v1`은 다음을 고정한다.

- trade dates, instruments, universe source/mode
- historical metadata source와 cheap filter spec
- tick input provenance, cutoff
- account assumptions
- candidate source와 canonical parameter identities
- deterministic top-N ranking
- output directory, seed, code revision

계획 digest는 정렬된 canonical JSON의 SHA-256이다. candidate file을 읽은 뒤 deduplicated identity 집합이
plan과 다르면 실행하지 않는다.

## Cache identity

### Input cache

`fast_backtest_input_cache_v1` identity:

- source dataset identity
- trade date, instrument, exclusive cutoff, policy
- source, session_id
- event count, canonical event SHA-256

payload는 `events.jsonl`, 계약은 `manifest.json`으로 create-only content-addressed directory에 둔다.
읽을 때 event count/digest와 receive order를 다시 검증한다.

### Feature cache

`fast_backtest_feature_cache_v1`은 input event digest와 다음 window family를 고정한다.

- `recent_ticks`
- `breakout_window_seconds`
- `session_start_seconds`
- `max_quote_age_ns`

각 row는 현재 또는 이전 이벤트만 사용한다. prior breakout high는 현재 trade를 넣기 전에 계산하고,
recent volume/buy ratio는 현재 trade까지 포함한다. exact second window boundary는 포함한다.
unknown direction이 recent window에 남아 있으면 buy ratio는 unavailable이며 신규 진입을 막는다.

## Fast sweep와 exact bridge

숫자 표현을 canonicalize한 parameter identity로 같은 candidate를 한 번만 계산하고 source alias를 보존한다.
순위는 `net_pnl` 내림차순, canonical identity 오름차순이다. open position candidate는 v1 ranking에서 뒤로 둔다.

fast simulator는 single-instrument, long-only, top-of-book screening model이다. quote freshness, displayed
liquidity refresh, latency, fee, stop loss, fixed/tick/step trail을 반영하지만 production authoritative 결과가 아니다.
상위 N개는 기존 `run_nxt_portfolio` 또는 selected-v2 public runner가 다시 실행한다.

비교 항목:

- entry/exit decision point
- fill 수, round trip 수
- net PnL
- deterministic candidate ordering

합성 회귀는 no signal, single buy/sell, stop loss, trailing exit, spread/OBI/buy-ratio/volume/breakout
rejection, session boundary, stale quote, no fill, tie를 포함한다.

## 실행

일반 normalized 입력:

```powershell
.venv\Scripts\python.exe scripts\run_fast_backtest.py `
  --plan C:\path\plan.json `
  --metadata-csv C:\path\historical-metadata.csv `
  --events-jsonl C:\path\verified-ordered-ticks.jsonl `
  --candidates C:\path\candidates.json
```

고정 #268 selected-v2 reference benchmark:

```powershell
.venv\Scripts\python.exe scripts\benchmark_fast_backtest_reference.py `
  --raw C:\path\frozen-working.db `
  --selected-report C:\path\selected-prefix-result.json `
  --output-root C:\Projects\_data\Stock\fast_backtest\<run-id>
```

보존된 materialized input/exact 결과와 전체 고유 후보 benchmark:

```powershell
.venv\Scripts\python.exe scripts\benchmark_fast_backtest_saved_reference.py `
  --events-jsonl C:\path\materialized_events.jsonl `
  --exact-result C:\path\production-result.json `
  --trade-date 2026-09-21 `
  --candidate-id 268 `
  --output-root C:\Projects\_data\Stock\fast_backtest\<run-id>

.venv\Scripts\python.exe scripts\benchmark_fast_backtest_full_sweep.py `
  --events-jsonl C:\path\materialized_events.jsonl `
  --summary-jsonl C:\path\summary.jsonl `
  --strict-report C:\path\strict-result.json `
  --exact-top-n 10 `
  --output-root C:\Projects\_data\Stock\fast_backtest\<run-id>
```

두 명령 모두 output directory를 새로 만들며 덮어쓰지 않는다. 대용량 cache/result는 Git에 넣지 않는다.

## Benchmark 해석

실제 benchmark 입력은 `2026-09-21 / 005930 / 10:00 KST exclusive / selected-v2` 77,558건과
기존 탐색의 693 records/663 unique parameter다. materialized event 원본 SHA-256은
`a6fbcce85c321da1ee07f898f525537e8beb2361cc5769d08174935531741e48`다.

- #268: verified input cache 2.143초, feature build 11.768초, fast 평가 0.093초, 총 16.154초.
  production exact와 의사결정 시점·2 fills·1 trade·fee 533·net PnL `+4467`이 모두 같아 parity `PASS`다.
- frozen raw에서 selected-v2를 다시 보호 검증한 별도 #268 측정은 event materialization 1,651.357초,
  cache 7.026초, feature 44.831초, fast 0.401초, production exact 1,593.937초,
  총 3,316.661초, peak traced memory 213,372,197 bytes였고 parity `PASS`다.
  이 실행은 `tracemalloc`을 켠 50.6 GB raw 보호 스캔 2회를
  포함하므로 아래 materialized-input sweep과 같은 성능 구간으로 비교하지 않는다.
- 전체 663 unique: cache 2.167초, feature build 45.689초, sweep 62.924초,
  cache 포함 총 113.236초. sweep 평균은 후보당 약 0.095초다.
- 전체 benchmark peak working set은 266,194,944 bytes, output/cache 합계는 77,299,018 bytes다.
- fast 상위 10개는 보존된 production exact 결과와 모두 parity `PASS`였다. 해당 exact run elapsed 합은
  188.820초이며 이번 benchmark는 이를 새로 실행하지 않고 저장 결과를 대조했다.
- `2026-09-18` 고정 holdout 30,800건도 #268 fast/exact가 signal/fill/trade 0, PnL 0으로 parity `PASS`다.

과거 production exact baseline은 693회(고유 parameter 663개), 개별 run elapsed 합계 11,918.286초다.
fast sweep 구간만 비교하면 약 189배, cache·feature까지 포함하면 약 105배 규모의 차이다. 다만 과거 값은
개별 exact elapsed의 합이고 새 값은 한 프로세스 wall-clock이므로 동일 정의의 공식 speedup으로 해석하지 않는다.
fast의 순위와 PnL은 계속 screening-only이며 최종 top-N은 production exact가 판정한다.

근거 artifact:

- `C:\Projects\_data\Stock\fast_backtest\20260925T190000+0900-reference-268-saved\benchmark.json`
- `C:\Projects\_data\Stock\fast_backtest\20260925T183131+0900-reference-268\benchmark.json`
- `C:\Projects\_data\Stock\fast_backtest\20260925T190100+0900-full-663-top10\benchmark.json`
- `C:\Projects\_data\Stock\fast_backtest\20260925T185500+0900-holdout-268\benchmark.json`

## 알려진 한계

- historical free-float ratio/shares/market cap 미지원
- total market cap은 float market cap이 아님
- 한 run 한 종목·한 날짜
- fast는 screening-only이며 exact accounting을 대신하지 않음
- selected-v2 adapter의 input eligibility와 whole-stream 차단 조건을 승격하지 않음
- #268 evidence는 2026-09-21 development input과 고정 2026-09-18 holdout 범위에 한정됨
