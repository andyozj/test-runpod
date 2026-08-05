# How RunPod works — the whole platform, and how to use it well

Written 2026-08-05 from the current docs, the tutorials, and one day of
building against it. Facts from docs are stated plainly; things learned the
hard way today are marked **(learned today)**; community-sourced numbers are
marked **(community)**.

---

## 1. What RunPod is

A GPU cloud with two compute products and a storage layer:

| Product | What you get | You pay for |
|---|---|---|
| **Pods** | A whole GPU machine, yours until you stop it. SSH, Jupyter, root | Every second it exists, busy or idle |
| **Serverless** | Your container, run on demand. Scales 0 → N workers and back | Seconds a worker is actually up |
| **Storage** | Network volumes (persistent disks) + an S3-compatible API | Per GB per month |

Rule of thumb: interactive work and one-off heavy jobs → Pod. Anything that
serves requests → Serverless. This project uses Serverless only.

## 2. Serverless: the two endpoint types

| | **Queue-based** (what we use) | Load-balancing |
|---|---|---|
| Your code is | A Python function (`handler.py`) | A whole HTTP server you ship |
| Requests | Land in RunPod's queue, workers pull them | Routed straight to a port on your worker |
| You get free | Queue, retries, job status, progress store, cancellation, the whole HTTP API | Nothing — you build all of it |
| Autoscaling signal | Queue depth | Request count |
| Right for | Jobs from ~5s to hours; bursty traffic; GPU-bound work | Sub-second latency; streaming protocols (WebSocket/SSE) |
| Cold start behaviour | Job waits safely in the queue | First caller holds an open connection through the whole cold start |

For a 20-25s image-generation job, queue-based is not a close call.

## 3. Life of a queue endpoint

### The API surface (fixed — you cannot add routes)

```
POST /v2/{endpoint}/run        submit async  → job id immediately   (input ≤ 10MB)
POST /v2/{endpoint}/runsync    submit + wait → result in response   (input ≤ 20MB)
GET  /v2/{endpoint}/status/{id}   state + your handler's output, verbatim
GET  /v2/{endpoint}/stream/{id}   incremental results from generator handlers
POST /v2/{endpoint}/cancel/{id}   stop a queued or running job
POST /v2/{endpoint}/retry/{id}    requeue a failed/timed-out job, same id
POST /v2/{endpoint}/purge-queue   drop everything waiting (not running)
GET  /v2/{endpoint}/health        workers + queue stats
```

Numbers that bite if you don't know them:

| Limit | Value |
|---|---|
| Async result retention | **30 minutes** after completion, then gone |
| Sync result retention | **1 minute** |
| Job TTL (default) | 24h — if it expires **mid-run the job vanishes and `/status` returns 404** |
| Execution timeout (default) | 600s per job |
| Webhook retries | Your `webhook` URL is called on completion, retried ~2× |

### The worker lifecycle

```
scale-up trigger (queue delay > 4s, or request-count formula)
   │
1. host chosen        prefers hosts that already have your image / cached model
2. image pulled       cached on the host afterwards — first pull is the slow one
3. container starts   your CMD runs; load your model NOW, not per request
4. jobs stream in     one at a time, or more if you set concurrency
5. idle timeout       no jobs for N seconds → worker stops (billing stops)
6. FlashBoot          worker state may be kept resident, so the *next* start
                      skips model loading — free, on by default, best with
                      steady traffic
```

Endpoints idle for days get auto-scaled down (max workers → 2 after 3 days,
→ 0 after 7; any request resets it).

### The handler contract (Python SDK)

```python
import runpod

def handler(job):                      # job["input"] is your caller's payload
    ...
    return {"anything": "json"}        # becomes /status output verbatim
                                       # raise → job FAILED
                                       # return {"error": ...} → controlled failure

runpod.serverless.start({"handler": handler})
```

The SDK gives you, for free:

