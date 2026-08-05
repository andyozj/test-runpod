# 08 — Production readiness

An honest account of what this is not. The engineering is production-*shaped*; it is not production-*ready*, and the gap is enumerated here rather than left for a reviewer to find.

> **Status: this document describes the intended delivered state, and no code exists yet.**
>
> It is the document most easily made false — every row in *Built* is a commitment, and a reviewer will check the ones that sound impressive. Reconciling it against the shipped code is a required step before submission, not a courtesy. Anything that does not ship moves to *Not built* with the reason, and a spec that overstates what was delivered destroys the credibility of the parts that are accurate.

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
| Brief compliance | Worker returns base64 by default, so `GET /status/{job_id}` alone yields an image. Storage references are opt-in — [01](01-worker.md) |
| Cancellation | `POST /v1/jobs/{id}/cancel` delegating to RunPod's own cancel — the platform owns the queue, so only it can stop the work and the billing |
| Deploy safety | Immutable `{version}-{sha}-{variant}` tags, one-input rollback workflow, `latest` never deployed |
| Secret management | RunPod secrets manager, referenced as `{{ RUNPOD_SECRET_* }}`. No plaintext credential in committed config or image — [06](06-build-deploy.md#secrets) |
| Config as code | Endpoints declared in `deploy/endpoints/*.yaml`, applied through the REST API as a template upsert then an endpoint upsert. Reviewable in a diff, reconstructible after deletion |
| Deploy automation | Manually-triggered workflow taking a tag; rollback is the same workflow with the previous tag |
| Clean shutdown | Reconciler task cancelled and awaited under the lifespan hook, so an in-flight tick completes |
| Prompt retention for audit | Prompts stored on the job row deliberately, so a generation can be traced to what was asked for |

## Not built

Ranked by what would hurt first in a real deployment.

| # | Gap | Consequence | Cost to close |
|---|---|---|---|
| 1 | **No per-caller rate limit or quota** | One caller can exhaust the budget. Auth identifies them; nothing bounds them. Queue shedding protects latency, not spend | Low — a counter table and middleware |
| 2 | **No budget cap or spend alerting** | Cost overrun is discovered on the invoice | Low — RunPod API polling plus a threshold |
| 3 | **No metrics export** | `endpoint_health` gives a log-based time series, but there is no scrape endpoint, no dashboard, no alerting, no SLOs | Low — `prometheus-client` and a `/metrics` route |
| 4 | **The endpoint is a second door with one shared key** | The serverless endpoint is callable with the RunPod API key, which is account-scoped, identical for every holder, and can also create and delete resources. Anyone given it bypasses the gateway — no per-caller auth, no idempotency, no attribution — and there is no way to issue a narrower credential | Not closable from our side. Mitigated by key hygiene and by duplicating the guardrail into the worker |
| 5 | **No retention or deletion for prompts** | Prompts accumulate in `jobs.request`. Both are retained deliberately for audit, but with no expiry and no deletion route a takedown or erasure request cannot be honoured | Medium — lifecycle job plus a deletion route |
| 6 | **No distributed tracing** | The correlation ID is a hand-rolled substitute. No spans, no latency breakdown between queue wait and inference | Medium — OpenTelemetry across both tiers |
| 7 | **Circuit breaker state is per-process** | Correct for one instance, wrong for a fleet — each replica learns the outage separately | Medium — shared state in Redis |
| 8 | **No audit trail** | Abuse investigation and takedown rest on raw logs. `flag` verdicts are recorded but nothing consumes them | Medium — append-only audit table and a review queue |
| 9 | **`AVG_JOB_SECONDS` is a constant** | The queue-wait estimate driving the `429` does not adapt to resolution, step count, or GPU. A 50-step 1536² job is estimated the same as a 20-step 512² one | Low — rolling p50 from completed jobs |
| 10 | **No object storage** | The image is returned inline: ~2.7MB per result, re-sent on every poll of a completed job, and unusable in any pushed transport. Correct for a directly-callable endpoint, wrong at scale — where an upload plus presigned URLs is the answer. Built and removed once as inert code; the right time to add it is when a caller needs a pushed result | Medium — upload path, a proxy or presign route, retention |
| 11 | **No gateway key rotation** | A leaked gateway key is revoked by editing settings and restarting. The RunPod account key has no rotation story at all | Low — key versioning |
| 12 | **No load test or capacity model** | Concurrency limits and the queue threshold are reasoned, not measured | Medium — a load harness and a run |
| 13 | **No image provenance** | Output is not attributable as machine-generated | Medium — C2PA or invisible watermark |
| 14 | **No cancellation** | A queued job cannot be stopped, and it will still be billed. RunPod supports `POST /cancel`; we expose no route | Low — a route and one adapter call |
| 15 | **Worker image build is outside CI** | ~45GB against ~14GB of runner disk. Build and push happen on a Pod by runbook; only deploy is automated | Medium — a self-hosted runner on a Pod |
| 17 | **Single region** | A RunPod region outage is a total outage | High |
| 18 | **No DB backup, restore drill, or SLOs** | Recovery is untested | High — process, not code |

**#1 and #2 remain the pair that matter most.** Authentication answers *who*; nothing answers *how much*. Queue shedding bounds latency, not spend — a single authenticated caller submitting steadily inside the queue threshold can still run up an unbounded bill. Both are cheap to close and conspicuous to leave open.

**#4 is the one that cannot be fixed from here**, and it is worth stating precisely rather than dramatically. The endpoint is not open to the world — it requires the RunPod API key. The problem is the *shape* of that key: account-scoped, identical for everyone who holds it, capable of creating and deleting resources, and impossible to narrow to "may submit jobs to this one endpoint". So there is no way to let something call the endpoint directly without also handing it the account.

That is why the prompt guardrail is duplicated into the worker ([04](04-guardrails.md)): the gateway cannot be assumed to be the only door, so the safety check cannot live only there.

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
| Gateway not hosted | A credential-backed GPU spender exposed continuously with no quota. #1 above would need closing first |
| Prompts retained indefinitely | Deliberate: a generated image must be traceable to what was asked for, which is the basis of any abuse investigation. Retention *policy* is the gap (#5), not retention itself |
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
