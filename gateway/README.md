# Gateway (spike)

An API layer in front of the RunPod serverless endpoint. **A spike, beyond the brief**: the graded deliverable is the worker, which is callable without any of this.

## Why it exists

One API in front of the endpoint, so clients speak this vocabulary instead of RunPod's and never hold RunPod's key.

**It adds:**

- **Caller authentication.** Per-caller `key_id:secret` pairs from `GATEWAY_API_KEYS`, matched constant-time against SHA-256 digests (`settings.resolve_key`, enforced in `api/app.py:authenticate`). RunPod's own account-scoped key — which can delete resources — stays server-side. Every job is attributed to a caller, with prompt, result and timings.
- **A per-key active-job cap.** `MAX_ACTIVE_JOBS_PER_KEY` non-terminal jobs (`core/service._check_active_job_cap`), so one runaway credential cannot occupy the queue. Bounds how much a key holds, not how often it submits — that is the rate limit below. Not a spend cap.
- **A per-key request rate limit.** A token bucket per key (`adapters/ratelimit`): `GATEWAY_RATE_LIMIT_BURST` (default 10) submissions at once, refilled at `GATEWAY_RATE_LIMIT_RPM` (default 30) per minute. One global policy, per-key buckets. POST `/v1/jobs` only — polling GETs are the designed usage pattern and are never limited, and an idempotent replay is answered before the bucket, so retrying an accepted job costs no token. Buckets are in-process, one set per instance — the same single-instance honesty as the queue-health cache.
- **Idempotent submission.** An `Idempotency-Key` replays the original job instead of generating and billing twice. `core/service.submit` resolves a replay on row *identity* before shedding, and releases the key when a job is shed, so a 429'd retry can still become a real attempt.
- **A job store.** Two adapters behind one `JobRepository` protocol, selected by `DATABASE_URL` in `main.py`. Set: `adapters/postgres.PostgresJobRepository` (asyncpg, Alembic-migrated schema) — the idempotent insert is a partial unique index plus `ON CONFLICT`, claims are `FOR UPDATE SKIP LOCKED` plus a lease column, and terminal jobs are kept: they are the gallery. Unset: `adapters/memory.InMemoryJobRepository` — one `asyncio.Lock` makes the idempotent insert atomic; terminal jobs are evicted after an hour.
- **An image store.** In Postgres mode, images leave the job row: on completion the base64 is decoded to `{GATEWAY_IMAGE_DIR}/{job_id}.{png|jpeg}` and the row keeps the path, dimensions, seed, timings and a ~25-byte [ThumbHash](https://evanw.github.io/thumbhash/) placeholder (`adapters/images`, `adapters/thumbhash` — a verified port of the reference encoder). The directory is capped by `GATEWAY_IMAGE_STORE_MAX_BYTES`, oldest evicted first; an evicted job keeps its metadata and answers `410` on the image route.
- **Reconciliation.** Nothing tells the gateway when a job finishes, so `workers/reconciler` polls and `core/service.reconcile` resolves each in-flight job — claims are leased (`claim_unresolved(lease_s=...)`) and released per job; a job with no upstream id is adopted and resubmitted only after `submit_grace_s`.
- **Queue-pressure shedding.** Estimated wait over `MAX_QUEUE_WAIT_S` returns 429 with a `Retry-After` jittered 0.8–1.2×, so a shed burst does not retry in lockstep. Fails open when the reading is missing or stale.
- **Upstream resilience.** Bounded retries with jittered backoff plus a circuit breaker with an exclusive half-open probe (`adapters/runpod_client`) absorb an upstream blip instead of surfacing it.

**Not its job:**

- **Image generation and the authoritative content verdict.** The gateway runs the shared prompt blocklist as an early reject (`adapters/guardrails`, same `contracts/blocklist.json` and `normalisation.json` the worker reads, both tiers pinned by `contracts/guardrail-corpus.json`). The worker re-checks the prompt and owns the image-stage verdict outright.
- **Multi-instance operation.** Postgres makes the store shared, but the queue-health cache and reconciler are still per-process; running several instances is untested. The invariants both adapters prove — atomic idempotent insert, claim leasing, race-safe cap counting — are in [`docs/DESIGN.md`](../docs/DESIGN.md#7-ports-and-adapters-memory-first-postgres-behind-the-same-port) and pinned by the contract suite in `tests/contract/`.

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

# once COMPLETED, the result carries image_url (the image lives on disk)
curl -s localhost:8000/v1/jobs/<job_id>/image \
  -H "Authorization: Bearer local-development-key" > fox.png

# the gallery: newest first, metadata plus thumbhash, no image bytes
curl -s "localhost:8000/v1/jobs?limit=20" \
  -H "Authorization: Bearer local-development-key" | jq '.jobs[0]'
```

`GATEWAY_API_KEYS` is unset in `.env.example`, so compose falls back to
`demo:local-development-key` — the secret in the curls above. Set your own
before exposing the port; the application itself never invents a credential.

Compose runs Postgres by default (`pgdata` and `images` volumes, so jobs and
images survive a restart). With `DATABASE_URL` empty the gateway runs in
memory mode instead: nothing persists, and a completed result carries
`image_base64` inline rather than `image_url` — the image route still serves
the bytes in both modes.

## Endpoints

Every route the app serves. Interactive docs, unauthenticated, at
`localhost:8000/docs` (Swagger UI) and `localhost:8000/redoc`; the OpenAPI
document is at `/openapi.json`.

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/v1/jobs` | POST | yes | Submit. `202` with a job id, or `200` replaying an idempotent duplicate |
| `/v1/jobs?limit=N` | GET | yes | The caller's jobs, newest first: metadata (`num_inference_steps`, `guidance_scale` and `output_format` included) plus thumbhash and `image_url`, never image bytes. `limit` 1-200, default 50 |
| `/v1/jobs/{id}` | GET | yes | Status, `request` (the submitted parameters, so the job is reproducible from the response), live progress, result or error. Scoped to the caller's own jobs. Progress may carry a worker latent-preview frame (`preview_b64` + `preview_format`), passed through while running and cleared on any terminal state. A stored image appears as `result.image_url` + `result.thumbhash`; memory mode keeps `result.image_base64` inline |
| `/v1/jobs/{id}/image` | GET | yes | The image bytes with their content type. Scoped likewise; `410` once the file has been evicted from the byte-capped store |
| `/v1/jobs/{id}/cancel` | POST | yes | Stop a queued or running job, upstream included. Scoped likewise |
| `/v1/metrics` | GET | yes | The Operate dashboard's aggregates: per-key job counts, latency percentiles, hourly throughput, estimated cost, plus the shared upstream readings. See [Metrics](#metrics) |
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

## Metrics

`GET /v1/metrics` feeds the Operate dashboard from the job ledger. Everything
except `upstream` is **per-key** — a caller sees only its own jobs' aggregates,
the same scoping as the job routes; `upstream` is shared topology, the reading
`/health/detailed` already reports and equally behind auth.

- `jobs`: `by_status` (all-time in the store, every status present), `last_hour`
  (created in the trailing hour), `active_now` (non-terminal). `by_status` is
  all-time while `GET /v1/jobs` is capped by its `limit`, so the two
  legitimately disagree: the counts are not a total of the rows on screen
- `latency`: nearest-rank p50/p95/max of `inference_seconds` and of wall time
  (`completed_at - created_at`) over the newest `GATEWAY_METRICS_WINDOW`
  (default 100) completed jobs
- `throughput`: completions per UTC hour over the last 24h, 24 dense buckets,
  zeros included
- `cost`: the window's execution seconds × `GATEWAY_GPU_RATE_USD_HR` (default
  1.75, BENCHMARKS.md's measured 48GB-tier rate) / 3600, total and per job.
  Named `estimated_cost_usd` because it is an estimate, not billing data.
  `exec_seconds_in_window` is the input it was computed from, so
  `exec_seconds_in_window * gpu_rate_usd_hr / 3600 == estimated_cost_usd`
  holds and the figure can be checked rather than trusted
- `upstream`: the cached queue reading and reconciler liveness, each with its
  age (`age_s` / `last_tick_s`), a `stale` verdict and the `stale_after_s`
  threshold behind it. The judgement is the gateway's, not the caller's: the
  queue reuses `HEALTH_MAX_AGE_S` (default 30s), the same cutoff load shedding
  uses; the reconciler uses three `RECONCILE_IDLE_INTERVAL_S` (default 10s), so
  slowing the loop down moves the threshold with it. `stale` is true when the
  reading is missing (`status: "unknown"`) as well as when it is too old —
  in both cases the numbers are not current
- `window`, `completed_in_window`, `window_started_at`, `window_ended_at`,
  `generated_at`: what the window was, how many jobs it actually held, the
  oldest and newest `completed_at` it covers (both null when empty), and when
  the snapshot was assembled. The span turns a window total into a rate: the
  window is "newest N completions", not a fixed period

The Postgres adapter answers all of it with SQL aggregates (`GROUP BY status`,
`date_trunc('hour', completed_at, 'UTC')`, `inference_seconds` extracted from
the result jsonb) — no result rows are loaded. In memory mode the totals only
cover what retention keeps (terminal jobs evicted after an hour).

## Structure

```
src/gateway/
  core/           the rules: models, protocols, JobService. Imports nothing
                  outward, enforced by import-linter
  adapters/       memory.py + postgres.py (job repositories), images.py
                  (filesystem image store), thumbhash.py (placeholder
                  encoder), schema.py (startup migrations), runpod_client.py
                  (HTTP client, retry, circuit breaker), guardrails.py
                  (blocklist)
  api/            app.py (routes, auth, health), schemas.py (wire types)
  workers/        reconciler.py: polls upstream, resolves outstanding jobs
  contracts.py    locate the repo-root contracts/ directory
  settings.py     the only module that reads the environment
  main.py         composition root: the only module naming both a protocol and
                  an implementation
migrations/       Alembic migrations (alembic.ini beside them); applied
                  automatically at startup when DATABASE_URL is set
```

Everything is testable with no database and no endpoint, because every dependency is a protocol with a hand-written fake.

## Persistence

`DATABASE_URL` is the switch, bound once in `main.py`:

- **Set** (compose default): `PostgresJobRepository` + `FilesystemImageStore`.
  Alembic migrations run inside the app lifespan before the pool opens, so a
  fresh database self-provisions. The memory adapter's invariants hold in SQL:
  the idempotent insert is atomic (partial unique index + `ON CONFLICT`),
  `claim_unresolved` leases via `FOR UPDATE SKIP LOCKED` + `lease_expires_at`,
  and the active-job count can only over-count under concurrency (the
  triggering row is inserted before counting), never admit past the cap.
- **Unset**: `InMemoryJobRepository`, no image store, results inline — the
  pre-Postgres behaviour, still what the test fakes exercise.

The repository contract lives in `tests/contract/test_job_repository.py` and
runs against both adapters: memory always, Postgres when `DATABASE_URL_TEST`
names a disposable database (skips itself otherwise). CI can point it at a
service container; locally:

```bash
docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=test postgres:16
cd gateway && DATABASE_URL_TEST=postgresql://postgres:test@localhost:5433/postgres \
  uv run pytest -m postgres -q --no-cov
```

Timestamps, leases and grace windows all come from the injected `Clock`, so
the same frozen-clock tests drive both adapters.

## Load shedding

Three paths return `429 QUEUE_SATURATED`, all with `Retry-After`:

- estimated queue wait above `MAX_QUEUE_WAIT_S` (default 120s), derived from
  the upstream queue reading and `AVG_JOB_S`. `Retry-After` is that estimate
  jittered 0.8-1.2× and floored at 1s, so a shed burst does not retry in
  lockstep
- that key already holding `MAX_ACTIVE_JOBS_PER_KEY` (default 10) non-terminal
  jobs. `Retry-After` is `AVG_JOB_S + 1`
- that key's token bucket is empty: `GATEWAY_RATE_LIMIT_BURST` (default 10)
  submissions at once, refilled at `GATEWAY_RATE_LIMIT_RPM` (default 30) per
  minute. `Retry-After` is the seconds until a token exists, ceiled and
  floored at 1s. The contract has no `RATE_LIMITED` code, so the envelope
  reuses `QUEUE_SATURATED`

All three release the request's `Idempotency-Key`, so a shed retry can still
become a real attempt. The cap bounds one caller's share of the queue; the
bucket bounds its request rate. Neither is a spend cap.

`503 UPSTREAM_UNAVAILABLE` with `Retry-After: 5` means the circuit breaker is
open or RunPod is unreachable. Every environment variable and its default is
listed in [`.env.example`](../.env.example).

## Not implemented

No budget cap — authentication answers *who*, the rate limit *how often*; nothing yet answers *how much you're spending*. The ranked limits are in [`docs/DESIGN.md`](../docs/DESIGN.md#known-limits).

In memory mode specifically: jobs do not survive a restart, and terminal jobs are evicted after an hour — results carry multi-MB images inline there, so unbounded retention is an OOM, and RunPod's own copy expires after 30 minutes anyway.
