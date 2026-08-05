# 05 — Observability

> **Diagram:** [correlation ID propagation](https://excalidraw.com/#json=Uig_Bds2I3M_Cq9NVyszm,_Y0E4L_-BuLpMwEW6chxlA) — opens in Excalidraw, editable

## The serverless constraint

RunPod serverless supports no sidecars. There is no log shipper, no metrics agent, no way to run anything alongside the handler. **Handler stdout is the entire in-container observability surface.** Anything not emitted there is only observable from outside via the RunPod API.

That splits the picture in two, and the runbook has to say which side each signal lives on:

| Signal | Visible from |
|---|---|
| Generation latency, seed, resolution, errors, OOM | Worker stdout |
| Pipeline load duration, cold-start success | Worker stdout |
| Queue depth, worker counts, endpoint reachability | RunPod API only — the container cannot see them |
| Cold-start *failures* | Neither. The container never reaches the handler, so nothing is emitted. Detectable only by synthetic probing |

That last row is the uncomfortable one and belongs in the runbook explicitly.

### Endpoint metrics from outside

`GET /v2/{endpoint_id}/health` returns what the container cannot see:

```json
{"jobs": {"completed": 1, "failed": 5, "inProgress": 0, "inQueue": 2, "retried": 0},
 "workers": {"idle": 0, "running": 0}}
```

The reconciler already polls on a 2s tick ([02](02-gateway-core.md#queue-pressure)), so it fetches this on the same tick and emits one `endpoint_health` log line per refresh. That produces a real time series of queue depth, in-flight jobs, and worker counts at no extra cost — a log-based substitute for the metrics export listed in [08](08-production-readiness.md).

The same cached value drives the `429` and populates `/health/detailed`. One upstream call, three consumers.

Cold-start failures remain invisible even here. `workers.running` rising without `jobs.completed` following is the closest available signal, and it is inference rather than observation.

## Logging

`structlog`, JSON renderer, flat key-value pairs, `snake_case` event names. Every entry carries `timestamp`, `level`, `event`, `correlation_id`.

Worker events: `pipeline_loaded` (duration), `generation_started`, `generation_completed` (duration, steps, resolution, seed), `guardrail_blocked`, `oom_detected`.

Gateway events: `request_completed` (method, path, status, duration), `job_submitted`, `job_reconciled`, `endpoint_health` (queue depth, worker counts), `queue_rejected`, `upstream_retry`, `breaker_opened`, `breaker_closed`, `auth_failed`.

**Per-step progress is not logged.** It is emitted via `progress_update` and stored on the job ([01](01-worker.md#progress-reporting)), and logging it would add 28 lines per image describing something already visible in the job record. Only the start and the end of a generation are events.

**`runpod_job_id` is logged alongside `correlation_id`** on every job-related entry. Our correlation ID joins our two tiers; it means nothing in RunPod's own dashboard, where jobs are keyed by their id. Logging both is what makes our logs joinable to theirs when the question is "what did RunPod think was happening".

Prompts are logged as an 80-character preview plus full length. A hash makes bad-image reports unanswerable; the full text is unbounded user content in a log store. The preview is the compromise, and it is deliberate rather than incidental.

Never logged: API keys, `HF_TOKEN`, `RUNPOD_API_KEY`, connection strings. Enforced by ruff `S` per [`STANDARDS.md`](../../STANDARDS.md) §11.

## Correlation

One ID, generated at the gateway from `X-Correlation-ID` or fresh, then:

1. bound into the gateway's log context
2. persisted on the `jobs` row
3. passed into the RunPod job input
4. bound into the worker's log context
5. returned on the response and on every error envelope

A user reporting a failure quotes one ID that maps to logs on both tiers with no search.

This is a hand-rolled substitute for distributed tracing. It gives correlation but no spans and no latency breakdown — OpenTelemetry is the real answer and is listed in [08](08-production-readiness.md).

## Health

Two endpoints, deliberately different in cost.

**`GET /health`** — liveness. **Unauthenticated.** Returns 200 if the process is up. No dependency checks, no I/O.

```json
{"status": "ok", "version": "1.4.0+a3f21c8"}
```

It is unauthenticated because the thing that polls it — an orchestrator probe, a load balancer, `docker compose` healthcheck — generally cannot hold a credential, and requiring one turns liveness checking into a credential-distribution problem. It reveals nothing: process up, and a version string.

**`GET /health/detailed`** — **authenticated**, because it reports internal topology and failure detail.

```json
{
  "status": "degraded",
  "version": "1.4.0+a3f21c8",
  "checks": {
    "database": {"status": "ok", "latency_ms": 3},
    "runpod": {"status": "degraded", "detail": "circuit breaker half-open",
               "in_queue": 2, "in_progress": 1, "workers_running": 1, "workers_idle": 0},
    "reconciler": {"status": "ok", "last_run_s_ago": 4}
  }
}
```

The `runpod` block is the cached endpoint health the reconciler already fetched — no extra upstream call to render it.

Rules that stop it becoming a liability:

- **Never gates traffic.** A dependency check wired into liveness means a slow database restarts a healthy process.
- **Results cached with a short TTL.** Otherwise it becomes an amplification vector — one request to the gateway causing several to Postgres and RunPod.
- **`degraded` is distinct from `unhealthy`.** The circuit breaker being half-open is worth showing and is not an outage.

`reconciler.last_run_s_ago` catches the failure that is otherwise invisible: a background task that has silently died while the process stays perfectly alive.

### Version

`version` is the package version plus the short git SHA, injected at build time — `1.4.0+a3f21c8`.

The SHA is the part that matters. A semantic version alone cannot distinguish two builds of the same release, so a report of "it broke on 1.4.0" cannot be traced to what actually ran. For the worker it must match the image tag, or a benchmark cannot be attributed to a build.

## What is not here

Metrics export, tracing, alerting, and SLOs — all in [08](08-production-readiness.md) with cost estimates. Logs plus health endpoints are the floor, not the target.
