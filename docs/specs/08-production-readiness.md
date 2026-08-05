# 08 — Production readiness

An honest account of what this is not. The engineering is production-*shaped*; it is not production-*ready*, and the gap is enumerated here rather than left for a reviewer to find.

## Built

| Concern | Implementation |
|---|---|
| Authentication | API key, hashed at startup, `hmac.compare_digest`, `api_key_id` attributed on every job and log line |
| Idempotency | `(api_key_id, key)` unique constraint, request-hash conflict detection, race-tested with concurrent inserts |
| Upstream resilience | Retry with jittered backoff, circuit breaker, timeouts on every call |
| Load shedding | `429 QUEUE_SATURATED` on estimated queue wait, from cached endpoint health — [02](02-gateway-core.md#queue-pressure) |
| Health | `/health` unauthenticated liveness, `/health/detailed` authenticated dependency status |
| Content safety | Prompt guardrail at both tiers, image hook registered, fail-closed — [04](04-guardrails.md) |
| Correlation | One ID from HTTP through the GPU and back, plus `runpod_job_id` for joining to RunPod's own view |
| Progress | Per-step `progress_update` from the worker, stored and served — [01](01-worker.md#progress-reporting) |
| Queue-depth visibility | `endpoint_health` logged every 2s — a log-based time series of depth and worker counts |
| Reproducibility | Seed always echoed, model revision pinned in `contracts/`, `model_version` on every result |
| Credential containment | Storage keys never leave the server; the gateway proxies image bytes |
| Deploy safety | Immutable `{version}-{sha}-{variant}` tags, documented rollback, `latest` never deployed |
| Clean shutdown | Reconciler task cancelled and awaited under the lifespan hook, so an in-flight tick completes |

## Not built

Ranked by what would hurt first in a real deployment.

| # | Gap | Consequence | Cost to close |
|---|---|---|---|
| 1 | **No per-caller rate limit or quota** | One caller can exhaust the budget. Auth identifies them; nothing bounds them. Queue shedding protects latency, not spend | Low — a counter table and middleware |
| 2 | **No budget cap or spend alerting** | Cost overrun is discovered on the invoice | Low — RunPod API polling plus a threshold |
| 3 | **No metrics export** | `endpoint_health` gives a log-based time series, but there is no scrape endpoint, no dashboard, no alerting, no SLOs | Low — `prometheus-client` and a `/metrics` route |
| 4 | **Nothing restricts direct endpoint access** | Anyone holding the RunPod API key bypasses the gateway entirely — no auth, no idempotency, no attribution. The duplicated worker-side guardrail exists precisely because this hole cannot be closed from our side | Not closable. Mitigated by key hygiene |
| 5 | **No image retention or deletion** | Images accumulate on the network volume indefinitely, billed per GB per month. A deletion request cannot be honoured, and neither can a takedown | Medium — lifecycle job plus a deletion route |
| 6 | **No distributed tracing** | The correlation ID is a hand-rolled substitute. No spans, no latency breakdown between queue wait and inference | Medium — OpenTelemetry across both tiers |
| 7 | **Circuit breaker state is per-process** | Correct for one instance, wrong for a fleet — each replica learns the outage separately | Medium — shared state in Redis |
| 8 | **No audit trail** | Abuse investigation and takedown rest on raw logs. `flag` verdicts are recorded but nothing consumes them | Medium — append-only audit table and a review queue |
| 9 | **`AVG_JOB_SECONDS` is a constant** | The queue-wait estimate driving the `429` does not adapt to resolution, step count, or GPU. A 50-step 1536² job is estimated the same as a 20-step 512² one | Low — rolling p50 from completed jobs |
| 10 | **Gateway proxies image bytes** | Every image traverses the gateway. Fine at this scale; a bandwidth bottleneck at any real one | Low — presigned URLs, one route, no callers affected |
| 11 | **No key rotation** | A leaked key is revoked by editing settings and restarting | Low — key versioning |
| 12 | **No load test or capacity model** | Concurrency limits and the queue threshold are reasoned, not measured | Medium — a load harness and a run |
| 13 | **No image provenance** | Output is not attributable as machine-generated | Medium — C2PA or invisible watermark |
| 14 | **No cancellation** | A queued job cannot be stopped, and it will still be billed. RunPod supports `POST /cancel`; we expose no route | Low — a route and one adapter call |
| 15 | **Region is pinned twice, for different reasons** | The S3 API exists in five datacenters and the volume variant pins its own. Together they narrow the GPU pool — exactly when scaling up | Medium — regional storage, or accept |
| 16 | **Single region** | A RunPod region outage is a total outage | High |
| 17 | **No DB backup, restore drill, or SLOs** | Recovery is untested | High — process, not code |

**#1 and #2 remain the pair that matter most.** Authentication answers *who*; nothing answers *how much*. Queue shedding bounds latency, not spend — a single authenticated caller submitting steadily inside the queue threshold can still run up an unbounded bill. Both are cheap to close and conspicuous to leave open.

**#4 is the one that cannot be fixed here.** The serverless endpoint is a public URL with its own credential. Everything the gateway enforces — auth, idempotency, quota, attribution — is bypassed by calling RunPod directly. That is why the prompt guardrail is duplicated into the worker ([04](04-guardrails.md)), and it is the honest limit of a design where the gateway is not the only door.

## Known limitations

Smaller than gaps, but real, and better stated than discovered.

| Limitation | Detail |
|---|---|
| **Prompt truncation is possible** | The 2000-character cap is a proxy for T5's 512-**token** limit. A dense prompt inside the cap can still exceed 512 tokens, and `diffusers` truncates silently. Validating exactly means loading the tokenizer — a dependency to inject and fake for a rare case. See [01](01-worker.md#prompt-length) |
| **The blocklist is not a classifier** | It stops naive cases. It does not understand intent, euphemism, or context, and it never will. Real safety is the classifier hook in [04](04-guardrails.md), unbuilt |
| **`flag` has no consumer** | Recorded and counted, but no review queue acts on it. Today it is `allow` plus an audit line |
| **Negative prompts unsupported** | Deliberate — real CFG doubles cost, adds a guidance control that fights the distilled embedding, and does not reliably improve output. Measured in [09](09-benchmarks.md) rather than asserted |
| **Progress resolution is tick-bound** | Bounded by the reconciler's 2s tick, not by the step rate. ~10 updates across a 22s generation |
| **Queue pressure fails open** | A stale health cache admits traffic. Deliberate: load shedding is an optimisation, and refusing everything because we cannot measure the queue converts a monitoring failure into an outage |

## Deliberate non-goals

Not gaps. Choices, with reasons.

| Choice | Why |
|---|---|
| No synchronous endpoint | Bounds concurrency by held connections rather than GPU capacity, and cannot survive a cold start — [03](03-facades.md) |
| No `torch.compile` | 3-10 min compile on every cold start. Net slower for serverless traffic; revisit only for dedicated pods with sustained load |
| No ComfyUI | Subprocess plus workflow-JSON routing between request and model, for flexibility a fixed-model API does not need |
| `concurrency_modifier = 1` | The worker is GPU-bound. A second concurrent job causes VRAM contention, not throughput |
| No batch inference | Serverless is one job, one image. Batching needs a different execution model |
| No preview-image streaming | `pipe()` blocks and the callback cannot yield, so it needs a thread, a queue, and a VAE decode per preview — [01](01-worker.md) |
| Webhook-out, SSE, WebSocket, MCP documented not built | Each imposes real cost on the *caller* — [03](03-facades.md). The protocol boundary keeps them cheap to add |
| Config-as-code for endpoints | Two endpoints created once do not justify it; values recorded in the runbook instead — [06](06-build-deploy.md#endpoint-creation) |
| Gateway not hosted | A credential-backed GPU spender exposed continuously with no quota. #1 above would need closing first |
| No fine-tuning or LoRA | Out of scope. This is a deployment and inference exercise |

## Licence

FLUX.1-dev is released under a non-commercial licence. Fine for evaluation; a commercial deployment needs FLUX.1-schnell (Apache-2.0) or a commercial licence from Black Forest Labs. Noted for completeness, not a constraint on this exercise.

## If this went to production

In order:

1. **Rate limits and quotas, then budget caps and spend alerts.** Bound the blast radius before anything else. Everything below is worthless if a single caller can drain the account first.
2. **Metrics export and alerting** on error rate, p95 latency, queue depth, and block rate. The log-based time series is a stopgap; you cannot alert on it.
3. **Retention and deletion**, before stored images accumulate enough to make the problem expensive and a deletion request impossible to honour.
4. **Load test**, to replace the reasoned concurrency and queue thresholds with measured ones — and to make `AVG_JOB_SECONDS` adaptive.
5. **Shared breaker state and a separate reconciler deployment**, the moment there is more than one replica.
6. **Presigned URLs**, once image bandwidth through the gateway becomes visible.
7. **Tracing**, at the point correlation IDs stop being sufficient — typically when a third service appears.

Multi-region last. It is the most expensive and the least likely to be the binding constraint.
