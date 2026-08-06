# Design decisions and trade-offs

**Date:** 2026-08-07 · **Evidence:** [`BENCHMARKS.md`](../BENCHMARKS.md), [`RUNBOOK.md`](RUNBOOK.md), the code

The design record: what was chosen, what was rejected, and what each choice knowingly gave up. Each section carries its reasoning here and links the measurement or code path that tested it. Numbers are quoted from `BENCHMARKS.md` (measured 2026-08-06, N=3-10 per cell) or from the code.

| # | Decision | Choice | What it cost |
|---|---|---|---|
| 1 | Weight delivery | RunPod cached models; baked image a build target, unpublished; volume dropped | Cold start depends on platform staging (p50 89.9s, max 518s); one console-only config field |
| 2 | Base image | `ubuntu:22.04` + pip torch, not `nvidia/cuda` | No CUDA toolchain in the image; GPU-injection env vars set by hand |
| 3 | Model revision | Discovered from the staged snapshot; the pin was deleted | Weights can drift with the cache; bounded by reporting and refusal |
| 4 | Tier split | Worker graded; gateway a fenced spike; contract duplicated, conformance-tested | Five contract files, tests in both suites |
| 5 | Interface | Async-only: submit, poll | A bare `curl` is two commands |
| 6 | Result | Inline base64, no object storage | ~2.7MB re-sent on every poll of a completed job |
| 7 | Gateway architecture | Ports-and-adapters over an in-memory repository | Jobs lost on restart; three invariants left to the Postgres port |
| 8 | Load shedding | 429 on estimated wait, not queue depth; fails open | Estimate is a lower bound; a dead reconciler admits everything |
| 9 | Idempotency | Insert-first; replay by row identity; key released on shed | A request hash per row; ordering constraints in `submit` |
| 10 | Completion | Reconciler polling, lease + derived submit grace | Up to one 2s tick of phantom latency; N replicas = N pollers |
| 11 | Guardrails | Same blocklist both tiers, fail-closed; crash ≠ block | The worker's check runs on billed GPU time |
| 12 | Negative prompts | Not exposed | A schema change to add later |
| 13 | GPU and defaults | Chosen by $/image, not $/hr | 28-step default kept despite 20 steps being 28% cheaper |
| 14 | CI/CD | Tag-driven versioning; checks gate everything; deploys human-only | Post-deploy smoke test detects, does not prevent; rollback is manual |
| 15 | Observability | `structlog` JSON to stdout; one correlation ID; log-based health series | No metrics export, no tracing |
| 16 | Testing | GPU-free unit/integration; contract conformance; recording fakes | Platform divergence caught only by e2e |

## 1. Weight delivery: cached models, not baked, not a volume

**Context.** The brief asks for a Docker image containing handler and model. FLUX.1-dev is ~33GB in the diffusers layout; where those bytes live decides image size, cold start, and region constraints.

**Options.** Baked into the image (~45GB); a network volume (per-GB monthly bill, datacenter pin); RunPod cached models (platform pre-stages the HF repo on hosts, unbilled).

**Choice.** The deployed endpoint uses cached models with a 2.92GB slim image. The baked image stays a one-command build target (`make build-baked`) because the brief names it — buildable, not published: pushing 45GB to GHCR proves nothing the build target does not. The volume variant was tried and dropped, never populated or deployed: its cost is a datacenter pin that narrows the GPU pool exactly when scaling up under load, plus storage billing and a population step. It is not the fallback either — a volume is only a fallback once populated, and populating one costs everything removing it avoided. The fallback is the baked image, buildable before an incident rather than during one.

**Trade-off accepted.** True cold start includes whole-repo staging on a fresh host: p50 89.9s, max 518.1s when staging lands cold. FlashBoot resume is 16.0s, warm 0.1s — the platform is trusted for steady traffic, minutes are budgeted for bursts from zero. The endpoint's Model field is console-only (no REST surface, verified against its OpenAPI 2026-08-06): one manual step in an otherwise scripted deploy.

