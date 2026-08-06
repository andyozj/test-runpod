# Gateway (spike)

An API layer in front of the RunPod serverless endpoint. **A spike, beyond the brief**: the graded deliverable is the worker, which is callable without any of this.

## Why it exists

One API in front of the endpoint, so clients speak this vocabulary instead of RunPod's and never hold RunPod's key.

**It adds:**

- **Caller authentication.** Per-caller `key_id:secret` pairs from `GATEWAY_API_KEYS`, matched constant-time against SHA-256 digests (`settings.resolve_key`, enforced in `api/app.py:authenticate`). RunPod's own account-scoped key — which can delete resources — stays server-side. Every job is attributed to a caller, with prompt, result and timings.
- **A per-key active-job cap.** `MAX_ACTIVE_JOBS_PER_KEY` non-terminal jobs (`core/service._check_active_job_cap`), so one runaway credential cannot occupy the queue. Not a request-rate limit, not a spend cap.
- **Idempotent submission.** An `Idempotency-Key` replays the original job instead of generating and billing twice. `core/service.submit` resolves a replay on row *identity* before shedding, and releases the key when a job is shed, so a 429'd retry can still become a real attempt.
- **A job store.** `adapters/memory.InMemoryJobRepository` behind a protocol: one `asyncio.Lock` makes the idempotent insert atomic; terminal jobs are evicted after an hour.
- **Reconciliation.** Nothing tells the gateway when a job finishes, so `workers/reconciler` polls and `core/service.reconcile` resolves each in-flight job — claims are leased (`claim_unresolved(lease_s=...)`) and released per job; a job with no upstream id is adopted and resubmitted only after `submit_grace_s`.
- **Queue-pressure shedding.** Estimated wait over `MAX_QUEUE_WAIT_S` returns 429 with a `Retry-After` jittered 0.8–1.2×, so a shed burst does not retry in lockstep. Fails open when the reading is missing or stale.
- **Upstream resilience.** Bounded retries with jittered backoff plus a circuit breaker with an exclusive half-open probe (`adapters/runpod_client`) absorb an upstream blip instead of surfacing it.

**Not its job:**

- **Image generation and the authoritative content verdict.** The gateway runs the shared prompt blocklist as an early reject (`adapters/guardrails`, same `contracts/blocklist.json` and `normalisation.json` the worker reads, both tiers pinned by `contracts/guardrail-corpus.json`). The worker re-checks the prompt and owns the image-stage verdict outright.
- **Persistence beyond process lifetime.** Jobs are lost on restart; Postgres is specified, unimplemented.
- **Multi-instance operation.** The store is per-process, so idempotency and the per-key cap hold within one instance only. The invariants a database port inherits — atomic idempotent insert, claim leasing, race-safe cap counting — are in [`docs/DESIGN.md`](../docs/DESIGN.md#7-ports-and-adapters-in-memory-persistence).

## Run it

From the repo root (`compose.yaml` and `.env.example` live there):

```bash
cp .env.example .env      # RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID are required;
                          # compose refuses to start without either
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

`GATEWAY_API_KEYS` is unset in `.env.example`, so compose falls back to
`demo:local-development-key` — the secret in the curls above. Set your own
before exposing the port; the application itself never invents a credential.

## Endpoints

Every route the app serves. Interactive docs, unauthenticated, at
`localhost:8000/docs` (Swagger UI) and `localhost:8000/redoc`; the OpenAPI
document is at `/openapi.json`.

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/v1/jobs` | POST | yes | Submit. `202` with a job id, or `200` replaying an idempotent duplicate |
| `/v1/jobs/{id}` | GET | yes | Status, live progress, result or error. Scoped to the caller's own jobs |
| `/v1/jobs/{id}/cancel` | POST | yes | Stop a queued or running job, upstream included. Scoped likewise |
| `/health` | GET | **no** | Liveness: status and version, no I/O. Probes cannot hold credentials |
| `/health/detailed` | GET | yes | Upstream queue counts plus reconciler liveness. Authenticated because it reports topology |

Another caller's job id answers `404`, not `403`: confirming the id exists is
itself a leak.

Request headers: `Authorization: Bearer <key>`, `Idempotency-Key` for safe
retries, `X-Correlation-ID` to supply your own trace id.

Response headers: `X-Correlation-ID` on every response, `Idempotency-Replayed:
true` on a replay, `Retry-After` on `429` and `503`. Starlette lowercases
response headers on the wire — compare case-insensitively.

Every error, including FastAPI's own validation failures, comes back in one
envelope: `{"error": {"code", "message", "suggestion", "correlation_id"}}`.
Codes come from [`contracts/error-codes.json`](../contracts/error-codes.json).

## Structure

```
src/gateway/
  core/           the rules: models, protocols, JobService. Imports nothing
                  outward, enforced by import-linter
  adapters/       memory.py (job repository), runpod_client.py (HTTP client,
                  retry, circuit breaker), guardrails.py (blocklist)
  api/            app.py (routes, auth, health), schemas.py (wire types)
  workers/        reconciler.py: polls upstream, resolves outstanding jobs
  contracts.py    locate the repo-root contracts/ directory
  settings.py     the only module that reads the environment
  main.py         composition root: the only module naming both a protocol and
                  an implementation
```

Everything is testable with no database and no endpoint, because every dependency is a protocol with a hand-written fake.

## Load shedding

Two paths return `429 QUEUE_SATURATED`, both with `Retry-After`:

- estimated queue wait above `MAX_QUEUE_WAIT_S` (default 120s), derived from
  the upstream queue reading and `AVG_JOB_S`. `Retry-After` is that estimate
  jittered 0.8-1.2× and floored at 1s, so a shed burst does not retry in
  lockstep
- that key already holding `MAX_ACTIVE_JOBS_PER_KEY` (default 10) non-terminal
  jobs. `Retry-After` is `AVG_JOB_S + 1`

The per-key cap bounds one caller's share of the queue. It is not a
request-rate limit and not a spend cap.

`503 UPSTREAM_UNAVAILABLE` with `Retry-After: 5` means the circuit breaker is
open or RunPod is unreachable. Every environment variable and its default is
listed in [`.env.example`](../.env.example).

## Not implemented

Persistence is in-memory. Postgres and Alembic are specified but unimplemented; `InMemoryJobRepository` implements the same protocol, so swapping it is one binding in `main.py`. Jobs do not survive a restart, and terminal jobs are evicted after an hour: results carry multi-MB images, so unbounded retention is an OOM, and RunPod's own copy expires after 30 minutes anyway.

The ranked limits are in [`docs/DESIGN.md`](../docs/DESIGN.md#known-limits). The two that matter most: no per-caller request-rate limit, and no budget cap. Authentication answers *who*; nothing yet answers *how much you're spending*.
