# 05 — Observability

> **Diagram:** correlation ID propagation — *pending Excalidraw*

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

## Logging

`structlog`, JSON renderer, flat key-value pairs, `snake_case` event names. Every entry carries `timestamp`, `level`, `event`, `correlation_id`.

Worker events: `pipeline_loaded` (duration), `generation_started`, `generation_completed` (duration, steps, resolution, seed), `guardrail_blocked`, `oom_detected`.

Gateway events: `request_completed` (method, path, status, duration), `job_submitted`, `job_reconciled`, `upstream_retry`, `breaker_opened`, `breaker_closed`, `auth_failed`.

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

**`GET /health`** — liveness. Returns 200 if the process is up. No dependency checks, no I/O. This is what an orchestrator polls every few seconds, and it must stay cheap enough that polling it is free.

```json
{"status": "ok", "version": "1.2.0"}
```

**`GET /health/detailed`** — dependency status, for humans and dashboards.

```json
{
  "status": "degraded",
  "version": "1.2.0",
  "checks": {
    "database": {"status": "ok", "latency_ms": 3},
    "runpod": {"status": "degraded", "detail": "circuit breaker half-open"},
    "reconciler": {"status": "ok", "last_run_s_ago": 4}
  }
}
```

Rules that stop it becoming a liability:

- **Never gates traffic.** A dependency check wired into liveness means a slow database restarts a healthy process.
- **Results cached with a short TTL.** Otherwise it becomes an amplification vector — one request to the gateway causing several to Postgres and RunPod.
- **Authenticated.** It reports internal topology and failure detail; that is not public information.
- **`degraded` is distinct from `unhealthy`.** The circuit breaker being half-open is worth showing and is not an outage.

`reconciler.last_run_s_ago` catches the failure that is otherwise invisible: a background task that has silently died while the process stays perfectly alive.

## What is not here

Metrics export, tracing, alerting, and SLOs — all in [08](08-production-readiness.md) with cost estimates. Logs plus health endpoints are the floor, not the target.
