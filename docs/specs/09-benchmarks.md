# 09 — Benchmarks

`BENCHMARKS.md` is the primary deliverable alongside the working endpoint. A generated image proves the task was completed. A rigorous benchmark proves the deployment was *understood* — where the time goes, what it costs, where it breaks, and which GPU is actually right rather than assumed.

Every number in it must survive being questioned.

## Scope discipline

**This is the last thing that happens.** It runs once, in Phase 2b, after everything else works. Nothing here is a reason to build anything earlier.

Phase 2a writes only the harness, its config, and the report skeleton — enough that 2b is a single execution pass with no decisions left in it. Every measurement below must be obtainable from **one benchmark run plus the logs the system already emits**. Anything requiring new instrumentation on the hot path is out of scope by definition.

The measurement list is deliberately capped. Adding a sweep costs GPU minutes and credits, and a benchmark that does not finish is worth less than a smaller one that does.

## Methodology rules

These are the difference between a benchmark and a screenshot of one run.

| Rule | Reason |
|---|---|
| **N ≥ 10 per configuration** | One sample is an anecdote. Serverless latency has real variance |
| **Discard the first run** after any cold start | It measures load, not inference, and mixing them hides both |
| **Report p50, p95, min, max** — never a bare mean | A mean conceals the tail, and the tail is what a user experiences |
| **Record hardware, date, image tag, driver, `diffusers` version** | A number without provenance cannot be reproduced or defended |
| **Fixed seeds throughout** | Removes generation variance as a confound |
| **One variable at a time** | A sweep that changes steps and resolution together measures neither |
| **State the rate source and its date** | Prices change. An undated cost figure rots silently |

Per [`STANDARDS.md`](../../STANDARDS.md) §10, anything not measured this way is labelled an estimate.

## What gets measured

### 1. Cold start, decomposed

The single most important serverless number, and useless as a single figure. Broken into:

| Segment | How | Visible from |
|---|---|---|
| Image pull | First-ever request on a fresh worker | RunPod dashboard |
| Container start → handler import | Timestamp at import | Worker stdout |
| Pipeline load (33GB → VRAM) | `pipeline_loaded` duration | Worker stdout |
| First inference | `generation_completed` | Worker stdout |

Reported separately for a true cold start versus a FlashBoot resume. Conflating them makes the FlashBoot benefit invisible, and that benefit is the main argument for the module-level warm-up design in [01](01-worker.md).

### 2. Latency sweeps

One variable at a time, at 1024² unless stated.

- **Steps:** 4, 10, 20, 28, 40, 50 — establishes the linear region and where quality stops improving
- **Resolution:** 512², 768², 1024², 1280², 1536² at 28 steps

`timings.upload_s` is recorded alongside inference for every run. The worker holds the GPU while uploading, so upload time is on the hot path and belongs in the latency figure rather than outside it.

