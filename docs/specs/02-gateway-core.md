# 02 — Gateway core

Tier 2. The domain layer and its persistence. Contains no HTTP and no RunPod vocabulary.

> **Diagram:** [job state machine](https://excalidraw.com/#json=GpbKfRPNbJuct2xSpLadJ,RQKzqy43XOY0h7AxpzIIlg) — opens in Excalidraw, editable

## Layering

```
api/  adapters/  workers/     may import core/
        │
        ▼
      core/                   imports nothing outward
```

`core/` must not import FastAPI, httpx, psycopg, or SQLAlchemy. Enforced by `import-linter` in `make check`, not by review — a stray `from fastapi import HTTPException` passes ruff, mypy, and every test while welding the domain to one transport, and the cost surfaces only when the second facade turns out to be a rewrite.

## Domain types

Everything in `core/`. No framework types, no database types.

```python
class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"

class ErrorCode(StrEnum):
    INVALID_PROMPT = "INVALID_PROMPT"
    INVALID_DIMENSIONS = "INVALID_DIMENSIONS"
    INVALID_STEPS = "INVALID_STEPS"
    PROMPT_BLOCKED = "PROMPT_BLOCKED"
    IMAGE_BLOCKED = "IMAGE_BLOCKED"
    OOM = "OOM"
    INFERENCE_FAILED = "INFERENCE_FAILED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_TIMEOUT = "JOB_TIMEOUT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    QUEUE_SATURATED = "QUEUE_SATURATED"

@dataclass(frozen=True)
class RequestContext:
    """Who is asking and under what trace, threaded through every call."""
    api_key_id: str
    correlation_id: str
    idempotency_key: str | None = None

@dataclass(frozen=True)
class JobResult:
    """What the worker produced, as stored on a completed job."""
    image_base64: str | None
    storage_key: str | None
    format: str
    seed: int
    width: int
    height: int
    num_inference_steps: int
    guidance_scale: float
    model_version: str
    inference_seconds: float

@dataclass(frozen=True)
class Job:
    """A generation request and everything known about it so far."""
    id: UUID
    status: JobStatus
    request: GenerationRequest
    context: RequestContext
    runpod_job_id: str | None
    result: JobResult | None
    error_code: ErrorCode | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
```

Frozen dataclasses rather than Pydantic models: these never parse untrusted input. Validation happens at the boundary in `api/`, and by the time a `Job` exists the data is already trusted. Immutability means a transition returns a new `Job` rather than mutating one another caller still holds.

`ErrorCode` is an enum, not a string. The error-code contract requires 100% test coverage ([`STANDARDS.md`](../../STANDARDS.md) §9), which is not achievable if any string can be written into the field.

## JobService

```python
class JobService:
    def __init__(
        self,
        repository: JobRepository,
        runpod: RunPodClient,
        guardrail: PromptGuardrail,
        clock: Clock,
    ) -> None: ...

    async def submit(self, request: GenerationRequest, ctx: RequestContext) -> Job: ...
    async def get(self, job_id: UUID) -> Job | None: ...
    async def reconcile(self, limit: int) -> int: ...
```

`Clock` is injected rather than calling `datetime.now()` — timestamp assertions in tests need a fixed clock, and a service that reaches for wall time is a service that cannot be tested deterministically.

## Protocols

```python
class JobRepository(Protocol):
    async def create(self, job: Job) -> Job: ...
    async def get(self, job_id: UUID) -> Job | None: ...
    async def get_by_idempotency_key(self, key: str) -> Job | None: ...
    async def attach_runpod_id(self, job_id: UUID, runpod_job_id: str) -> Job: ...
    async def mark_in_progress(self, job_id: UUID) -> Job: ...
    async def mark_completed(self, job_id: UUID, result: JobResult) -> Job: ...
    async def mark_failed(self, job_id: UUID, code: ErrorCode, message: str) -> Job: ...
    async def mark_blocked(self, job_id: UUID, verdict: GuardrailVerdict) -> Job: ...
    async def claim_unresolved(self, limit: int) -> list[Job]: ...

class RunPodClient(Protocol):
    async def submit(self, payload: dict[str, Any]) -> str: ...
    async def status(self, runpod_job_id: str) -> RunPodJobStatus: ...
    async def health(self) -> EndpointHealth: ...

class Clock(Protocol):
    def now(self) -> datetime: ...
```

Postgres and httpx implementations live in `adapters/`. The whole gateway is buildable and testable against fakes for both, which is why Phases 3 and 4 are not blocked on the endpoint existing.

**Named transitions, not a generic `update(**changes)`.** A catch-all writer types nothing — mypy cannot check that a completion carries a result or that a failure carries a code, and the call site does not say what it means. Each named method encodes one legal transition, so an illegal one is a type error rather than a runtime surprise.

**`claim_unresolved`, not `list_unresolved`.** It issues `SELECT ... FOR UPDATE SKIP LOCKED`. With one reconciler this is redundant; with two it is the difference between polling each job once and polling it twice, double-counting and racing on the write. The cost of specifying it now is one clause.

## Data model

`jobs`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid | pk |
| `idempotency_key` | text | nullable; unique with `api_key_id` |
| `request_hash` | text | nullable; detects a key reused with a different body |
| `runpod_job_id` | text | nullable, indexed |
| `status` | enum | domain enum, below |
| `request` | jsonb | validated parameters as submitted |
| `result` | jsonb | nullable |
| `error_code` | text | nullable |
| `error_message` | text | nullable |
| `progress` | jsonb | nullable; latest step/total/percent, overwritten each tick |
| `correlation_id` | text | indexed |
| `api_key_id` | text | which caller submitted it |
| `created_at` / `updated_at` / `completed_at` | timestamptz | `completed_at` nullable |

Migrations via Alembic. `api_key_id` exists from the first migration — retrofitting attribution onto existing rows is not possible, and without it there is no way to answer "who generated this".

### Idempotency

Enforced by the unique constraint, not a read-then-write check. Check-then-insert has exactly the race that idempotency exists to prevent: two concurrent identical requests both see no existing row and both submit, double-billing the GPU.

Insert first, catch the unique violation, return the existing job. The integration suite asserts this with genuinely concurrent inserts, not sequential ones — a sequential test passes against the broken implementation.

**The constraint is on `(api_key_id, idempotency_key)`, not the key alone.** Scoped to the key only, two callers picking the same value collide and one receives the other's image. That is a data leak, not an inconvenience.

`request_hash` is stored with the key. A replay with a matching hash returns the original job; a mismatch is `409 IDEMPOTENCY_CONFLICT` ([03](03-facades.md#idempotency)). Silently returning the first job when the body has changed hands the caller an image of something they did not ask for, with nothing to indicate it.

### Status

Domain enum: `QUEUED`, `IN_PROGRESS`, `COMPLETED`, `FAILED`, `TIMED_OUT`, `CANCELLED`, `BLOCKED`.

`BLOCKED` is distinct from `FAILED`: a guardrail rejection is not an error, it is a decision, and conflating them makes the block rate unmeasurable.

RunPod's vocabulary (`IN_QUEUE`, `IN_PROGRESS`, `COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT`) is mapped in the adapter. It does not leak into `core/`.

`CANCELLED` has no producer on our side — we expose no cancel route. It exists because RunPod can report it independently, for instance when a worker is reclaimed, and an unmapped upstream status must not become an unhandled case.

RunPod does provide `POST /v2/{endpoint_id}/cancel/{job_id}`, so adding cancellation is a route plus one adapter call — the domain already understands the state and the upstream already supports it. `/retry` and `/purge-queue` exist on the same API and are similarly available. None are built; none require redesign.

Terminal states are written once. The reconciler only advances a job, never moves it backwards — an out-of-order poll response must not resurrect a completed job.

## Authentication

API key in `Authorization: Bearer <key>`, verified by middleware ahead of every `/v1` route.

Keys come from settings as a list of `key_id:secret` pairs, hashed once at startup into an in-memory map. No table, no migration — this is a small fixed set of callers, and a database round trip per request buys nothing.

Comparison is constant-time via `hmac.compare_digest`. Because the stored value is a fixed-length digest, the length-leak argument does not apply — the reason is the prefix leak: `==` short-circuits on the first differing byte, so comparison time correlates with how much of a guess is correct, which is enough to walk a digest byte by byte.

The resolved `api_key_id` lands in `RequestContext`, on the job row, and in every log line, which is what makes per-caller attribution and abuse investigation possible at all.

Rotation means editing settings and restarting. That is acceptable at this scale and recorded as a gap in [08](08-production-readiness.md).

Unauthenticated is not an option worth shipping: an open image-generation endpoint is a stranger's GPU on your credit card.

## RunPod adapter resilience

The adapter owns all upstream failure handling so `core/` never sees a transport concern.

- **Retry** on connection errors, timeouts, and 5xx: 3 attempts, exponential backoff with full jitter. Never retry 4xx — a rejected payload is rejected on every attempt.
- **Circuit breaker** around the endpoint: after N consecutive failures the breaker opens and `submit` fails fast with `UPSTREAM_UNAVAILABLE` for a cooldown, then half-opens for a single probe. Without it, an outage turns every request into a full retry cycle and the gateway spends its thread pool waiting on a dead host.
- **Timeouts** on every call. An httpx client with no timeout waits forever by default.

Breaker state is per-process and in-memory. That is correct for a single instance and wrong for a fleet; noted in [08](08-production-readiness.md).

## The reconciler

Nothing tells us when a job finishes — webhooks are documented-not-built ([03](03-facades.md)), so the only way to learn an outcome is to ask. The reconciler asks.

It is a distinct hop from the async facade. The client polls *us*; the reconciler polls *RunPod*. Neither is aware of the other, and the client would poll identically if we learned the result by webhook instead.

```
CLIENT ──poll──▶ GATEWAY ──poll──▶ RUNPOD
       async facade      reconciler
```

### Why not just ask RunPod when the client asks

A simpler design exists: have `GET /v1/jobs/{id}` call RunPod live and skip the background loop entirely. It is rejected for one reason.

**If no client is polling, that design never learns the job finished.** No record, no cost attribution, no metrics — and nothing to push. Announcing completion over SSE, a webhook, or an MCP notification requires knowing about it *without being asked*, and the reconciler is the only component that provides that. An on-demand design can answer questions; it can never announce anything.

The secondary reason: RunPod API calls would scale with client poll rate rather than with job count. Ten clients polling every 2s is five calls a second for one job.

### Cadence

| Setting | Value | Reason |
|---|---|---|
| Tick interval | 2s | Generation is ~20-25s, so ~10 polls per job. Adds at most 2s of phantom latency |
| Idle interval | 10s | When nothing is unresolved, stop querying at speed for no reason |
| Batch limit | 50 per tick | Bounds the work and the upstream call volume per tick |
| Jitter | ±20% | Two replicas starting together would otherwise tick in lockstep forever |

`claim_unresolved` orders oldest-first by `updated_at`, so when the backlog exceeds the batch limit, no job is starved.

### Rules

**Unknown is not failure.** If `runpod.status()` raises — timeout, connection error, breaker open — the job is left exactly as it is and retried next tick. Writing `FAILED` on our own inability to reach the upstream would discard a perfectly good result over a two-second blip, and terminal states are written once. Only an explicit RunPod `FAILED` becomes our `FAILED`.

**Transitions only advance.** Overlapping ticks can return a stale `IN_PROGRESS` after `COMPLETED` was already written. Terminal states are never overwritten.

**One job's failure does not abort the tick.** Each job is handled independently; an exception on one is logged and the loop continues. Otherwise a single malformed record stops every other job from ever resolving.

**Concurrent reconcilers do not collide.** `FOR UPDATE SKIP LOCKED` means a second instance takes different rows rather than blocking or duplicating.

**Orphaned jobs are adopted, not spun on.** `submit` inserts the row before calling RunPod, so a crash between the two leaves a `QUEUED` job with `runpod_job_id = None`. Nothing upstream corresponds to it, and a naive reconciler would claim it every tick forever with nothing to poll.

The rule: a claimed job with no `runpod_job_id` is **resubmitted**, not polled. It is safe because no upstream job exists to duplicate — that is precisely what the null means. Jobs older than the deadline are timed out as usual, so a permanently failing resubmit cannot loop indefinitely.

Recording the row first is deliberate: the alternative — submit, then insert — loses the job entirely if the crash lands the other way, and an untracked job still bills.

### Progress

The worker emits per-step progress via `progress_update` ([01](01-worker.md)), which RunPod returns on the status response while the job is still running. The reconciler stores it on the job so a client polling mid-generation gets a percentage rather than a bare `IN_PROGRESS`.

`progress` is a nullable jsonb column, overwritten each tick. It is not history — only the latest value matters, and keeping every step would write 28 rows per image for information nobody reads twice.

This is still pull, not push: RunPod holds the progress and we fetch it. **The tick interval therefore bounds progress granularity** — at 2s against a 22s generation, a client sees roughly ten updates, which is enough for a smooth bar and far short of per-step. Tightening it trades upstream API calls for resolution, and 2s is the chosen point.

A progress update is not a state transition. It never moves `status`, and it is ignored entirely on a terminal job.

### Timeout

A job whose deadline passes becomes `TIMED_OUT` with error code `JOB_TIMEOUT`. This is the only terminal state *we* originate rather than RunPod.

Without it, a job RunPod has lost stays unresolved forever: polled every tick, never answered, accumulating in the claim query.

```
deadline = created_at + JOB_TIMEOUT_SECONDS   # default 600
```

600s sits inside RunPod's **30-minute result retention** for `/run` jobs — a job resolved later than that would find nothing left to fetch — and well above the 300s execution timeout ([01](01-worker.md)) plus plausible queue wait. **The gateway deadline must always exceed the endpoint execution timeout** — set below it and we would time out jobs that are still running normally, then discard their results when they complete.

### Where it runs

**Here:** an asyncio task started in the FastAPI `lifespan` hook, in the same process. One container, nothing extra to run, which is what keeps `docker compose up` to a single command ([06](06-build-deploy.md)).

The consequence is that N gateway replicas means N reconcilers. That is why `SKIP LOCKED` is specified now rather than later.

On shutdown the task is cancelled and awaited, so an in-flight tick completes rather than being killed mid-write.

**In production:** a separate deployment, so API replicas can scale for request load without multiplying polling load. Noted in [08](08-production-readiness.md).

## Queue pressure

RunPod exposes `GET /v2/{endpoint_id}/health`:

```json
{"jobs": {"completed": 1, "failed": 5, "inProgress": 0, "inQueue": 2, "retried": 0},
 "workers": {"idle": 0, "running": 0}}
```

`jobs.inQueue` is the depth and `workers` is the capacity. Both are needed — a raw depth threshold is meaningless on its own, since 20 queued jobs are comfortable against 50 workers and hopeless against 3.

### Rejecting on time, not depth

```
capacity        = max(workers.running + workers.idle, 1)
estimated_wait  = (jobs.inQueue / capacity) * AVG_JOB_SECONDS
```

If `estimated_wait > MAX_QUEUE_WAIT_SECONDS` (default 120), `submit` fails with `429 QUEUE_SATURATED` and `Retry-After: ceil(estimated_wait * uniform(0.8, 1.2))`.

Two honest caveats about that number. `capacity` counts a *running* worker as available, which it is not until its current job finishes — so the estimate is a lower bound and real waits skew longer. And the jitter is not decoration: without it every shed client returns at the same instant, converting one queue spike into a synchronised second one.

Rejecting on estimated time rather than raw depth means the threshold survives a change to `max_workers`, and it makes `Retry-After` a real number rather than a guess. `AVG_JOB_SECONDS` starts at a stated estimate and is replaced by the measured p50 from [09](09-benchmarks.md).

The alternative — accept everything — is worse than it looks. A client whose job sits queued for six minutes and then times out has learned nothing, consumed a queue slot, and will very likely retry. Rejecting immediately lets it back off, and keeps the queue bounded.

### Refreshed on the reconciler tick, not per request

Calling `health()` inside `submit` would add an upstream round trip to the hot path and a second failure mode to every submission.

Instead the reconciler refreshes it on its existing 2s tick and caches the result in memory. Submissions read a value at most 2s stale, at zero latency cost and zero extra upstream calls regardless of request rate.

If the cached value is missing or stale beyond a grace period — the reconciler has died, or RunPod is unreachable — **submissions are allowed through**. This is the one place the design fails open, deliberately: queue pressure is a load-shedding optimisation, not a safety control, and refusing all traffic because we cannot measure the queue converts a monitoring failure into an outage. Guardrails fail closed ([04](04-guardrails.md)); this does not.

## The duplicated contract

Package isolation ([`STANDARDS.md`](../../STANDARDS.md) §2) forbids the gateway importing from the worker. But both define the generation parameters and both know the error codes, so the contract exists twice.

Three options were available:

| Option | Cost |
|---|---|
| A third shared package | A third `pyproject.toml`, a third lockfile, and a dependency that must be published or path-installed into two images. Real overhead for ~10 fields |
| Relax the isolation rule | Loses the guarantee that torch never reaches the gateway and FastAPI never reaches the worker — the thing keeping both images honest |
| **Duplicate, and test that they agree** | Two small definitions plus one test |

Duplication is chosen, with the drift made detectable rather than trusted.

`contracts/generation-request.schema.json` and `contracts/error-codes.json` are committed at the repository root. Each package has a test asserting its own definitions match the committed contract — the worker that it accepts every field, the gateway that it sends them, and both that their error-code sets are identical.

The contract files are the source of truth; the two implementations are conformant copies. Changing a field means changing the contract, and both test suites fail until both sides follow. That is the behaviour a shared package would have given, without a third package to build and ship.

This is the honest cost of the isolation rule, paid explicitly. Silent drift between two copies of a contract is a genuinely nasty failure — the gateway accepts a request the worker rejects, and it only shows up in production.

## Error codes

| Code | Tier | Meaning |
|---|---|---|
| `INVALID_PROMPT` | worker, gateway | Blank, or over the 2000-character cap |
| `INVALID_DIMENSIONS` | worker, gateway | Outside 256–1536 |
| `INVALID_STEPS` | worker, gateway | Outside 1–50 |
| `PROMPT_BLOCKED` | worker, gateway | Guardrail rejection |
| `IMAGE_BLOCKED` | worker | Post-generation guardrail rejection |
| `OOM` | worker | VRAM exhausted; `refresh_worker` set |
| `INFERENCE_FAILED` | worker | Unclassified pipeline failure |
| `UNAUTHENTICATED` | gateway | Missing or invalid API key |
| `UPSTREAM_UNAVAILABLE` | gateway | RunPod unreachable, 5xx, or breaker open |
| `QUEUE_SATURATED` | gateway | Estimated queue wait over threshold; `Retry-After` set |
| `JOB_NOT_FOUND` | gateway | Unknown id |
| `JOB_TIMEOUT` | gateway | Exceeded the 600s deadline |
| `IDEMPOTENCY_CONFLICT` | gateway | Key reused with a different body |

Envelope:

```json
{"error": {"code": "INVALID_PROMPT",
           "message": "Prompt is 2841 characters; the limit is 2000.",
           "suggestion": "Shorten the prompt to 2000 characters or fewer.",
           "correlation_id": "01J..."}}
```

`suggestion` exists because callers include agents, which cannot infer a next action from prose. `correlation_id` is on every error so a user-reported failure maps to logs on both tiers without a search.
