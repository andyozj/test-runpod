# 03: Facades

Transports over one `JobService`. A transport choice is a **cost imposed on the caller**, not an implementation detail, so each option is described by what it forces the consumer to do.

## Async only

There is one interface. Submit, then poll.

```
POST /v1/jobs        → 202  {"job_id": ...}
GET  /v1/jobs/{id}   → 200  status, progress, and result when terminal
```

**No synchronous endpoint is provided.** A blocking call was considered as a `curl`-friendly convenience and rejected on two grounds:

- **It does not scale.** Every in-flight request holds a connection and a worker for 20-25s. Concurrency is then bounded by held connections rather than by GPU capacity, and the failure mode under load is connection exhaustion in the gateway while the GPUs sit idle.
- **It would fail exactly when first tried.** A cold worker spends 30-60s loading the pipeline before generating for another 20-25s. Any deadline short enough to be safe against load-balancer idle timeouts (typically 30-60s) is comfortably exceeded by the first request after an idle period. The convenience path would be the path most likely to look broken on a reviewer's opening call.

The cost is that a bare `curl` is two commands instead of one. `client/generate.py` covers that, and the README shows both.

## Endpoints

All routes require `Authorization: Bearer <api-key>`.

### `POST /v1/jobs`

Headers:

| Header | Required | Purpose |
|---|---|---|
| `Authorization` | yes | `Bearer <api-key>` |
| `Idempotency-Key` | no | Safe retries (see below) |
| `X-Correlation-ID` | no | Generated if absent; echoed on the response |

Body: only `prompt` is required.

```json
{
  "prompt": "a red fox in falling snow, cinematic lighting",
  "width": 1024,
  "height": 1024,
  "num_inference_steps": 28,
  "guidance_scale": 3.5,
  "seed": null,
  "output_format": "png"
}
```

`202 Accepted`:

```json
{
  "job_id": "0192f3a1-...",
  "status": "QUEUED",
  "created_at": "2026-08-05T10:31:02Z"
}
```

### `GET /v1/jobs/{id}`

One response shape, three populated states.

Running:

```json
{
  "job_id": "0192f3a1-...",
  "status": "IN_PROGRESS",
  "progress": {"step": 12, "total": 28, "percent": 43},
  "result": null,
  "error": null,
  "created_at": "2026-08-05T10:31:02Z",
  "updated_at": "2026-08-05T10:31:19Z"
}
```

Completed:

```json
{
  "job_id": "0192f3a1-...",
  "status": "COMPLETED",
  "progress": {"step": 28, "total": 28, "percent": 100},
  "result": {
    "image_base64": "iVBORw0KGgoAAAANS...",
    "format": "png",
    "seed": 918273,
    "width": 1024, "height": 1024,
    "num_inference_steps": 28, "guidance_scale": 3.5,
    "model_version": "black-forest-labs/FLUX.1-dev@<revision>",
    "inference_seconds": 21.4
  },
  "error": null,
  "created_at": "...", "completed_at": "..."
}
```

Failed or blocked:

```json
{
  "job_id": "0192f3a1-...",
  "status": "BLOCKED",
  "progress": null,
  "result": null,
  "error": {
    "code": "PROMPT_BLOCKED",
    "message": "Prompt rejected by content policy.",
    "suggestion": "Rephrase the prompt and resubmit.",
    "correlation_id": "01J..."
  }
}
```

