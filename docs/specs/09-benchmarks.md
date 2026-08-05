# 09 — Benchmarks

`BENCHMARKS.md` is the primary deliverable alongside the working endpoint. A generated image proves the task was completed. A rigorous benchmark proves the deployment was *understood* — where the time goes, what it costs, where it breaks, and which GPU is actually right rather than assumed.

Every number in it must survive being questioned.

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
- **Aspect ratios:** 1024×1024 vs 1344×768 at equal pixel count — tests whether non-square costs extra

### 3. GPU comparison

The measurement that turns a guess into a recommendation.

| GPU | VRAM | Serverless rate |
|---|---|---|
| L40S | 48GB | $1.75/hr |
| A100 80GB | 80GB | $2.72/hr |
| RTX 4090 | 24GB | $1.10/hr |

Rates from the RunPod pricing page, 2026-08-05. Re-verify before publishing.

The 4090 run doubles as the VRAM-boundary test — bf16 FLUX needs ~24-26GB, so it is expected to OOM at higher resolutions. **The resolution at which it fails is a result, not a failure of the benchmark**, and it is the empirical basis for the GPU recommendation in [01](01-worker.md).

### 4. Cost per image

Derived, not quoted:

```
cost = execution_seconds × (hourly_rate / 3600)
```

Reported per GPU per configuration, plus the honest version that includes idle-timeout billing between requests at a stated request rate. Execution-only cost understates real spend for bursty traffic, and bursty is the normal case.

Cross-check the derived figure against RunPod's reported spend for the benchmark run. A model that disagrees with the invoice is wrong.

### 5. Throughput and concurrency

- Sequential requests against one warm worker — steady-state ceiling
- N concurrent requests against max_workers=3 — measures queue wait separately from execution time

Queue wait is the number a user feels and the one a latency figure omits.

### 6. Resource ceilings

- Peak VRAM per configuration via `torch.cuda.max_memory_allocated()`
- Maximum resolution before OOM, per GPU
- Response payload size by format and resolution — PNG vs JPEG vs WebP

The payload measurement resolves the open question from [01](01-worker.md): RunPod does not document a response ceiling, so it gets probed at 1536² rather than assumed.

### 7. Quality versus steps

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

1. **Summary** — headline numbers and the GPU recommendation, with the reasoning
2. **Methodology** — the rules above, so the numbers are checkable
3. **Cold start** — decomposed table, cold vs FlashBoot
4. **Latency** — the sweeps, with p50/p95
5. **Cost** — per GPU per configuration, execution-only and with idle
6. **Ceilings** — VRAM, OOM boundary, payload sizes
7. **Quality vs steps** — the image grid
8. **Threats to validity** — below

## Threats to validity

Stated rather than hidden. A benchmark that does not know its own weaknesses is not trustworthy.

- **Single-region, single-session.** GPU allocation varies between hosts; another run may draw different silicon.
- **Small N.** 10 runs bounds variance loosely, not tightly. Confidence intervals are not claimed.
- **Rates change.** Cost figures are correct as of the stated date and no longer.
- **Queue behaviour depends on fleet state.** Measured under low load; contention was not simulated.
- **Quality is subjective.** The step grid supports judgement; it does not replace it.

## Dependency

Everything here runs in Phase 2b and is blocked on credits. The harness, the config, and the report skeleton are written in Phase 2a, so 2b is a single execution pass with no decisions left in it.
