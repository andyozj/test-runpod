# FLUX.1-dev on RunPod Serverless — Design

**Date:** 2026-08-05
**Status:** Approved for planning
**Governed by:** [`STANDARDS.md`](../../../STANDARDS.md)

## 1. Context

Take-home for RunPod. The brief requires a `handler.py`, a Docker image containing the handler **and the model**, a deployed serverless endpoint, a demonstration of text → image, and clear documentation.

Graded on: following the setup instructions, handler/model integration quality, successful deployment, and documentation clarity.

### Scope decisions

| Decision | Value |
|---|---|
| Model | `black-forest-labs/FLUX.1-dev`, bf16, unquantized |
| Inference stack | `diffusers.FluxPipeline`, no `torch.compile`, no ComfyUI |
| Weights | Baked into the image, per the explicit instruction |
| Build host | RunPod GPU Pod |
| Registry | GHCR |
| Scope | Two tiers: the serverless worker, plus a FastAPI gateway |
| Facades built | Sync HTTP, async HTTP + polling |
| Facades documented only | Webhook-out, SSE, WebSocket, MCP |
| Out of scope | Fine-tuning, LoRA, batch inference, multi-region, priority queues |

The gateway is deliberately beyond the brief. It is sequenced strictly after the graded deliverable is deployed and benchmarked, so that running out of time degrades the bonus rather than the submission.

## 2. Architecture

```
client / agent
      │
      │  ① sync: POST /v1/images         (blocks, returns image)
      │  ② async: POST /v1/jobs → job_id
      │           GET  /v1/jobs/{id}
      ▼
┌─────────────────────────────────────────────┐
│ gateway (FastAPI)                            │
│                                              │
│   api/  ──────┐                              │
│   workers/ ───┼──►  core/JobService          │
│   adapters/ ──┘        │                     │
│                        ├─► JobRepository ◄── Postgres
│                        └─► RunPodClient  ◄── httpx
└─────────────────────────────────────────────┘
      │  POST /v2/{endpoint}/run
      ▼
┌─────────────────────────────────────────────┐
│ RunPod Serverless (L40S 48GB)                │
│   worker image, weights baked                │
│     handler.py → pipeline.py → FluxPipeline  │
└─────────────────────────────────────────────┘
```

Dependencies point inward. `core/` imports nothing from `api/`, `adapters/`, or `workers/` — it declares `Protocol` interfaces that `adapters/` implements. Per `STANDARDS.md` §3 this is the load-bearing structural rule: it is what makes each additional transport facade a ~50-line addition instead of a rewrite.

## 3. Tier 1 — the worker

### 3.1 Modules

| Module | Responsibility |
|---|---|
| `handler.py` | RunPod entrypoint. Parse → delegate → serialise. No inference logic. |
| `pipeline.py` | Lazy pipeline accessor and the `ImagePipeline` protocol. |
| `inference.py` | `generate(request, pipeline) -> GenerationResult`. Pure, injectable. |
| `schemas.py` | `GenerationRequest`, `GenerationResult`, error envelope. |
| `settings.py` | `pydantic-settings`. The only module reading the environment. |
| `errors.py` | Domain exceptions and error codes. |

### 3.2 The lazy pipeline

The pipeline must load once per worker (not per job) to preserve the FlashBoot benefit, while `handler.py` must remain importable without a GPU (`STANDARDS.md` §9).

```python
_pipeline: ImagePipeline | None = None

def get_pipeline() -> ImagePipeline:
    """Return the process-wide FLUX pipeline, loading it on first use."""
    global _pipeline
    if _pipeline is None:
        _pipeline = _load_pipeline()
    return _pipeline
```

A module-level constant satisfies the first requirement and breaks the second. The accessor satisfies both: tests inject a fake via `set_pipeline()`, and a warm worker still pays load cost exactly once.

`ImagePipeline` is a `Protocol` defined locally rather than a direct `FluxPipeline` dependency. `diffusers` is not usefully typed; the protocol is what makes `mypy strict` viable and gives tests something to fake.

**Warm-up:** `get_pipeline()` is called in the `if __name__ == "__main__":` block, before `runpod.serverless.start()`. Production workers therefore load during container start rather than during the first billed job, while merely *importing* `handler.py` — which is what tests do — never touches the GPU. No configuration flag is needed; the entrypoint and the import path are simply different.

### 3.3 Handler contract

Input (`GenerationRequest`):

