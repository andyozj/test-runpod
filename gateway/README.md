# Gateway

An API layer in front of the RunPod serverless endpoint. **Beyond the brief** — the graded deliverable is the worker, which is callable without any of this.

## Why it exists

Clients could call RunPod directly. What that costs:

| Direct | With the gateway |
|---|---|
| Every client holds your RunPod API key — which is account-scoped and can also delete resources | Clients hold their own key; RunPod's stays server-side |
| Clients speak RunPod's job vocabulary | Clients speak one API; RunPod is swappable |
| No record of anything | Every job attributed to a caller, with prompt, result and timings |
| A retried request generates and bills twice | Idempotency keys make retries free |
| An upstream blip is a user-visible failure | Retry and circuit breaker absorb it |
| One transport, forever | SSE, webhooks and MCP are additions, not rewrites |

## Run it

From the repo root — `compose.yaml` and `.env.example` live there:

```bash
cp .env.example .env      # RUNPOD_API_KEY, RUNPOD_ENDPOINT_ID
docker compose up

curl -X POST localhost:8000/v1/jobs \
  -H "Authorization: Bearer local-development-key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a red fox in falling snow"}'
# -> 202 {"job_id": "...", "status": "QUEUED"}

curl localhost:8000/v1/jobs/<job_id> -H "Authorization: Bearer local-development-key"

# once COMPLETED, the result carries the image inline
curl -s localhost:8000/v1/jobs/<job_id> -H "Authorization: Bearer local-development-key" \
  | jq -r '.result.image_base64' | base64 -d > fox.png
```

Interactive docs at `localhost:8000/docs`.

## Endpoints

| Route | Auth | Purpose |
|---|---|---|
| `POST /v1/jobs` | yes | Submit. `202` with a job id, or `200` replaying an idempotent duplicate |
| `GET /v1/jobs/{id}` | yes | Status, live progress, result or error. Scoped to the caller's own jobs |
| `POST /v1/jobs/{id}/cancel` | yes | Stop a queued or running job, upstream included. Scoped likewise |
| `GET /health` | **no** | Liveness. Probes cannot hold credentials |
| `GET /health/detailed` | yes | Upstream queue counts plus reconciler liveness |

Headers: `Idempotency-Key` for safe retries, `X-Correlation-ID` to supply your own trace id.

## Structure

```
core/       the rules. Imports nothing outward — enforced by import-linter
adapters/   memory repository, RunPod HTTP client, blocklist
api/        FastAPI routes, auth, wire schemas
workers/    the reconciler
main.py     composition root: the only module naming both a protocol and an implementation
```

Everything is testable with no database and no endpoint, because every dependency is a protocol with a hand-written fake. That is why this exists at all while credits are pending.

## Not implemented

Persistence is in-memory. Postgres and Alembic are specified in [`docs/specs/02-gateway-core.md`](../docs/specs/02-gateway-core.md); `InMemoryJobRepository` implements the same protocol, so swapping it is one binding in `main.py`. Jobs do not survive a restart, and terminal jobs are evicted after an hour — results carry multi-MB images, so unbounded retention is an OOM, and RunPod's own copy expires after 30 minutes anyway.

The full gap list, ranked, is in [`docs/specs/08-production-readiness.md`](../docs/specs/08-production-readiness.md). The two that matter most: no per-caller rate limit, and no budget cap. Authentication answers *who*; nothing answers *how much*. The default `GATEWAY_API_KEYS` is a documented dev credential — the composition root logs a warning when it is in use; set your own before exposing the port.