`PROMPT_BLOCKED` and `IMAGE_BLOCKED` are policy verdicts only. A guardrail that *crashes* still fails closed, but reports `INFERENCE_FAILED`: a crash is an infra fault and retryable as-is, a block is terminal until the prompt changes, and conflating them would misreport a guardrail outage as a policy spike ([04](04-guardrails.md#failure-policy)).

`progress` is the only reason polling mid-generation is informative. Without it every poll before completion returns an indistinguishable `IN_PROGRESS`, and no client can show a bar or estimate anything ([02](02-gateway-core.md#progress)).

Poll guidance, published in the API docs: **every 2s**, backing off to 5s after 60s, giving up at the 600s job deadline. Undocumented polling guidance produces either hammering or sluggish clients.

### `POST /v1/jobs/{id}/cancel`

Stops a queued or running job and returns it in `CANCELLED`.

Delegates to RunPod's own `POST /v2/{endpoint}/cancel/{id}` rather than
reimplementing it. The platform owns the queue, so it is the only thing that
can actually stop the work and stop the billing. A job already terminal is
returned unchanged: cancelling a completed job must not discard its result.

### `GET /health`, `GET /health/detailed`

See [05](05-observability.md).

## Status codes

| Code | When | Error code |
|---|---|---|
| `200` | Status read; or an idempotent replay of an existing job | - |
| `202` | Job created | - |
| `400` | Request failed validation | `INVALID_PROMPT`, `INVALID_DIMENSIONS`, `INVALID_STEPS` |
| `401` | Missing or invalid API key | `UNAUTHENTICATED` |
| `404` | Unknown job id | `JOB_NOT_FOUND` |
| `409` | `Idempotency-Key` reused with a different body | `IDEMPOTENCY_CONFLICT` |
| `429` | Estimated queue wait over threshold, **or** the caller is at its `MAX_ACTIVE_JOBS_PER_KEY` cap; `Retry-After` set | `QUEUE_SATURATED` |
| `503` | RunPod unreachable or circuit breaker open | `UPSTREAM_UNAVAILABLE` |

FastAPI's default `422` for validation failures is remapped to `400` with our envelope, so every error a caller sees has the same shape.

**A failed job is still `200`.** The HTTP call succeeded; the *job* failed, and its outcome is in the body. Returning `500` for a job that OOM'd would conflate "your request was malformed or we are broken" with "the work ran and did not succeed", and a client retrying on 5xx would resubmit a request that will fail identically. Transport failures get transport status codes; job outcomes get job status.

## Idempotency

The client generates a unique key per logical request (a UUID is fine) and sends it as a header:

```
Idempotency-Key: 7f3a9c22-...
```

This follows the Stripe convention because clients and agents already know it.

Behaviour:

| Situation | Response |
|---|---|
| New key | Job created, `202` |
| Same key, same body | `200` with the **original** job, header `Idempotency-Replayed: true`. No second generation, no second charge |
| Same key, different body | `409 IDEMPOTENCY_CONFLICT` |
| No key | Every request creates a new job |

The `409` matters. Silently returning the first job when the body has changed hands the caller an image of something they did not ask for, and the mismatch would be invisible. A request hash is stored alongside the key to detect it.

**Keys are scoped per caller.** The unique constraint is on `(api_key_id, idempotency_key)`, not the key alone: otherwise two clients choosing the same key collide, and one receives the other's image. That is a data leak, not an inconvenience.

Without a key, a retry after a network timeout generates and bills twice. With one, the retry is free. This is the single cheapest protection against double-spend on a 20-25s GPU operation.

## Documented, not built

The protocol boundary in [02](02-gateway-core.md) makes each an addition rather than a rewrite. Cost below is what the *consumer* pays.

| Facade | Consumer cost | Consumer benefit | Why not built |
|---|---|---|---|
| **Webhook out** | Must run a public HTTPS endpoint, verify signatures, tolerate duplicate and out-of-order delivery, and *still* poll as a fallback. Impossible for browser clients | No polling cost, lowest notification latency | Callback field plumbed, route unbuilt. Delivery is best-effort (RunPod retries twice at 10s intervals then stops), so the poller is required regardless |
| **SSE** | One long-lived connection per job. Intermediate proxies buffer and break it. Unidirectional. Needs reconnect via `Last-Event-ID` | Live progress without a polling loop | **The strongest candidate.** The worker already emits per-step progress and the reconciler already stores it, so a stream would carry real events. Unbuilt for scope, not for lack of content |
| **WebSocket** | Connection state, reconnect logic, sticky sessions through any load balancer | Bidirectional; enables mid-generation cancel | Infrastructure weight disproportionate to the benefit |
| **MCP** | None: the client is an agent framework and the tool schema is the contract | Agent-native call with zero glue | A thin wrapper over the async API. Cheap to add; out of scope |
| **gRPC** | Codegen and a toolchain | Efficient streaming, typed contract | No consumer asking for it |

## The result is the image, not a reference

The worker returns base64 inline ([01](01-worker.md)). No storage backend, no
reference, no proxy route.

An earlier revision returned a storage key on the grounds that a ~200-byte
result can be pushed over SSE or a webhook while a 2.7MB blob cannot. That
argument is sound and it was the wrong trade here: RunPod's job surface is
fixed, `GET /status/{job_id}` returns the handler's output verbatim, and a key
is unresolvable by anyone not running the gateway. It made the graded tier
depend on the ungraded one.

Object storage is the right answer at a scale this does not operate at, and it
is recorded as an extension in [08](08-production-readiness.md) rather than
built and left inert.


## Why the boundary makes this cheap

Each facade translates HTTP-shaped input into a `JobService` call and translates the result back. None touch persistence, RunPod, or the domain rules.

That holds only while `core/` stays free of transport concerns. One `HTTPException` raised from `JobService` and every other facade inherits an HTTP dependency it has no use for. Hence `import-linter` rather than intention (see [02](02-gateway-core.md#layering)).

## Agent-callable design

Callers include automated agents, which changes three things:

- Errors carry a stable machine-readable `code` and a `suggestion` naming the next valid action. An agent cannot infer recovery from prose.
- Only `prompt` is required. Everything else has a defensible default, so a minimal call succeeds.
- Responses echo **effective** values: the seed actually used, the dimensions actually rendered, the model version. An agent never has to infer what happened, and a retry with the echoed seed is deterministic.

This is the entire prerequisite for the MCP facade: if the async API is agent-callable, MCP is a schema wrapper.