| Field | Type | Default | Constraint |
|---|---|---|---|
| `prompt` | `str` | required | 1–2000 chars, non-blank |
| `width` | `int` | 1024 | 256–1536, snapped down to ×16 |
| `height` | `int` | 1024 | 256–1536, snapped down to ×16 |
| `num_inference_steps` | `int` | 28 | 1–50 |
| `guidance_scale` | `float` | 3.5 | 0–20 |
| `seed` | `int \| None` | `None` | randomised when absent, always echoed |
| `output_format` | `"png" \| "jpeg"` | `"png"` | — |
| `correlation_id` | `str \| None` | `None` | bound into the log context |

FLUX.1-dev is guidance-distilled and takes no negative prompt. The schema omits the field rather than accepting and ignoring it.

Dimensions snap down to a multiple of 16 (the FLUX latent space is 16× downsampled) and the effective values are returned. Recent `diffusers` already rounds and warns — verify at implementation time and delegate rather than duplicate if so.

Output:

```json
{
  "image_base64": "...", "format": "png",
  "seed": 42, "width": 1024, "height": 1024,
  "num_inference_steps": 28, "guidance_scale": 3.5,
  "timings": {"inference_s": 21.4, "encode_s": 0.3}
}
```

Base64 inline. A 1024² PNG is ~1.5MB → ~2MB encoded. RunPod does not publish a payload ceiling in its endpoint docs, so **the actual limit is measured during Phase 2 at 1536² and recorded in `BENCHMARKS.md`** rather than assumed. If it binds, JPEG becomes the default and S3 output is added.

### 3.4 Image build

Base `nvidia/cuda:12.4.1-runtime-ubuntu22.04`, `uv` for dependency install, weights fetched at build time.

Two build details are non-obvious and both are build-blockers:

**Gated repo.** FLUX.1-dev requires accepting the licence on HuggingFace and an `HF_TOKEN`. It is passed as a BuildKit secret, never an `ARG` — an `ARG` token is recoverable from image history (`STANDARDS.md` §11).

```dockerfile
RUN --mount=type=secret,id=hf_token \
    HF_TOKEN=$(cat /run/secrets/hf_token) python scripts/fetch_weights.py
```

**Duplicate weights.** The repo ships both the `diffusers` sharded layout *and* standalone `flux1-dev.safetensors` (23.8GB) + `ae.safetensors`. A naive `snapshot_download` pulls ~56GB. `fetch_weights.py` uses `ignore_patterns` to take only the diffusers layout (~33GB).

Layer order: system deps → Python deps → weights → application code. Code changes must not invalidate the 33GB weight layer.

Expected image size ~45GB. Pushed to GHCR — Docker Hub's free-tier pull rate limits can throttle RunPod scaling.

### 3.5 Endpoint configuration

| Setting | Value | Reason |
|---|---|---|
| GPU | L40S 48GB | bf16 FLUX needs ~24–26GB steady, more at 1536². 24GB is too tight to be safe. |
| Workers | min 0, max 3 | Scale to zero. 3 is enough to demonstrate concurrency. |
| FlashBoot | on | Preserves VRAM residency between jobs. |
| Idle timeout | 60s | Long enough that a demo sequence stays warm. |
| Execution timeout | 300s | Well above worst-case 50-step 1536². |
| `concurrency_modifier` | 1 | The worker is GPU-bound; a second concurrent job only causes VRAM contention. |

**GPU selection is provisional.** L40S is the reasoned starting point; the final recommendation comes from measured Phase 2 numbers across at least L40S and A100 80GB, per `STANDARDS.md` §10.

### 3.6 Errors

`torch.cuda.OutOfMemoryError` is caught explicitly: log with context, `torch.cuda.empty_cache()`, return the error envelope with `refresh_worker: True`. VRAM fragmentation outlives `empty_cache()`, so the worker is retired rather than reused.

## 4. Tier 2 — the gateway

### 4.1 Core

`core/service.py` holds `JobService` with `submit()`, `get()`, and `complete()`. It depends only on two protocols:

```python
class JobRepository(Protocol):
    async def create(self, job: Job) -> Job: ...
    async def get(self, job_id: UUID) -> Job | None: ...
    async def get_by_idempotency_key(self, key: str) -> Job | None: ...
    async def update_status(self, job_id: UUID, status: JobStatus, ...) -> Job: ...
    async def list_unresolved(self, limit: int) -> list[Job]: ...

class RunPodClient(Protocol):
    async def submit(self, payload: dict[str, Any]) -> str: ...
    async def status(self, runpod_job_id: str) -> RunPodJobStatus: ...
    async def health(self) -> EndpointHealth: ...
```