**Named output: `AVG_JOB_SECONDS`.** The p50 at the default configuration — 1024², 28 steps, on the recommended GPU — is the constant driving the queue-pressure threshold in [02](02-gateway-core.md#queue-pressure). It is currently an estimate ([08](08-production-readiness.md) gap #9), and this sweep is what replaces it. Recorded explicitly as config output, not left to be inferred from a table.

### 3. GPU comparison

The measurement that turns a guess into a recommendation.

| GPU | VRAM | Serverless rate |
|---|---|---|
| L40S | 48GB | $1.75/hr |
| A100 80GB | 80GB | $2.72/hr |
| RTX 4090 | 24GB | $1.10/hr |

Rates from the RunPod pricing page, 2026-08-05. Re-verify before publishing.

The 4090 run doubles as the VRAM-boundary test — bf16 weights are ~34GB resident (23.8GB transformer + ~9.5GB T5-XXL + CLIP + VAE), so a 24GB card is expected to fail **at pipeline load**, before any generation. **Where it fails is a result, not a failure of the benchmark**: it establishes the VRAM floor empirically and is the basis for the GPU recommendation in [01](01-worker.md). If it somehow loads (driver-level paging), the resolution sweep finds the working boundary instead.

### 4. Cost per image

Derived, not quoted:

```
cost = execution_seconds × (hourly_rate / 3600)
```

Reported per GPU per configuration, plus the honest version that includes idle-timeout billing between requests at a stated request rate. Execution-only cost understates real spend for bursty traffic, and bursty is the normal case.

Storage adds nothing for the deployed variant: cached models are staged by the platform and bill nothing, and no images are stored. The baked variant's only storage cost is the registry. This is a change worth stating — an earlier design carried a per-GB monthly bill that grew whether or not anyone submitted a request.

Cross-check the derived figure against RunPod's reported spend for the benchmark run. A model that disagrees with the invoice is wrong.

### 5. Throughput and concurrency

- Sequential requests against one warm worker — steady-state ceiling
- N concurrent requests against max_workers=3 — measures queue wait separately from execution time

Queue wait is the number a user feels and the one a latency figure omits.

### 5b. End-to-end, as the caller sees it

One extra column, from the client that already exists.

Every worker-side figure above excludes the parts a caller actually waits through: queue wait, then **up to one reconciler tick (2s) before we notice completion**, then the image proxy transfer. The demo client already timestamps submit and first-successful-fetch, so the delta is recorded for free.

| Reported | Meaning |
|---|---|
| `inference_s` | What the GPU did |
| `observed_s` | Submit → image in hand |
| Delta | Everything the system adds around the model |

Worth one row because it is the only latency anyone experiences, and because the reconciler tick is a design parameter — if the delta is dominated by it, that is an argument for a shorter tick or for SSE, decided from data rather than taste.

### 6. Resource ceilings

- Peak VRAM per configuration via `torch.cuda.max_memory_allocated()`
- Maximum resolution before OOM, per GPU

Response payload size **matters again**, because the worker returns base64 by default ([01](01-worker.md)). Request caps are documented — 10MB for `/run`, 20MB for `/runsync` — but no ceiling is published for the `/status` response, which is the one carrying the image.

Measured at 512², 1024² and 1536², PNG and JPEG. If 1536² PNG exceeds the limit, JPEG becomes the default at high resolutions — a decision this measurement exists to make. Object storage would sidestep the question entirely and is not built ([08](08-production-readiness.md) gap #10), so the measurement has to settle it.

### 7. Weight delivery: cached models versus baked

Three endpoints, identical worker code, differing only in where weights come from. The headline platform comparison.

The third endpoint costs almost nothing to add: it reuses the ~10GB volume image with no volume attached — the **Model** field on the endpoint names the HF repo, RunPod pre-stages it on host machines before the worker starts (unbilled), and `WEIGHTS_PATH` points at the cache snapshot path ([01](01-worker.md#weight-path)). Cached models are RunPod's stated recommendation for HF-hosted models, so a weight-delivery comparison that omits them measures yesterday's platform. If 2b time runs short, this endpoint is the one dropped — the two-way comparison stands on its own.

**Precondition, run first.** Every endpoint must report the same `sha256` over its weight files ([07](07-testing.md)). If they differ, one is running a different revision — a stale volume, a stale image, or a cache staged from `main` rather than the pinned SHA — and every row below compares two variables at once. **The comparison is void until this passes.**

Deliberately *not* a pixel comparison. Two invocations land on different physical GPUs, and bf16 kernels and cuDNN algorithm selection are not bit-reproducible across hosts — an image diff would fail for reasons that have nothing to do with weights, and this spec would then discard its headline comparison over a non-problem. The hash is exact, costs no GPU time, and needs no credits.

| Measured | Why it matters |
|---|---|
| Image size and push duration | Iteration speed during development, and the practical cost of a rebuild |
| Fresh-worker scale-up latency | 45GB pull, versus 10GB pull plus volume mount, versus 10GB pull with weights already on the host. The number that decides which wins under burst |
| Warm-worker load time | Image layer on local disk, versus volume read, versus host cache read — all → VRAM |
| Steady-state inference latency | Expected identical across all three. If it is not, the weight source is on the hot path and that is a finding |
| GPU availability in the pinned region | The volume's hidden cost — measured as which GPU types were actually offerable. The cached endpoint has no pin; its placement is the platform's |
| Cache staging time (cached only) | Observed once, on first deploy: unbilled, but it is wall-clock the first worker waits — and it pulls the whole repo, duplicate `flux1-dev.safetensors` included, so it stages ~56GB not ~33GB |
| Storage cost per month | ~33GB at the published per-GB rate for the volume; the cache is platform-managed and free; verify before publishing |

The hypothesis: baked wins on availability and steady state, volume wins on iteration, cached wins scale-up outright. Reported as *when each wins*, not as a single verdict — the answer depends on traffic shape, and saying so is more useful than picking a side.

### 8. Claims made in the specs

Two assertions are load-bearing elsewhere and are asserted rather than measured. Both are cheap to settle in the same run.

| Claim | Made in | Measurement |
|---|---|---|
| Real CFG costs ~2× | [01](01-worker.md#why-there-is-no-negative-prompt) — the reason no negative prompt is exposed | Same prompt and seed at `true_cfg_scale` 1.0 and 3.5. If it is not ~2×, the justification needs rewriting |
| FlashBoot resume beats cold start substantially | [01](01-worker.md) — the reason for the module-level warm-up design | Already falls out of §1; called out here so it is reported as a verdict on the design, not just a number |

A spec that justifies a design decision with an unmeasured number is a spec with a soft spot. These are the two, and this is where they get closed.

### 9. Quality versus steps

A fixed-seed grid across step counts, committed as images.

Not a performance measurement, but it is what makes the performance measurement *actionable*: if 20 steps is visually indistinguishable from 28, the 28-step latency is the wrong default and the benchmark has changed a decision rather than just described one.

## Harness

`client/benchmark.py`, driven by a config file, emitting JSONL raw results plus a rendered markdown table.

- Runs against a **fake client** during development, so it is written and tested in Phase 2a with no credits and no GPU.
- Raw JSONL is committed alongside the summary. A summary table without its underlying data cannot be re-analysed or checked.
- Idempotent and resumable — a run that dies partway does not discard completed configurations.
- Every run stamps hardware, image tag, and timestamp automatically. Provenance that depends on remembering to write it down goes missing.

## Reporting

`BENCHMARKS.md` structure:

1. **Summary** — headline numbers, the GPU recommendation, and the weight-delivery recommendation, with reasoning
2. **Methodology** — the rules above, so the numbers are checkable
3. **Cold start** — decomposed table, cold vs FlashBoot
4. **Latency** — the sweeps with p50/p95, plus end-to-end as the caller sees it
5. **Cost** — per GPU per configuration, execution-only, with idle, and storage
6. **Ceilings** — VRAM and the OOM boundary
7. **Weight delivery three-way** — baked vs volume vs cached, with its precondition stated
8. **Claims verified** — the 2× CFG cost and the FlashBoot benefit
9. **Quality vs steps** — the image grid
10. **Threats to validity** — below

Values that feed configuration — `AVG_JOB_SECONDS`, the recommended GPU, the recommended variant — are listed together at the end of the summary, so the report has an actionable output and not only a descriptive one.

## Threats to validity

Stated rather than hidden. A benchmark that does not know its own weaknesses is not trustworthy.

- **Single-region, single-session.** GPU allocation varies between hosts; another run may draw different silicon.
- **Small N.** 10 runs bounds variance loosely, not tightly. Confidence intervals are not claimed.
- **Rates change.** Cost figures are correct as of the stated date and no longer.
- **Queue behaviour depends on fleet state.** Measured under low load; contention was not simulated.
- **Quality is subjective.** The step grid supports judgement; it does not replace it.

## Dependency

Everything here runs in Phase 2b and is blocked on credits. The harness, the config, and the report skeleton are written in Phase 2a, so 2b is a single execution pass with no decisions left in it.
