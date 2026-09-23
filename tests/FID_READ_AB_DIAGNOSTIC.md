# FID read A-B-A diagnostic (Mock-only)

## Why

On 2026-09-23, a 10-code 60s run stayed near 59–68 callbacks/sec with FID clock
difference about 1–3s and no sustained lag climb.

Full-universe (~3,757 codes) 60s baselines at `5b5156f…` showed:

| Session | callbacks/sec | FID clock difference |
| --- | --- | --- |
| Mock | ~1,602 | ~2s → ~16s |
| Live | ~1,533 | ~2s → ~20s |

Python raw-v2 queue peaks were small (`accepted=committed`, `dropped=0`).
A separate x86 offline replay at 1,600 callbacks/sec × 60s kept the producer
schedule, sampled queue peak 32, drain ~0.016s, memory ~+3 MiB.

So “Python raw-v2 storage worker is the primary bottleneck” is a weak explanation.
Operational FID reads are currently:

- TRADE: `20,10,15,14,27,28` → 6 `GetCommRealData` / trade callback
- QUOTE: `21,41..80` → 41 / quote callback

≈ 47k OCX FID reads/sec at full-universe rates.

This experiment asks: **with the same full-universe realtime subscription held
constant, does reducing only in-callback `GetCommRealData` call counts change
FID clock lag growth?**

## What changes (diagnostic ON only)

Independent variable: **GetCommRealData call count** inside the callback.

Does **not** change across phases:

- `SetRealReg` / `REAL_FIDS`
- code count / screen layout
- subscription membership
- default production FID lists when the flag is OFF

## A-B-A schedule (90s)

Relative to `_subscribed_at`:

| Phase | Elapsed | FID set |
| --- | --- | --- |
| A1 | `[0, 30)` | FULL |
| B | `[30, 60)` | ESSENTIAL |
| A2 | `[60, 90)` | FULL |

Pre-subscription callbacks stay FULL. Duration shutdown uses the existing path.

## Essential FIDs (verified against `tick_normalizer.py`)

- TRADE: `20, 10, 15` (3)
- QUOTE: `21, 41, 51, 61..80` (23)

Unread FIDs keep their keys in the raw `fids` dict with value `None`
(no fake strings, no stale reuse).

## Mock only / research ineligible

- Flag: `--fid-read-ab-test`
- Requires: `raw-v2`, `--capture-telemetry`, full universe (no `--codes`),
  no NXT, no aftermarket, teardown OFF, `--duration-seconds 90`
- After login, observed server must be **mock**; live aborts before subscribe
- `feed_scope`: `kiwoom_universe_fid_read_diagnostic`
- Sidecar: `fid_read_ab_test.json` with `diagnostic_only=true`,
  `research_eligible=false`

Default collector behavior with the flag OFF must match current master contracts.

## Lag note

FID clock difference here is **not** network latency. It is an unverified
same-day KST clock difference between provider HHMMSS and receive wall time.
A successful A-B-A lag change does **not** by itself prove a single root cause.

## Next regular-session command (DO NOT RUN in this prep PR)

```text
python -m collector.kiwoom.kiwoom_universe_logger ^
  --storage raw-v2 ^
  --capture-telemetry ^
  --fid-read-ab-test ^
  --duration-seconds 90
```

Use 32-bit collector env, Mock login only, separate explicit approval.
This prep branch must not be merged as a production default change.
