# 08 — Production readiness

An honest account of what this is not. The engineering is production-*shaped*; it is not production-*ready*, and the gap is enumerated here rather than left for a reviewer to find.

## Built

| Concern | Implementation |
|---|---|
| Authentication | API key, hashed at rest, constant-time comparison, attributed on every job and log line |
| Upstream resilience | Retry with jittered backoff, circuit breaker, timeouts on every call |
| Health | `/health` liveness, `/health/detailed` dependency checks |
| Content safety | Prompt guardrail at both tiers, image hook registered — [04](04-guardrails.md) |
| Idempotency | Unique constraint, race-tested |
| Correlation | One ID from HTTP through the GPU and back — [05](05-observability.md) |
| Reproducibility | Seed always echoed; `model_version` on every result |
| Deploy safety | Immutable tags, documented rollback, `latest` never deployed |

## Not built

Ranked by what would hurt first in a real deployment.

| # | Gap | Consequence | Cost to close |
|---|---|---|---|
| 1 | **No per-caller rate limit or quota** | One caller can exhaust the budget. Auth identifies them; nothing bounds them | Low — a counter table and middleware |
| 2 | **No budget cap or spend alerting** | Cost overrun is discovered on the invoice | Low — RunPod API polling plus a threshold |
| 3 | **No metrics export** | Logs only. No dashboards, no alerting, no SLOs | Low — `prometheus-client` and a scrape endpoint |
| 4 | **No distributed tracing** | The correlation ID is a hand-rolled substitute. No spans, no latency breakdown | Medium — OpenTelemetry across both tiers |
| 5 | **Circuit breaker state is per-process** | Correct for one instance, wrong for a fleet — each replica learns the outage separately | Medium — shared state in Redis |
| 6 | **No data retention policy** | Prompts and images accumulate indefinitely. A deletion request cannot be honoured | Medium — retention job, deletion endpoint |
| 7 | **No audit trail** | Abuse investigation and takedown are not supported beyond raw logs | Medium — append-only audit table |
| 8 | **No graceful shutdown** | In-flight reconciler work is lost on deploy; jobs stay unresolved until the next poll | Low — lifespan hook and drain |
| 9 | **No key rotation** | A leaked key is revoked by hand | Low — key versioning |
| 10 | **No load test or capacity model** | Concurrency limits and queue thresholds are guesses | Medium — a load harness and a run |
| 11 | **No image provenance** | Output is not attributable as machine-generated | Medium — C2PA or invisible watermark |
| 12 | **Single region** | RunPod region outage is total outage | High |
| 13 | **No DB backup, restore drill, or SLOs** | Recovery is untested | High — process, not code |

**#1 and #2 are the pair that matter most.** Authentication answers *who*, and nothing answers *how much*. An authenticated caller with no quota is a known party running up an unbounded bill — cheap to close and conspicuous to leave open.

## Deliberate non-goals

Not gaps. Choices, with reasons.

| Choice | Why |
|---|---|
| No `torch.compile` | 3-10 min compile on every cold start. Net slower for serverless traffic; revisit only for dedicated pods with sustained load |
| No ComfyUI | Subprocess plus workflow-JSON routing between the request and the model, for flexibility a fixed-model API does not need |
| `concurrency_modifier = 1` | The worker is GPU-bound. A second concurrent job causes VRAM contention, not throughput |
| No batch inference | Serverless is one job, one image. Batching needs a different execution model |
| Webhook-out, SSE, WebSocket, MCP documented not built | Each imposes real cost on the *caller* — see [03](03-facades.md). The protocol boundary keeps them cheap to add |
| No fine-tuning or LoRA | Out of scope. This is a deployment and inference exercise |

## Licence

FLUX.1-dev is released under a non-commercial licence. Fine for evaluation; a commercial deployment needs FLUX.1-schnell (Apache-2.0) or a commercial licence from Black Forest Labs. Noted for completeness, not a constraint on this exercise.

## If this went to production

In order:

1. Rate limits and quotas, then budget caps and spend alerts — bound the blast radius before anything else.
2. Metrics export and alerting on error rate, p95 latency, queue depth, and block rate. You cannot operate what you cannot see.
3. Retention and deletion, before user data accumulates enough to make the problem expensive.
4. Load test to replace the guessed concurrency and queue-depth thresholds with measured ones.
5. Shared circuit-breaker state and graceful shutdown, once there is more than one replica.
6. Tracing, which is the point at which correlation IDs stop being sufficient.

Multi-region last. It is the most expensive and the least likely to be the binding constraint.