Postgres and httpx implementations live in `adapters/`. `core/` imports neither.

### 4.2 Data model

`jobs` — `id` (uuid pk), `idempotency_key` (unique, nullable), `runpod_job_id` (nullable, indexed), `status`, `request` (jsonb), `result` (jsonb, nullable), `error_code`, `error_message`, `correlation_id`, `created_at`, `updated_at`, `completed_at`.

Idempotency is enforced by the unique constraint, not by a read-then-write check — the check-then-insert race is exactly the failure mode idempotency exists to prevent. Insert first, catch the violation, return the existing job.

Status is a domain enum mapped from RunPod's (`IN_QUEUE`, `IN_PROGRESS`, `COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT`) in the adapter. RunPod vocabulary does not leak into `core/`.

Migrations via Alembic.

### 4.3 Facades built

**Sync** — `POST /v1/images` submits and polls internally until terminal, then returns the image. Bounded by a server-side deadline shorter than the client's; on expiry it returns `202` with a `job_id` rather than hanging. Convenient for `curl` and the demo.

**Async** — `POST /v1/jobs` returns `202` with a `job_id`; `GET /v1/jobs/{id}` returns status and, when terminal, the result. The honest interface for a 20-25s operation.

### 4.4 Reconciler

A background task polls RunPod for non-terminal jobs on an interval with backoff. It is the sole completion mechanism today, and remains the fallback if webhook-out is built later — which is precisely the resilience argument for keeping it: webhook delivery is best-effort (RunPod retries twice at 10s intervals, then stops), so a poller must exist regardless.

### 4.5 Queue pressure

`POST` returns `429` with `Retry-After` when RunPod queue depth exceeds a threshold. A client that learns immediately can back off; a client whose job sits queued for minutes and then times out has learned nothing and consumed capacity.

### 4.6 Facades documented only

`docs/FACADES.md` covers each with its consumer-side impact — the point being that the transport choice is a cost imposed on the *caller*, not just an implementation detail.

| Facade | Consumer impact | Why not built |
|---|---|---|
| Sync HTTP | Simplest possible client. But holds a connection 20–25s, and most load balancers and API gateways idle-timeout at 30–60s. No safe retry — a retry re-bills a fresh generation. | Built |
| Async + poll | Two calls plus a loop. Resilient, retry-safe, survives client restarts. Polling cost grows linearly with active jobs. | Built |
| Webhook out | No polling cost, lowest notification latency. Requires the consumer to run a public HTTPS endpoint, verify signatures, tolerate out-of-order and duplicate deliveries, and still poll as a fallback. Impossible for browser clients. | Callback field plumbed; route unbuilt |
| SSE | Per-step progress, good perceived latency. One long-lived connection per job; intermediate proxies buffer and break it; unidirectional. Needs `progress_update` from the worker — real GPU-side work. | Cost on both tiers |
| WebSocket | Bidirectional, enables mid-generation cancel. Needs sticky sessions and connection-state handling. | Infrastructure weight |
| MCP | Agent-native tool call, zero glue for MCP clients. Not a general-purpose API — a wrapper over the async facade. | Scope |

### 4.7 Agent-callable design

Errors carry a stable `code`, a `message`, and a `suggestion` where a next action exists (`STANDARDS.md` §7). Only `prompt` is required; everything else defaults. Responses echo effective values — the seed actually used and the dimensions actually rendered — so a caller never has to infer what happened. This is what makes retries deterministic and is the prerequisite for the MCP facade.

## 5. Error codes

| Code | Tier | Meaning |
|---|---|---|
| `INVALID_PROMPT` | worker, gateway | Empty, blank, or over length |
| `INVALID_DIMENSIONS` | worker, gateway | Outside 256–1536 |
| `INVALID_STEPS` | worker, gateway | Outside 1–50 |
| `OOM` | worker | VRAM exhausted; `refresh_worker` set |
| `INFERENCE_FAILED` | worker | Unclassified pipeline failure |
| `UPSTREAM_UNAVAILABLE` | gateway | RunPod unreachable or 5xx |
| `QUEUE_SATURATED` | gateway | Depth over threshold; `Retry-After` set |
| `JOB_NOT_FOUND` | gateway | Unknown id |
| `JOB_TIMEOUT` | gateway | Exceeded deadline |

## 6. Observability

`structlog`, JSON, flat fields. Correlation ID generated at the gateway from `X-Correlation-ID` or fresh, persisted on the job, passed into the RunPod job input, bound into the worker's log context. One ID traces HTTP → GPU → response.

