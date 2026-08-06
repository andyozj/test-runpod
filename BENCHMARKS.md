# Benchmarks

Measured 2026-08-06 against endpoint `7jrg4nu4b47fsv`, image `0.1.0-72e537d-slim`, GPU RTX PRO 6000 MIG 48GB (requested: NVIDIA L40S; platform substituted within the 48GB tier), model per response `model_version`. Rate $1.75/hr (2026-08-05, re-verify before quoting). Raw data: `benchmarks/raw.jsonl` (129 records). Methodology: `docs/specs/09-benchmarks.md`; p50/p95 over N runs, fixed seed, one variable at a time.

## Cold start, decomposed

| Phase | N | delay p50 | delay max |
|---|---|---|---|
| resume | 2 | 15.4s | 16.0s |
| true_cold | 2 | 304.0s | 518.1s |
| warm | 2 | 0.1s | 0.1s |

`true_cold` includes image pull and the ~34GB pipeline load; `resume` is FlashBoot restoring a retained worker; `warm` is a live worker taking the next job.

## Steps sweep (1024², seed fixed)

| Steps | N | exec p50 | exec p95 | $/image at p50 |
|---|---|---|---|---|
| 4 | 10 | 3.8s | 4.0s | $0.0018 |
| 10 | 10 | 8.2s | 8.3s | $0.0040 |
| 20 | 10 | 15.7s | 15.9s | $0.0076 |
| 28 | 10 | 21.8s | 22.0s | $0.0106 |
| 40 | 10 | 30.7s | 30.9s | $0.0149 |
| 50 | 10 | 38.2s | 38.2s | $0.0186 |

Quality grid for the same seeds: `samples/quality-grid/`.

## Resolution sweep (28 steps)

| Size | N | exec p50 | exec p95 | $/image at p50 |
|---|---|---|---|---|
| 512² | 10 | 6.7s | 6.7s | $0.0033 |
| 768² | 10 | 12.7s | 12.9s | $0.0062 |
| 1024² | 10 | 21.7s | 22.0s | $0.0106 |
| 1280² | 10 | 36.0s | 39.0s | $0.0175 |
| 1536² | 10 | 55.4s | 55.5s | $0.0269 |

## Response payload (1536²)

| Format | N | base64 p50 | vs 10MB `/run` input cap |
|---|---|---|---|
| jpeg | 3 | 0.49MB | fits |
| png | 3 | 2.85MB | fits |

## Concurrency

Burst of 6 against workersMax=3: queue wait p50 27.1s, max 50.6s. Execution time is flat; the queue absorbs the burst, which is the design.

## Named outputs

- `AVG_JOB_SECONDS = 21.8` — feeds the gateway queue-pressure threshold
- Cost per default image (1024², 28 steps): $0.0106 execution-only

## Descoped, and why

| Planned | Status |
|---|---|
| GPU comparison (A100, 4090) | Descoped by decision: endpoint is L40S-only so every number is one card. The A100 fallback ran exactly one job before the change (14.9s at 28 steps — consistent with its bandwidth advantage) |
| Weight-delivery three-way | Only cached models is deployed; volume was dropped in design, baked is built but not deployed |
| CFG 2× claim | Not measurable through the deployed contract — the input schema deliberately omits `true_cfg_scale` |

## Threats to validity

- Single region, single session, single GPU type; another allocation may differ.
- N bounds variance loosely; no confidence intervals claimed.
- Costs are execution-only; idle-timeout billing between requests is additive and traffic-shaped.
- Cross-check against the RunPod invoice before quoting totals.
