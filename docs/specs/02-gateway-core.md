# 02 — Gateway core

Tier 2. The domain layer and its persistence. Contains no HTTP and no RunPod vocabulary.

> **Diagram:** job state machine — *pending Excalidraw*

## Layering

```
api/  adapters/  workers/     may import core/
        │
        ▼
      core/                   imports nothing outward
```

`core/` must not import FastAPI, httpx, psycopg, or SQLAlchemy. Enforced by `import-linter` in `make check`, not by review — a stray `from fastapi import HTTPException` passes ruff, mypy, and every test while welding the domain to one transport, and the cost surfaces only when the second facade turns out to be a rewrite.

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
    async def mark_failed(self, job_id: UUID, code: str, message: str) -> Job: ...
    async def mark_blocked(self, job_id: UUID, verdict: GuardrailVerdict) -> Job: ...
    async def claim_unresolved(self, limit: int) -> list[Job]: ...

class RunPodClient(Protocol):
    async def submit(self, payload: dict[str, Any]) -> str: ...
    async def status(self, runpod_job_id: str) -> RunPodJobStatus: ...
    async def health(self) -> EndpointHealth: ...
```

Postgres and httpx implementations live in `adapters/`. The whole gateway is buildable and testable against fakes for both, which is why Phases 3 and 4 are not blocked on the endpoint existing.

**Named transitions, not a generic `update(**changes)`.** A catch-all writer types nothing — mypy cannot check that a completion carries a result or that a failure carries a code, and the call site does not say what it means. Each named method encodes one legal transition, so an illegal one is a type error rather than a runtime surprise.

**`claim_unresolved`, not `list_unresolved`.** It issues `SELECT ... FOR UPDATE SKIP LOCKED`. With one reconciler this is redundant; with two it is the difference between polling each job once and polling it twice, double-counting and racing on the write. The cost of specifying it now is one clause.

## Data model

`jobs`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid | pk |
| `idempotency_key` | text | unique, nullable |
| `runpod_job_id` | text | nullable, indexed |
| `status` | enum | domain enum, below |
| `request` | jsonb | validated parameters as submitted |
| `result` | jsonb | nullable |
| `error_code` | text | nullable |
| `error_message` | text | nullable |
| `correlation_id` | text | indexed |
| `api_key_id` | text | which caller submitted it |
| `created_at` / `updated_at` / `completed_at` | timestamptz | `completed_at` nullable |

Migrations via Alembic. `api_key_id` exists from the first migration — retrofitting attribution onto existing rows is not possible, and without it there is no way to answer "who generated this".

### Idempotency

Enforced by the unique constraint, not a read-then-write check. Check-then-insert has exactly the race that idempotency exists to prevent: two concurrent identical requests both see no existing row and both submit, double-billing the GPU.

Insert first, catch the unique violation, return the existing job. The integration suite asserts this with genuinely concurrent inserts, not sequential ones — a sequential test passes against the broken implementation.

### Status

Domain enum: `QUEUED`, `IN_PROGRESS`, `COMPLETED`, `FAILED`, `TIMED_OUT`, `CANCELLED`, `BLOCKED`.

`BLOCKED` is distinct from `FAILED`: a guardrail rejection is not an error, it is a decision, and conflating them makes the block rate unmeasurable.

RunPod's vocabulary (`IN_QUEUE`, `IN_PROGRESS`, `COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT`) is mapped in the adapter. It does not leak into `core/`.

`CANCELLED` has no producer on our side — there is no cancel endpoint. It exists solely because RunPod can report it independently, for instance when a worker is reclaimed, and an unmapped upstream status must not become an unhandled case. Adding a cancel endpoint later is a route plus one client call; the domain already understands the state.

Terminal states are written once. The reconciler only advances a job, never moves it backwards — an out-of-order poll response must not resurrect a completed job.

## Authentication

API key in `Authorization: Bearer <key>`, verified by middleware ahead of every `/v1` route.

Keys come from settings as a list of `key_id:secret` pairs, hashed once at startup into an in-memory map. No table, no migration — this is a small fixed set of callers, and a database round trip per request buys nothing.

Comparison is constant-time via `hmac.compare_digest`. A naive `==` on a secret leaks length and prefix information through timing, and the correct call is the same length as the wrong one.

The resolved `api_key_id` lands in `RequestContext`, on the job row, and in every log line, which is what makes per-caller attribution and abuse investigation possible at all.

Rotation means editing settings and restarting. That is acceptable at this scale and recorded as a gap in [08](08-production-readiness.md).

Unauthenticated is not an option worth shipping: an open image-generation endpoint is a stranger's GPU on your credit card.

Key rotation and per-key quotas are out of scope — see [08](08-production-readiness.md).

## RunPod adapter resilience

The adapter owns all upstream failure handling so `core/` never sees a transport concern.

- **Retry** on connection errors, timeouts, and 5xx: 3 attempts, exponential backoff with full jitter. Never retry 4xx — a rejected payload is rejected on every attempt.
- **Circuit breaker** around the endpoint: after N consecutive failures the breaker opens and `submit` fails fast with `UPSTREAM_UNAVAILABLE` for a cooldown, then half-opens for a single probe. Without it, an outage turns every request into a full retry cycle and the gateway spends its thread pool waiting on a dead host.
- **Timeouts** on every call. An httpx client with no timeout waits forever by default.

Breaker state is per-process and in-memory. That is correct for a single instance and wrong for a fleet; noted in [08](08-production-readiness.md).

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
| `INVALID_PROMPT` | worker, gateway | Blank, or over the T5 token limit |
| `INVALID_DIMENSIONS` | worker, gateway | Outside 256–1536 |
| `INVALID_STEPS` | worker, gateway | Outside 1–50 |
| `PROMPT_BLOCKED` | worker, gateway | Guardrail rejection |
| `IMAGE_BLOCKED` | worker | Post-generation guardrail rejection |
| `OOM` | worker | VRAM exhausted; `refresh_worker` set |
| `INFERENCE_FAILED` | worker | Unclassified pipeline failure |
| `UNAUTHENTICATED` | gateway | Missing or invalid API key |
| `UPSTREAM_UNAVAILABLE` | gateway | RunPod unreachable, 5xx, or breaker open |
| `JOB_NOT_FOUND` | gateway | Unknown id |
| `JOB_TIMEOUT` | gateway | Exceeded deadline |

Envelope:

```json
{"error": {"code": "INVALID_PROMPT",
           "message": "Prompt is 640 tokens; the limit is 512.",
           "suggestion": "Shorten the prompt to under 512 T5 tokens.",
           "correlation_id": "01J..."}}
```

`suggestion` exists because callers include agents, which cannot infer a next action from prose. `correlation_id` is on every error so a user-reported failure maps to logs on both tiers without a search.