| Feature | How |
|---|---|
| Progress visible mid-job | `runpod.serverless.progress_update(job, {...})` → shows in `/status` while IN_PROGRESS |
| Retire a broken worker | return `"refresh_worker": True` — next job gets a fresh container (use after GPU OOM) |
| Streaming output | make the handler a generator, `yield` chunks → `/stream` |
| Concurrency per worker | `concurrency_modifier` — leave at 1 for GPU-saturating work |
| Local run, no Docker | `python handler.py` (reads `test_input.json`), or `--test_input '{...}'` |
| Local fake endpoint | `python handler.py --rp_serve_api` → FastAPI on :8000 with `/runsync` |

## 4. Getting weights to the GPU (the big architectural choice)

| Mechanism | How | Cold start | Catch |
|---|---|---|---|
| **Cached models** (recommended for anything on HuggingFace) | Set the endpoint's **Model** field (+ HF token if gated). RunPod stages the repo on hosts **before** your worker starts, **unbilled** | Seconds | Beta. Stages the *whole* repo — for FLUX that includes a 23.8GB duplicate file. One model per endpoint. Cache lands at `/runpod-volume/huggingface-cache/hub/models--{org}--{name}/snapshots/{sha}/` |
| Network volume | Create volume, populate once (from a Pod), mount at `/runpod-volume` | Volume read → VRAM | **Pins the endpoint to the volume's datacenter**, shrinking the GPU pool exactly when you scale up. Billed per GB/month |
| Baked into the image | Weights in a Docker layer | Disk read → VRAM | Huge images push slowly, pull slowly on every new host, and can hit registry per-layer limits. Largest proven baked image ≈35GB **(community)** |
| Download at start | Handler pulls from HF on boot | Minutes, **billed** | Never do this in production. Also the failure mode of a *misconfigured* volume — it looks "slow", not "broken" **(learned today)** |

## 5. Endpoint configuration that matters

| Setting | Default | What to actually do |
|---|---|---|
| GPU types | — | List **up to 3 in priority order** — automatic fallback when your first choice is scarce |
| Max workers | 3 | Your cost ceiling. Docs suggest ~20% above expected peak |
| Active (min) workers | 0 | Set 1 during demos/launches: kills cold starts entirely, bills continuously |
| Idle timeout | 5s | Tune to traffic gaps; you pay for idle warm seconds |
| Execution timeout | 600s | Set above your real worst case, not at infinity |
| Job TTL | 24h | Remember the mid-run 404 behaviour |
| FlashBoot | on | Leave on |
| CUDA version filter | broad | Match your torch wheel: a cu13x wheel needs CUDA-13-capable hosts **(learned today)** |
| Environment variables | — | All runtime config; reference the **secrets manager** (`{{ RUNPOD_SECRET_name }}`) instead of pasting values |

## 6. Storage details

- **Network volumes**: persistent, attachable to Pods and Serverless
  (mounted at `/runpod-volume`). Live in one datacenter — co-locate with your
  endpoint, or rather: accept that they force co-location.
- **S3-compatible API**: exists in five datacenters only (EUR-IS-1, EU-RO-1,
  EU-CZ-1, US-KS-2, US-CA-2). Supports PutObject/GetObject/multipart. The URLs
  are authenticated — not public links you can hand to end users.

## 7. Managing it all