**Evidence.** [`BENCHMARKS.md`](../BENCHMARKS.md#cold-start-decomposed) cold-start table; [`RUNBOOK.md`](RUNBOOK.md) build and deploy procedures for both variants. `weights.resolve()` (`worker/src/worker/weights.py`) keeps all mechanisms live behind one code path: an explicit `WEIGHTS_PATH` wins, else the model cache. A deployment selects a mechanism by configuration alone.

## 2. Base image: `ubuntu:22.04`, not a CUDA base

**Context.** GPU images conventionally start from `nvidia/cuda:*-runtime`.

**Choice.** `ubuntu:22.04` with torch installed from cu130 wheels. The wheel vendors its own CUDA libraries and the driver arrives from the host runtime, so the CUDA base duplicated ~6.6GB nothing ever loads: 11.9GB → 2.92GB (measured: 2,916,120,790 bytes, 13 layers). Python comes from `uv`, not apt — Ubuntu 22.04's `python3.11` package is frozen at 3.11.0rc1, which predates `sys.get_int_max_str_digits` and crashes `torch._dynamo` on import.

**Trade-off accepted.** No preinstalled CUDA toolchain, and the two env vars the CUDA base used to provide (`NVIDIA_VISIBLE_DEVICES`, `NVIDIA_DRIVER_CAPABILITIES`) are set by hand. Safe because the worker compiles nothing at runtime; the wheels carry every library it loads. The wheel/driver pairing is enforced at scheduling instead: `allowed_cuda_versions` in the endpoint config makes a mismatch fail before a worker bills, not at model load.

**Evidence.** `worker/Dockerfile` (header comments carry the reasoning and the layer order: weights below application code, so a code change rebuilds one layer).

## 3. Model revision: discovered, not pinned

**Context.** The original design pinned a commit SHA in `contracts/model-revision.txt` and refused to start on a mismatch.

**Choice.** The file is deleted. Cached models stage from the console's Model field, which has no revision control: a pin the platform cannot honour only ever produces a startup refusal. `worker/weights.py` discovers the loaded revision instead — the cache snapshot directory name is the commit SHA; a baked build writes a `MANIFEST.json` (`worker/scripts/fetch_weights.py`, run only under `BAKE_WEIGHTS=true`) for the same purpose — and reports `model_version` as `<model_id>@<revision>` on every response.

**Trade-off accepted.** The staged weights can drift from what was benchmarked. Three bounds: every result names the revision it was generated with, so drift is visible per-image rather than silent; a `sha256` comparison of weight files across variants is specified future work, unwritten while one variant is deployed (the e2e suite is 7 cases); and the resolver refuses to start when several snapshots coexist with no `refs/main` naming the staged one — RunPod's own example sorts and takes the first, which would run a model the response then misattributes.

**Evidence.** `worker/src/worker/weights.py:resolve()`; `worker/scripts/fetch_weights.py` (`MANIFEST.json`); `worker/tests/e2e/test_endpoint.py` (7 cases, no weight-variant hash case).

## 4. Two tiers, one graded deliverable

**Context.** The worker alone satisfies the brief. The gateway (auth, idempotency, reconciler, load shedding) is beyond it.

**Choice.** The gateway is built as a fenced spike: local `docker compose` only, never hosted. Hosting adds nothing demonstrable — the endpoint is a public URL regardless — and adds a live liability: a credential-backed GPU spender exposed continuously with no per-key quota (the top-ranked gap, see [`SECURITY.md`](../SECURITY.md) §Known and accepted). Package isolation is enforced by construction: separate virtualenvs, so torch can never reach the gateway nor FastAPI the worker.

**Trade-off accepted.** Isolation forbids a shared package, so the contract exists twice. Options weighed: a third package (a third `pyproject.toml`, lockfile, and publish step for ~10 fields), relaxing isolation (loses the guarantee keeping both images honest), or duplicating with drift made detectable. Duplication won. Five files at the root — `contracts/generation-request.schema.json`, `error-codes.json`, `blocklist.json`, `normalisation.json`, `guardrail-corpus.json` — are the source of truth; both suites assert conformance, so changing a field fails both packages until both follow. The behaviour a shared package would have bought, without shipping a third package.

**Evidence.** `contracts/`; conformance tests in `worker/tests/unit/test_contracts.py` and `gateway/tests/unit/test_contracts.py`.

## 5. Async-only interface

**Context.** Generation takes ~20-25s warm; a cold worker adds 30-60s of pipeline load.

**Choice.** `POST /v1/jobs` → `job_id`, `GET /v1/jobs/{id}` → result. No synchronous endpoint: it bounds concurrency by held connections rather than GPU capacity, and any deadline safe against load-balancer idle timeouts (30-60s) is exceeded by the first post-idle request — the convenience path would be the one most likely to look broken on a reviewer's opening call. The demo still uses RunPod's own `/runsync` for the one-command case, against a deliberately warm worker.

**Trade-off accepted.** A bare `curl` is two commands. `client/generate.py` covers the ergonomics.

**Evidence.** `gateway/src/gateway/api/` routes; `client/generate.py`; [`gateway/README.md`](../gateway/README.md) endpoint list.

## 6. The result is the image, not a reference

**Context.** An earlier revision returned a storage key: ~200 bytes, pushable over SSE or a webhook where a 2.7MB blob is not.

**Choice.** Reverted to inline base64. RunPod's surface is fixed — `GET /status/{job_id}` returns the handler's output verbatim, and there is nowhere else a direct caller can fetch from — so a storage key hands the reviewer a reference they cannot resolve without running the gateway. The graded tier must not depend on the ungraded one.

**Trade-off accepted.** ~2.7MB per completed-job poll, and no pushed transport can carry the result. Measured at the worst case: 1536² PNG is 2.85MB base64, JPEG 0.49MB (5.8x smaller), both inside the platform caps. Object storage is an open gap — built once, removed as inert, to be re-added when a caller needs a pushed result.

**Evidence.** [`BENCHMARKS.md`](../BENCHMARKS.md) response-payload table; `worker/src/worker/handler.py`.

## 7. Ports-and-adapters, in-memory persistence

**Context.** The gateway needs a job store; Postgres is the specified production store, unimplemented.

**Choice.** `core/` declares `Protocol` interfaces and imports nothing outward — enforced by `import-linter` in `make check`, not review. Persistence is `InMemoryJobRepository` behind the same protocol: a compose file starting a database nothing needs would be scenery. The swap is one binding in the composition root (`gateway/src/gateway/main.py`).

**Trade-off accepted.** Jobs do not survive a restart, and three invariants the in-memory adapter gets from a single `asyncio.Lock` become the Postgres port's obligations, documented at their sites:

- **Atomic idempotent insert** — insert first, catch the unique violation on `(api_key_id, idempotency_key)`, return the existing row. Check-then-insert has exactly the race idempotency exists to prevent.
- **Claim leasing** — `claim_unresolved` takes `lease_s` and models `FOR UPDATE SKIP LOCKED`: a claimed job is invisible to other callers until released or expired. Redundant with one reconciler; the difference between polling once and racing on the write with two.
- **Race-safe cap counting** — `JobService._check_active_job_cap` is correct today only because the in-memory `count_active` never awaits, so nothing interleaves between insert and count. A database `count_active` is real I/O and reopens the check-then-act race unless the repository enforces the cap itself (atomic insert-and-count or a per-key row lock). The docstring says so, so the port inherits a requirement, not a surprise.

**Evidence.** [`STANDARDS.md`](../STANDARDS.md) §3; `gateway/src/gateway/adapters/memory.py` (lease and lock docstrings); `gateway/src/gateway/core/service.py` (cap race note).

## 8. Shed on estimated wait, not queue depth

**Context.** RunPod autoscales on queue delay below `max_workers`; above it, nothing more is coming and queued jobs sit until they time out.

**Choice.** `estimated_wait = (inQueue / capacity) × AVG_JOB_S`; over 120s, `submit` fails with `429 QUEUE_SATURATED` and `Retry-After = ceil(wait × uniform(0.8, 1.2))`. A raw depth threshold is meaningless on its own — 20 queued jobs are comfortable against 50 workers and hopeless against 3 — and a time threshold survives a `max_workers` change. The jitter prevents every shed client returning at the same instant and converting one spike into a synchronised second one. Health is refreshed on the reconciler's existing 2s tick and cached, not fetched per request: zero hot-path latency, zero extra upstream calls. On a missing or stale reading (>30s), submissions pass — the one deliberate fail-open, because load shedding is an optimisation and refusing all traffic for an unmeasurable queue converts a monitoring failure into an outage.

**Trade-off accepted.** The estimate counts a running worker as available capacity, so it is a lower bound and real waits skew longer. `AVG_JOB_S` is a constant fed by the measured p50 (21.8s at 1024²/28 steps; shipped default 22.0), not adaptive per configuration: a 50-step 1536² job is estimated the same as a 20-step 512² one.

**Evidence.** `service.py:_check_queue_pressure`, `_retry_after_s`; [`BENCHMARKS.md`](../BENCHMARKS.md#named-outputs) named outputs. The queue itself was measured under a 4x burst: 12 jobs against 3 workers drained linearly at ~11.6s per position, no failures — shedding guards the cost ceiling, not latency.

## 9. Idempotency: identity, ordering, release

**Context.** A retry after a network timeout on a 20-25s GPU operation bills twice without protection.

**Choice.** Stripe-convention `Idempotency-Key`, unique on `(api_key_id, key)` — scoped to the key alone, two callers picking the same value collide and one receives the other's image. Three refinements each bought a specific retry guarantee:

- **Replay by row identity, not status.** `submit` detects a replay by `stored.id != job.id`. A replay can land on a `QUEUED` row whose own submit has not returned yet; status alone cannot tell the two apart, and submitting again there is a second billed GPU job.
- **Replay resolves before shedding.** The pressure check runs after the insert, so a caller retrying under load gets its original `job_id` back rather than a 429 it can never recover the id from. A replay costs nothing upstream; shedding it protects nothing.
- **A shed request releases its key.** The shed row is written `FAILED`; a key left bound to it would replay that failure for the whole retention window, and the retry the `Retry-After` asks for could never become a real attempt. Nothing was submitted upstream, so there is no duplicate work to protect against. Every other terminal state keeps its binding.

**Trade-off accepted.** A `request_hash` per row (a key reused with a different body is `409 IDEMPOTENCY_CONFLICT` — silently returning the first job hands the caller an image of something they did not ask for), and ordering constraints in `submit` that the docstrings must carry.

**Evidence.** `service.py:submit`, `_shed_if_over_capacity`; the race is tested with genuinely concurrent inserts — a sequential test passes against the broken implementation.

## 10. A reconciler, not webhooks and not per-request polling

**Context.** Nothing announces completion. RunPod deletes `/run` results 30 minutes after completion and has no notion of which caller a job belonged to; durability past that window and attribution are the whole justification for a job store.

**Options.** Webhooks (delivery is best-effort — two retries at 10s, then nothing — so a poller is required regardless); ask RunPod live inside `GET /v1/jobs/{id}` (never learns of completion unless a client is polling: no record, no attribution, nothing to ever push; upstream calls scale with client poll rate, not job count); a background reconciler.

**Choice.** An asyncio-task reconciler in the FastAPI lifespan: 2s tick, 10s idle, batch 50, ±20% jitter. Unknown is not failure — an unreachable upstream leaves the job untouched for the next tick, because writing `FAILED` on our own inability to ask would discard a good result over a blip. Terminal states only advance. The double-submit race is closed structurally: `submit` inserts the row before calling RunPod, so a crash between the two leaves a job with no upstream id; the reconciler adopts and resubmits it only after a submit grace derived as `max(configured, submit_envelope_s(3 attempts, 30s timeout)) = 90.6s` — the worst case a live submit can still occupy, retries and backoff included. A grace configured below the client's own retry envelope reopens the race silently, so the floor is computed from the client, not trusted to configuration. Claims carry a 60s lease released per job as the tick proceeds; the lease exists to survive a reconciler that died mid-tick, not to slow a live one.

**Trade-off accepted.** Up to one tick (2s) of phantom latency between completion and the gateway noticing, and progress resolution bounded at ~10 updates per 22s generation. Today N gateway replicas would mean N reconcilers polling in parallel; a second instance is safe only through the lease semantics already specified in `claim_unresolved`, and production would run the reconciler as a separate deployment.

**Evidence.** `gateway/src/gateway/workers/reconciler.py`; `runpod_client.py:submit_envelope_s` (the 90.6 is a doctest); `main.py:submit_grace_s`.

## 11. Guardrails: duplicated, fail-closed, crash ≠ block

**Context.** `diffusers` FLUX pipelines ship no safety checker; whatever is not added here does not exist. And the RunPod endpoint is independently reachable by anyone holding the account API key — the gateway cannot be assumed to be the only door — not closable from this side, since the RunPod key is account-scoped and cannot be narrowed.

**Choice.** The same normalised blocklist runs at both tiers, loaded from one contract file; the gateway's is the chain meant to grow (it runs before GPU spend and can afford a model call), the worker's stays model-free (it runs on billed GPU time). A raising guardrail stops the request — fail-open for a safety control means the system reports itself protected while unprotected. But a crash is reported as `INFERENCE_FAILED`, never `PROMPT_BLOCKED`/`IMAGE_BLOCKED`: a block is a policy verdict, terminal until the prompt changes; a crash is an infra fault, retryable as-is. Conflating them lies to retrying agents and makes a guardrail outage arrive as a block-rate spike. The image hook runs before any upload, so a blocked image never reaches a reachable URL.

**Trade-off accepted.** The worker's check spends billed GPU milliseconds on every job, and the blocklist is matching machinery, not a classifier — it stops naive cases and proves the hook is wired; real classification is the unbuilt chain member.

**Evidence.** `worker/src/worker/guardrails.py`, `gateway/src/gateway/core/guardrails.py`; `contracts/blocklist.json`, `normalisation.json`, `guardrail-corpus.json`; both suites assert identical verdicts over the shared corpus.

## 12. No negative prompt

**Context.** `FluxPipeline.__call__` accepts one; the field could be passed through today.

**Choice.** Omitted. FLUX.1-dev is guidance-distilled — one forward pass per step, with guidance as an embedding. Restoring real CFG via `true_cfg_scale > 1` surrenders the distillation entirely: ~2× latency and cost, two interacting guidance controls with no principled joint tuning, and no reliable quality gain from a model not trained to be sampled that way.

**Trade-off accepted.** Callers wanting negatives are refused. Adding it later is a schema change and a pass-through. The 2× claim went unmeasured: the input schema deliberately omits the field, so it is not measurable through the deployed contract — recorded as descoped in `BENCHMARKS.md` rather than asserted.

**Evidence.** `worker/src/worker/schemas.py` (no `negative_prompt` field); [`BENCHMARKS.md`](../BENCHMARKS.md#descoped-and-why) descoped table.

## 13. GPU and defaults chosen by $/image

**Context.** The platform is fixed by the brief; the open choices were the card and the generation defaults.

**Choice.** Measured, then chosen by cost per image, not hourly rate. The 48GB tier at $1.75/hr and the A100 80GB at $2.72/hr tie at $0.0106 vs $0.0108 per default image — the A100 is 35% faster (14.3s vs 21.8s exec p50) and its HBM absorbs the +55% rate, because FLUX is memory-bandwidth-bound. $/hr picks the wrong card; $/image picks the right one. The steps sweep showed 20 steps at $0.0076 vs the 28-step default at $0.0106 — 28% cheaper and visually equivalent in the fixed-seed grid (`samples/quality-grid/`). The default stays 28 as a quality ceiling; the benchmark documents 20 as the value optimum rather than silently changing the contract.

**Trade-off accepted.** The 48GB floor is a policy artifact: this worker keeps everything resident in bf16 (~34GB). Offload (~27GB) or fp8 (~14GB) run FLUX on smaller cards, trading seconds per job — scoped in the README, and the planned 4090 floor test was dropped because it would only prove this policy's own requirement.

**Evidence.** [`BENCHMARKS.md`](../BENCHMARKS.md) GPU-tier and steps-sweep tables; methodology in its header (fixed seed, one variable at a time, N stated per cell), raw records in `benchmarks/raw.jsonl`.

## 14. CI/CD: publish on tags, deploy by hand, detect after

**Context.** A deploy costs money and bounces workers; a broken tag applied to the endpoint is a live outage.

**Choice.** `deploy.yml` has two entry points: a `v*` tag push builds and publishes `{version}-{sha}-slim` (version from `git describe`) but never deploys — deploys stay a human `workflow_dispatch`. Order: `checks` (the whole of `ci.yml`, on the tag path too) → `build-push` → `verify-tag` (`docker manifest inspect` on the tag about to be applied, so a rollback to a tag that was never pushed fails the workflow instead of the endpoint) → `deploy` (`apply_endpoint.py`, two idempotent upserts against the REST API; refuses moving tags) → `smoke-test` (`pytest -m gpu` against the live endpoint). The deploy bounces `workersMax` 0 → configured because a FlashBoot-retained worker — neither idle nor processing — resumed the previous image on its next job (observed 2026-08-06).

**Trade-off accepted.** The smoke test is detection, not prevention: `deploy` has already mutated the endpoint when it runs, so a failure there means a manual rollback — the same workflow with the previous tag, which works only because tags are immutable. Prevention needs a second endpoint and a traffic switch the platform does not give for free. The baked image never builds in CI: ~45GB against ~14GB of runner disk.

**Evidence.** `.github/workflows/deploy.yml`; [`RUNBOOK.md`](RUNBOOK.md) §Deploy via GitHub Actions and §Rollback.

## 15. Observability: stdout is the surface

**Context.** RunPod serverless runs no sidecars: no log shipper, no metrics agent. Handler stdout is the entire in-container observability surface.

**Choice.** `structlog` with a JSON renderer everywhere; `print` is banned by ruff `T20` because an unstructured line is a log no query finds. One correlation ID from HTTP header through the job row and the RunPod payload into the worker's log context and back on every error envelope; `runpod_job_id` is logged beside it so our logs join RunPod's. The reconciler's health fetch is emitted as an `endpoint_health` line per 2s tick — a log-based time series of queue depth and worker counts, one upstream call feeding three consumers (shedding, `/health/detailed`, the series). Prompts are logged as an 80-character preview plus length: a hash makes bad-image reports unanswerable, full text is unbounded user content in a log store.

**Trade-off accepted.** No metrics export, no alerting, no spans — correlation without latency breakdown. Cold-start *failures* are invisible from every surface: the container never reaches the handler, so nothing is emitted, and only synthetic probing detects them. Stated in the runbook rather than discovered.

**Evidence.** [`STANDARDS.md`](../STANDARDS.md) §8; `gateway/src/gateway/observability/`, `worker/src/worker/logging.py`; [`RUNBOOK.md`](RUNBOOK.md) §What is visible from where.

## 16. Testing: the constraint is the architecture

**Context.** CI has no GPU, no weights, no credentials for third parties.

**Choice.** No unit or integration test may require a GPU, weights, or an external network service — a design constraint, not a preference: it is why the pipeline sits behind a lazily-initialised injectable accessor (a module-level init makes `handler.py` unimportable without a GPU, therefore untestable) and why `inference.generate()` takes its pipeline as a parameter. Hand-written recording fakes at the protocol boundaries, not `MagicMock`: fakes make the negative assertions possible — the GPU was never called, nothing was uploaded — and those are frequently the assertion that matters. Contract conformance runs in both suites (schema, error codes, blocklist, normalisation, shared corpus) because it is the only thing preventing the silent-divergence failure the duplicated contract risks. Doctests are executed via scoped `--doctest-modules` invocations, so every `>>>` block is verified; the scoping keeps collection away from GPU-touching modules. Coverage gate: 80% per package (`--cov-fail-under=80` in `make check` and CI), 100% on validation and the error-code contract, review-enforced.

**Trade-off accepted.** Fakes assert against RunPod's documented contract; if the real platform diverges, only the e2e tier catches it — and it did: the error-envelope-as-string wire format was discovered by the live e2e suite on 2026-08-06, the class of bug nothing GPU-free can find. Current counts: 97 worker unit tests plus 7 e2e against the live endpoint; 240 gateway tests.

**Evidence.** [`STANDARDS.md`](../STANDARDS.md) §9; `worker/tests/`, `gateway/tests/`; [`README.md`](../README.md#current-state) §Current state for counts.

## Known limits

What the current design does not do, each with the reason that is acceptable for this repo's purpose.

- **Single-process gateway.** Breaker state and the health cache are in-memory; N replicas means N reconcilers. One local compose instance is the deployment; the multi-instance requirements are specified (lease semantics, shared breaker state) rather than built.
- **In-memory persistence.** Jobs are lost on restart and evicted after an hour. Postgres is one binding away behind the same protocol; a database nothing demonstrable needs would be scenery.
- **No per-key rate limit or spend cap.** `MAX_ACTIVE_JOBS_PER_KEY=10` bounds concurrent jobs, not request rate or spend; a key under the cap can submit indefinitely. Top-ranked gap, and the reason the gateway is not hosted; also recorded in [`SECURITY.md`](../SECURITY.md).
- **No rate limiting by IP.** Every `/v1` route already requires a key; anonymous traffic never reaches a spend path, so identity-based controls come first.
- **Endpoint id appears in git history.** Redacted from the tree (`17ebd0c`), not from history. Accepted: calling the endpoint requires the RunPod API key regardless, and the id is supplied to the reviewer anyway.
- **The endpoint is a second door with one shared key.** Account-scoped, identical for every holder, not narrowable. Not closable from this side; mitigated by key hygiene and by duplicating the guardrail into the worker.
- **Benchmark N is 3-10 per cell.** Enough to rank options, not SLO-grade percentiles; `BENCHMARKS.md` says so in its own header.
- **Prompt length is a proxy.** The 2000-character cap approximates T5's 512-token limit; a dense prompt inside the cap truncates silently. Exact validation means loading the tokenizer for a rare case.
- **`flag` verdicts have no consumer.** Recorded from day one so a review queue starts with history; today `flag` is `allow` plus an audit line.
- **One manual deploy step.** The cached-model Model field is console-only; recorded in the runbook, to be folded into `apply_endpoint.py` when the API grows the field.
- **Single region, no backup drill, no load test.** The failure domains of a real service, out of scope for a graded exercise; named here rather than left implied.
