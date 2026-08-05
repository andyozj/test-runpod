# 03 — Facades

Transports over one `JobService`. The argument of this document is that a transport choice is a **cost imposed on the caller**, not an implementation detail — so each option is described by what it forces the consumer to do.

## Built

### Async — the primary interface

```
POST /v1/jobs        → 202 {"job_id": "...", "status": "QUEUED"}
GET  /v1/jobs/{id}   → 200 {"status": "COMPLETED", "result": {...}}
```

**Consumer cost:** two calls plus a polling loop, and a decision about poll interval.

**Consumer benefit:** retry-safe, survives client restarts, no held connection, and the job survives the client crashing. For a 20-25s operation this is the honest interface.

Poll interval guidance is published in the API docs: 2s is reasonable, backing off after 60s. Undocumented polling guidance produces either hammering or sluggish clients.

### Sync — demo-grade wrapper

```
POST /v1/images      → 200 {image}  |  202 {"job_id": "..."}
```

Submits and polls internally until terminal, bounded by a server-side deadline set below the typical client timeout. On expiry it returns `202` with the `job_id` rather than hanging.

**Consumer cost:** holds a connection for ~20-25s. Most load balancers and API gateways idle-timeout between 30 and 60s, so this is close to the edge before anything goes wrong. A retry is not safe — it re-bills a fresh generation.

**Two response shapes on one endpoint is a wart.** It is tolerable only because sync is explicitly the convenience path: the `202` is the async interface showing through, and the documentation says so. The alternative — a hard `504` — is cleaner to type but discards a job that is still running and will still be billed.

Documented as demo-grade. Not the interface to build a client against.

## Documented, not built

The protocol boundary in [02](02-gateway-core.md) means each is an addition rather than a rewrite. Cost below is what the *consumer* pays.

| Facade | Consumer cost | Consumer benefit | Why not built |
|---|---|---|---|
| **Webhook out** | Must run a public HTTPS endpoint, verify signatures, tolerate duplicate and out-of-order delivery, and *still* poll as a fallback. Impossible for browser clients | No polling cost, lowest notification latency | Callback field plumbed, route unbuilt. Delivery is best-effort — RunPod retries twice at 10s intervals then stops — so the poller is required regardless |
| **SSE** | One long-lived connection per job. Intermediate proxies buffer and break it. Unidirectional. Needs reconnect handling via `Last-Event-ID` | Per-step progress, much better perceived latency | Requires `progress_update` from the worker — real GPU-side work on both tiers |
| **WebSocket** | Connection state management, reconnect logic, sticky sessions through any load balancer | Bidirectional; enables mid-generation cancel | Infrastructure weight disproportionate to the benefit here |
| **MCP** | None — the client is an agent framework and the tool schema is the contract | Agent-native call with zero glue | A thin wrapper over async. Cheap to add; out of scope |
| **gRPC** | Codegen and a toolchain | Efficient streaming, typed contract | No consumer asking for it |

## Why the result shape decides which facades are possible

The worker returns a storage **reference**, not image bytes ([01](01-worker.md)). That single choice is what keeps the table above open.

A ~200-byte result can be pushed: it fits in an SSE event, a webhook body, a WebSocket frame, an MCP tool result. A 2.7MB base64 blob fits in none of them comfortably, and a client polling every 2s would re-download it on every call until the job finished.

So the decision about which transports are *available* is made in the worker's serialisation, not in the API layer — and it is made once, early, in the place least visible to whoever later wants to add streaming. Returning bytes would foreclose event-driven delivery entirely while looking like a local choice about response encoding.

## Why the boundary is what makes this cheap

Each facade above translates HTTP-shaped input into a `JobService` call and translates the result back. None of them touch persistence, RunPod, or the domain rules.

That holds only while `core/` stays free of transport concerns. One `HTTPException` raised from `JobService` and every other facade inherits an HTTP dependency it has no use for. This is why the rule is enforced by `import-linter` rather than by intention — see [02](02-gateway-core.md#layering).

## Agent-callable design

Callers include automated agents, which changes three things:

- Errors carry a stable machine-readable `code` and a `suggestion` naming the next valid action. An agent cannot infer recovery from prose.
- Only `prompt` is required. Everything else has a defensible default, so a minimal call succeeds.
- Responses echo **effective** values — the seed actually used, the dimensions actually rendered, the model version. An agent never has to infer what happened, and a retry with the echoed seed is deterministic.

This is also the entire prerequisite for the MCP facade: if the async API is agent-callable, MCP is a schema wrapper.
