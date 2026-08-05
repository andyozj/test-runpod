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
    async def update(self, job_id: UUID, **changes: Any) -> Job: ...
    async def list_unresolved(self, limit: int) -> list[Job]: ...

class RunPodClient(Protocol):
    async def submit(self, payload: dict[str, Any]) -> str: ...
    async def status(self, runpod_job_id: str) -> RunPodJobStatus: ...
    async def health(self) -> EndpointHealth: ...
```

Postgres and httpx implementations live in `adapters/`. The whole gateway is buildable and testable against fakes for both, which is why Phases 3 and 4 are not blocked on the endpoint existing.

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

Terminal states are written once. The reconciler only advances a job, never moves it backwards — an out-of-order poll response must not resurrect a completed job.

## Authentication

API key in `Authorization: Bearer <key>`, verified by middleware ahead of every `/v1` route.

Keys are stored hashed. Comparison is constant-time. The resolved `api_key_id` lands in `RequestContext`, on the job row, and in every log line, which is what makes per-caller attribution and abuse investigation possible at all.

Unauthenticated is not an option worth shipping: an open image-generation endpoint is a stranger's GPU on your credit card.

Key rotation and per-key quotas are out of scope — see [08](08-production-readiness.md).

## RunPod adapter resilience

The adapter owns all upstream failure handling so `core/` never sees a transport concern.

- **Retry** on connection errors, timeouts, and 5xx: 3 attempts, exponential backoff with full jitter. Never retry 4xx — a rejected payload is rejected on every attempt.
- **Circuit breaker** around the endpoint: after N consecutive failures the breaker opens and `submit` fails fast with `UPSTREAM_UNAVAILABLE` for a cooldown, then half-opens for a single probe. Without it, an outage turns every request into a full retry cycle and the gateway spends its thread pool waiting on a dead host.
- **Timeouts** on every call. An httpx client with no timeout waits forever by default.

Breaker state is per-process and in-memory. That is correct for a single instance and wrong for a fleet; noted in [08](08-production-readiness.md).

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