Worker emits: `pipeline_loaded` (duration), `generation_started`, `generation_completed` (duration, steps, resolution, seed), `oom_detected`.

Prompts are user content — log length and SHA-256 prefix, never the text (`STANDARDS.md` §8).

RunPod serverless supports no sidecars. Handler stdout is the entire in-container surface; queue depth, cold-start failures, and endpoint reachability are observable only from outside via the RunPod API. `RUNBOOK.md` documents what is visible from where.

## 7. Testing

Per `STANDARDS.md` §9. No unit or integration test may require a GPU, network, or weights.

| Tier | Coverage |
|---|---|
| Unit | Schema validation and dimension snapping (parametrized boundaries: 255/256/1000/1536/1537). Handler success and every error path against a fake `ImagePipeline`, including a fake raising `OutOfMemoryError` to assert `refresh_worker`. `JobService` against in-memory fakes for both protocols. Status mapping. |
| Integration | Gateway against Postgres via testcontainers, RunPod adapter against a mocked transport. Idempotency race asserted with concurrent inserts. Alembic up/down. |
| E2E | `@pytest.mark.gpu`, deselected by default. Prompt → image against the live endpoint; idempotency replay; oversized-payload probe to find the real response ceiling. |

Doctests run for `core/` and `schemas.py` only, via `--doctest-modules` on those paths, so the required `Example` sections are verified rather than decorative.

Coverage: 80% minimum, 100% on validation and the error-code contract.

## 8. Sequencing

Phase 2 is the graded deliverable. Nothing in Phase 3+ starts until it is deployed, working, and benchmarked.

| Phase | Output | Gate |
|---|---|---|
| 0 | Repo scaffold, both `pyproject.toml`, pre-commit, CI | `make check` green |
| 1 | Worker: schemas, pipeline protocol, inference, handler, unit tests | Full suite green with no GPU |
| 2 | **Build on Pod, push to GHCR, deploy endpoint, smoke test, benchmark** | **Image generated from a prompt; `BENCHMARKS.md` populated** |
| 3 | Gateway core, adapters, migrations, unit + integration tests | Coverage gate |
| 4 | Sync and async facades, reconciler, compose | E2E through the gateway |
| 5 | Client demo script, README, ARCHITECTURE, FACADES, RUNBOOK | Docs complete |

Phase 2 also produces the measured numbers that replace every provisional figure in this document: cold start, warm latency at 20/28/50 steps, latency at 1024²/1536², cost per image at the verified L40S serverless rate of **$1.75/hr** (RunPod pricing page, 2026-08-05), and the observed response size ceiling.

## 9. Risks

| Risk | Mitigation |
|---|---|
| HF licence acceptance and RunPod credits are external dependencies | Both requested on day 1, before any code |
| ~45GB image push is slow and failure-prone | Build and push from the Pod; retry-resumable; GHCR avoids Docker Hub rate limits |
| L40S unavailable in the chosen region | Endpoint config allows a GPU-type fallback list; benchmark A100 80GB regardless |
| Base64 response exceeds an undocumented ceiling | Measured in Phase 2; JPEG default and S3 output are the prepared fallbacks |
| Gateway consumes time needed by the graded deliverable | Phase gate — Phase 2 must be complete and demonstrated first |
| `diffusers` API drift | Pin exact versions; `uv.lock` committed |

## 10. Corrections to prior design notes

This supersedes the earlier architecture notes. Substantive changes:

1. **Weights are baked into the image, not mounted from a network volume.** The brief requires it explicitly. The volume approach is documented in `ARCHITECTURE.md` as the production evolution, with its region-pinning cost stated.
2. **Cost figures were wrong by ~12×.** L40S serverless is $1.75/hr, not $0.54/hr ($0.99/hr is the *Pod* rate); 1024²/28 steps is ~20–25s, not 4–6s. All figures are now measured, per `STANDARDS.md` §10.
3. **Module-level pipeline initialisation replaced by a lazy accessor.** The original made `handler.py` unimportable without a GPU and therefore untestable.
4. **CI does not build the image.** Standard GitHub runners have ~14GB free disk.
5. **Gated-repo handling and the duplicate-weights trap added.** Both were absent and both block the build.
6. **Non-commercial licence noted.** FLUX.1-dev is not licensed for commercial use; stated in the README.

Retained as correct: diffusers over ComfyUI, no `torch.compile`, `refresh_worker` on OOM, fail-fast startup validation, never deploying `latest`, and the webhook-plus-polling resilience argument.