| Surface | Status | Use for |
|---|---|---|
| Console (web) | — | Exploration, first deploy, log reading |
| **REST API** (`rest.runpod.io`) | Current | Automation: create/update endpoints, config-as-code |
| GraphQL API | Legacy, still works | Older scripts (`saveEndpoint`) |
| `runpodctl` | — | CLI convenience |
| GitHub integration | — | Build-from-repo on push (won't fit big images) |
| Flash SDK | Separate product | No-Docker deploys of decorated Python functions; **1.5GB package cap** — irrelevant when you ship real images |

Config-as-code pattern (what this repo does): endpoint settings live in YAML
in git, a script applies them via the API. "Why is idle timeout 60s?" is
answerable from a diff, and a deleted endpoint is reconstructible.

## 8. Billing model, condensed

- Serverless: **per second a worker is up** — running jobs *and* idle-timeout
  seconds. Queue wait costs nothing. Cached-model staging costs nothing.
- Per-GPU-type rates (e.g. L40S ≈ $1.75/hr serverless; Pod rates are lower —
  don't mix them up when estimating **(learned today, the hard way, ~12×)**).
- Your real levers: scale-to-zero, idle timeout, max-workers cap, right-sized
  GPU, and steps/resolution of the workload itself.
- Cross-check any cost model against the actual invoice for one run.

## 9. Best practices — the condensed list

**Image**
1. Build `--platform linux/amd64`. Always.
2. Small base; the torch wheel already carries CUDA libs — you don't need the
   fat `nvidia/cuda` base image **(learned today: 11.9GB → 2.92GB)**.
3. Never apt-install Python on Ubuntu 22.04 — it ships a broken 3.11.0rc1
   that crashes modern torch. Use uv-managed Python **(learned today)**.
4. Install deps from a lockfile; end the layer with an import check so a bad
   build fails at build time, not at endpoint start **(learned today)**.
5. No weights in the image unless they're small and private.
6. Immutable tags (`0.1.0-<gitsha>`), never deploy `latest` — rollback is
   redeploying the previous tag, which must still exist.

**Handler**
7. Load the model at container start (before `runpod.serverless.start`), never
   inside the handler, never at module import (imports should work GPU-free so
   your code stays testable).
8. Validate input before touching the GPU; a bad request should cost $0.
9. Catch OOM explicitly → return `refresh_worker: True`.
10. Report progress on long jobs; a 25s black box polls as "is it broken?".
11. Return structured errors with a what-to-do-next field; raising gives your
    callers a stack trace instead of an answer.
12. Fail fast and loud on missing/mismatched weights — the silent fallback is
    a 33GB download per cold start that looks like "slow".

**Endpoint**
13. Three GPU types in priority order.
14. Min workers 1 for demos; back to 0 after.
15. Verify your CUDA-wheel ↔ host-driver pairing on first deploy.
16. Secrets via the secrets manager, referenced from config — never values in
    YAML, never tokens as Docker `ARG`s (recoverable from image history).

**Calling it**
17. `/run` + `/status` polling for services; webhooks preferred for async at
    scale; `/runsync` for demos and short jobs.
18. Fetch results inside the retention window (30 min async / 1 min sync);
    anything you need longer, persist yourself.
19. Retries re-bill. If callers may retry, implement idempotency on your side
    of the fence (this is exactly what our gateway demonstrates).

**Testing ladder (cheapest first)**
20. Unit tests with the pipeline faked — no GPU, no network.
21. `python handler.py` with `test_input.json` — SDK local mode.
22. Build the real image locally and run the handler inside it — this catches
    the class of bug that only exists in the image **(learned today: 5 bugs,
    each ≈ an hour of paid GPU debugging avoided)**.
23. Only then spend money.

## 10. Pitfalls quick-reference

| Pitfall | Consequence |
|---|---|
| Result fetched late | Gone — 30 min (async) / 1 min (sync) |
| Job TTL expires mid-run | Job vanishes, `/status` 404s |
| Volume in one DC, GPUs scarce there | Your endpoint starves precisely under load |
| `latest` tag deployed | No rollback target exists |
| Token passed as Docker ARG | Recoverable by anyone who pulls the image |
| Docker Hub free tier as registry | Pull rate limits hit exactly when N workers scale up at once |
| Giant single image layer | Slow pushes that fail late; possible registry caps |
| cu13x torch wheel, old-driver host | CUDA init fails at first job |
| Model download inside the handler | Billed minutes per cold start, forever |
| Load-balancer endpoint for long jobs | Timeouts, no queue, retry = double billing |
