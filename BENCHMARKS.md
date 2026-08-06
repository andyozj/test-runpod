# Benchmarks

Measured 2026-08-07 against endpoint `<endpoint-id: supplied in the submission email>`, image `0.1.0-72e537d-slim`, GPU RTX PRO 6000 MIG 48GB (requested: NVIDIA L40S; platform substituted within the 48GB tier), model per response `model_version`. Rate $1.75/hr (2026-08-05, re-verify before quoting). Raw data: `benchmarks/raw.jsonl` (156 records). Methodology: fixed seed, one variable at a time, N stated per cell.

**Read the N column before any percentile.** N is 3-10 per cell: sequential probes, no sustained load. Enough to rank options (step count, GPU tier, image format); not literal latency guarantees. SLO-grade p50/p95 needs a proper load test (e.g. Locust) at production concurrency, which this harness does not do.

## The comparisons that matter

**Warm vs post-idle vs true cold:** 0.1s vs 16.0s vs 90s p50 (true-cold max 518s, when staging lands on a fresh host). The whole serverless trade in one row: pin an active worker for demos, trust FlashBoot for steady traffic, budget minutes for bursts from zero.

**20 steps vs the 28-step default:** $0.0076 vs $0.0106 per image (28% cheaper) and visually equivalent in the fixed-seed grid (`samples/quality-grid/`): per-pixel different, indistinguishable at arm's length. The default is a quality ceiling, not the value optimum.

**JPEG vs PNG at 1536²:** 0.49MB vs 2.85MB base64, 5.8x smaller for polling clients.

**FlashBoot on vs off:** 1.0s vs 22.0s post-idle start p50, 23x. The spec asserted this benefit before anything was deployed; this row is the assertion closed with data. It costs nothing, which makes disabling it strictly worse.

**48GB tier vs A100 80GB:** $0.0106 vs $0.0108 per image (a tie) at 14.3s vs 21.8s exec p50 (35% faster). FLUX is memory-bandwidth-bound; HBM absorbs the +55% hourly rate. $/hr picks the wrong card, $/image the right one.

## Cold start, decomposed

| Phase | N | delay p50 | delay max |
|---|---|---|---|
| resume | 3 | 16.0s | 16.1s |
| true_cold | 3 | 89.9s | 518.1s |
| warm | 3 | 0.1s | 0.1s |

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

## GPU tiers, same workload (1024², 28 steps)

| Card | N | rate $/hr | $/second | exec p50 | $/image at p50 |
|---|---|---|---|---|---|
| 48GB tier (served: RTX PRO 6000 MIG) | 10 | $1.75 | $0.00049 | 21.8s | $0.0106 |
| A100 80GB PCIe | 5 | $2.72 | $0.00076 | 14.3s | $0.0108 |

$/hr is the pricing-page number; $/image is the one that should pick the card: a faster expensive card can tie or beat a cheaper slow one. Rates as configured in `benchmarks/config.json`, dated there.

## Queue under a 4x burst (12 jobs, workersMax=3)

Queue wait: first job 16s, last 144s, climbing ~11.6s per position: a linear drain, no failures, no timeouts. The queue is the product here: 4x overload became added latency, never an error.

Health timeline (67 samples at ~2s): peak inQueue 12, peak running workers 3, full ramp reached at t+149s. Raw timeline in `benchmarks/raw.jsonl` under `queue:health:timeline`.

## Concurrency

Burst of 6 against workersMax=3: queue wait p50 27.1s, max 50.6s. Execution time is flat; the queue absorbs the burst, which is the design.

## Named outputs

- `AVG_JOB_S = 21.8`: a p50, not a mean; feeds the gateway's `avg_job_s` queue-pressure setting
- Cost per default image (1024², 28 steps): $0.0106 execution-only

## Descoped, and why

| Planned | Status |
|---|---|
| Exact-GPU pinning | Not possible on serverless: scheduling is pool-based (the v2 API takes `gpu.pools`; the catalog assigns each card a pool), which is how an "NVIDIA L40S" request was served by an RTX PRO 6000 MIG. The levers are pool, CUDA filter and datacenter. Per-worker `gpuTypeId` exists in the v2 workers API, 403 on this account today, as are v2 catalog and GraphQL |
| 4090 floor test | Dropped: it would only prove this worker's resident-bf16 policy needs >34GB. Offload (~27GB) and fp8 (~14GB) run FLUX on smaller cards; the claim is scoped in the README instead |
| Weight-delivery three-way | Only cached models is deployed; volume was dropped in design, baked was never pushed or deployed |
| CFG 2× claim | Not measurable through the deployed contract: the input schema deliberately omits `true_cfg_scale` |

## Threats to validity

- Single region, single session, single GPU type; another allocation may differ.
- N bounds variance loosely; no confidence intervals claimed.
- Costs are execution-only; idle-timeout billing between requests is additive and traffic-shaped.
- Cross-check against the RunPod invoice before quoting totals.
