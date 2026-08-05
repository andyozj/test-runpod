# The full picture

2026-08-05. Everything in one place: what we're building, how it's structured,
what happens at runtime, every decision and why, and exactly what remains.
Companion to the short version in `where-we-are.md`.

---

## 1. The assignment

RunPod's hiring case study. Deploy `black-forest-labs/FLUX.1-dev` — a 12B
text-to-image model — as a serverless endpoint on their platform. Up to 3 days.

Required deliverables, verbatim from the brief:

1. A `handler.py` that handles serverless requests
2. A Docker image with the handler and the model
3. A deployed serverless endpoint
4. A demonstration: text in, image out
5. Code and documentation

Graded on: platform usage, handler/model integration, working deployment,
documentation clarity.

## 2. The shape of the solution

Two parts. Only part 1 is graded.

**Part 1 — the endpoint (graded).** Our worker code in a Docker image, run by
RunPod's queue infrastructure. Anyone with an API key can use it with curl.
Nothing else needs to exist for it to work.

**Part 2 — the gateway (reference architecture).** A FastAPI service showing
what a client company would build *around* the endpoint the moment their own
users' traffic touches it: their own API keys, retry-safe billing, durable job
history. It runs locally via `docker compose`. It is finished, frozen, and
deliberately not deployed — deploying it adds nothing gradeable and exposes a
GPU-spending credential.

The consulting line that connects them: internal scripts call the endpoint
directly (`client/generate.py` is the whole integration); the gateway pattern
starts when you resell generation to your own users.

## 3. Repository map

```
contracts/                      Shared truth. Both tiers load these files.
  generation-request.schema.json  What a valid request is
  error-codes.json                Every error code that can exist
  blocklist.json                  Prompt guardrail terms
  model-revision.txt              THE pinned model version (a git SHA)

worker/                         Part 1. The graded deliverable.
  src/worker/
    handler.py                    RunPod entrypoint: parse → guard → generate → return
    schemas.py                    Request/response validation (pydantic)
    pipeline.py                   Lazy FLUX loader (import never touches GPU)
    weights.py                    Finds weights: explicit path → model cache → refuse
    inference.py                  The actual generation. Pure function.
    guardrails.py                 Prompt blocklist (evasion-resistant matching)
    errors.py                     Error codes + envelopes
    settings.py                   All env reading happens here, nowhere else
    contracts.py                  Locates contracts/ at any directory depth
  scripts/fetch_weights.py      Downloads the 33GB diffusers layout (volume population)
  tests/                        70 tests, no GPU needed
  test_input.json               One-command smoke test on real hardware
  Dockerfile                    2.92GB image. No weights inside.

client/generate.py              Submit → poll → save image. Zero dependencies.

gateway/                        Part 2. FastAPI + reconciler + tests (67).
compose.yaml                    `docker compose up` runs the gateway locally.

deploy/endpoints/               Endpoint config as code, applied by script:
  cached.yaml                     THE deployed endpoint (cached models)
  volume.yaml                     Fallback (network volume, needs population)
  baked.yaml                      Vestigial — see §13 cleanups
scripts/apply_endpoint.py       Creates/updates endpoints from the YAML

docs/specs/00-09                Ten design docs. The reasoning, not just the what.
docs/RUNBOOK.md                 Operational steps (partly stale — see §13)
docs/notes/                     Working notes, this file
```

Two structural rules hold everywhere:

- **`core/` imports nothing outward** (gateway) — enforced by import-linter,
  not by review.
- **No test needs a GPU, weights, or the network.** This forced the design
  that makes everything below testable on a laptop.

## 4. What happens on a request

### Warm worker (the normal case, ~20-25s)

```
1. POST /runsync or /run          your prompt, as JSON
2. RunPod queues the job          worker picks it up
3. handler.py: parse              bad input → clean error, no GPU time spent
4. handler.py: prompt guardrail   blocked term → PROMPT_BLOCKED, no GPU time
5. inference: 28 denoising steps  ~20s on L40S; progress reported per step
6. encode to PNG/JPEG, base64     ~0.3s
7. response                       image + seed + dimensions + model version + timings
```

While it runs, polling `/status/{id}` shows `{"step": 12, "total": 28,
"percent": 43}` — a progress bar, not a black box.

Every response carries the seed (even when random) so any image is
reproducible, and `model_version` (repo@revision) so every image is
attributable to exact weights.

### Cold start (first request after idle)

```
1. RunPod picks a host            prefers one already holding the cached model
2. Pulls our 2.92GB image         fast; and cached on the host afterwards
3. Weights already on host disk   the cached-models feature staged them — unbilled
4. Container starts               weights.resolve() finds the snapshot, verifies
                                  it is the PINNED revision, else refuses to boot
5. FLUX loads to GPU              ~30-60s, once per worker lifetime, not per job
6. Then serves jobs               FlashBoot keeps VRAM warm across idle gaps
```

The refusal in step 4 matters: RunPod's own tutorial code loads whatever
snapshot it finds. Ours won't run weights it can't prove are the pinned
version — otherwise every response would claim a model version it might not be.

### Failure paths

| What breaks | What happens |
|---|---|
| Invalid input | Error envelope with code + message + what-to-do-next. HTTP still 200 — the *call* worked, the job didn't |
| Blocked prompt | `PROMPT_BLOCKED` before any GPU time is billed |
| GPU out of memory | `OOM` + `refresh_worker: true` — the worker is retired, not trusted again |
| Any other crash | `INFERENCE_FAILED`; detail logged, never leaked to caller |
| Wrong/missing weights | Worker refuses to start, error names what it found |

## 5. The weight story

The model is 33GB. The whole architecture question was: where do those bytes
live? Three mechanisms, one worker:

| Mechanism | Weights live | Cold start | Costs | Status |
|---|---|---|---|---|
| **Cached models** | Host disk, staged by RunPod before the worker starts | Seconds | Download unbilled, storage free | **Deployed** |
| Network volume | A volume we populate once (33GB) | Volume read | Per-GB/month + pins endpoint to one datacenter | Fallback, config-only switch |
| Baked in image | Inside a ~38GB image | Image pull | Slow push/pull, likely exceeds GHCR layer caps | Rejected |

`weights.py` resolves in order: explicit `WEIGHTS_PATH` → model cache → refuse
with an explanatory error. So switching mechanisms is endpoint configuration,
never a rebuild.

**The revision pin.** `contracts/model-revision.txt` holds one git SHA.
`fetch_weights.py` downloads exactly it, the resolver only accepts exactly it,
and every response reports it. Two deployments can never silently run
different models.

**The duplicate-weights trap.** The HF repo ships the same weights twice
(sharded layout + one 23.8GB single file). Naive download = 56GB instead of
33GB. `make weights-check` proves our filter against the live manifest without
downloading anything.

## 6. The image

| Layer | Size | Notes |
|---|---|---|
| `ubuntu:22.04` | ~80MB | Not the CUDA base — see below |
| CPython 3.11.15 via uv | ~95MB | Not Ubuntu's apt package — see below |
| Python deps from `uv.lock` | ~5GB | torch, diffusers, transformers — 130 exact pins |
| Our code + contracts | <200KB | The only layer that changes per commit |

Total 2.92GB as reported by Docker. Started the day at a projected 45GB.

Two non-obvious choices, both learned the hard way today:

- **No `nvidia/cuda` base.** The torch wheel bundles its own CUDA libraries
  and the driver comes from the host. The official CUDA base duplicated 6.6GB
  that nothing would ever load.
- **No apt Python.** Ubuntu 22.04 ships Python 3.11.0**rc1** — a release
  candidate, frozen forever, missing a function torch 2.13 needs. It crashes
  at import. uv installs a real 3.11.15 instead.

The deps layer ends with `python -c "import torch, diffusers, ..."` — so a
broken interpreter/deps combination fails the *build*, never the endpoint.

## 7. Decision log

Every material decision, one line of why. Specs hold the full reasoning.

| Decision | Why |
|---|---|
| Queue endpoint, not load-balancer | 20-25s GPU-bound jobs need a queue absorbing bursts and cold starts. LB endpoints suit sub-second work. Also: the brief grades `handler.py`, which only queue endpoints have |
| Cached models deployed | Fastest cold start, no datacenter pin, no storage bill, unbilled staging. RunPod's own recommendation for HF-hosted models |
| Volume kept as fallback | Cached models is beta. Same image, one YAML apart |
| Baked variant dropped | ~38-45GB image: registry layer caps likely block the push, and every scale-up pulls it. The brief's "image includes the model" wording is addressed head-on in the README as a documented deviation |
| No Pods anywhere | They were only build machines for the 45GB image. At 2.92GB we build and push from a laptop |
| bf16, unquantized | The case study should show the real model, not a compromise of it |
| No negative prompt | FLUX is guidance-distilled: real CFG would double latency/cost for unreliable quality gain. Documented, measured in benchmarks |
| No `torch.compile` | 3-10 min compile on every cold start is net slower for serverless traffic |
| `concurrency_modifier = 1` | One job saturates the GPU; a second causes VRAM contention, not throughput |
| L40S 48GB, A100 80GB fallback | bf16 weights are ~34GB resident. 24GB cards cannot load the model. GPU priority list keeps the demo alive if L40S is scarce |
| Base64 response by default | `GET /status` returns handler output verbatim and a reviewer has no storage credentials. Only base64 unconditionally satisfies "return an image" |
| Async is the service interface | 20-25s held connections fight every load-balancer default and re-bill on retry |
| `/runsync` is the demo | One curl → one image is literally the grading test. Works because a warm job fits the hold |
| Errors carry `suggestion` | Callers include agents that can't infer recovery from prose |
| Seed always returned | Every image reproducible |
| Revision refusal at boot | A model mismatch should be a loud failure, not a silent misattribution |
| Config as code (`deploy/endpoints/*.yaml`) | "Why is idle timeout 60s?" must be answerable from a diff; a deleted endpoint must be reconstructible |
| Immutable image tags, never `latest` | Rollback = redeploy the previous tag. `latest` destroys the rollback target |

## 8. Testing — what each layer proves

| Layer | Count | Needs | Proves |
|---|---|---|---|
| Unit + integration (`make check`) | 137 | Nothing | Logic: validation, guardrails, error codes, resolver, gateway service, contract conformance between tiers |
| Doctests | few | Nothing | Examples in docstrings actually run |
| Local Docker build + in-image smoke | — | Docker | The image builds, imports work, the real handler returns correct envelopes — done 4× today |
| `test_input.json` via SDK local mode | 1 | GPU | First real image, before any endpoint exists |
| E2E against live endpoint | 8 cases | Deployed endpoint | The graded demonstration + reproducibility + progress + blocked-prompt path |

The rule forcing the design: **no test in `make check` may need a GPU, weights,
or the network.** That's why the pipeline is behind a lazy accessor, why
inference takes its pipeline as a parameter, and why today's five bugs were
findable on a Mac.

Today's catch record, each worth ~an hour of paid GPU debugging:

1. Dockerfile `COPY` paths wrong for the repo-root build context
2. `fetch_weights.py` resolved `contracts/` at the wrong depth inside the image
3. Same bug in `guardrails.py` and `errors.py` (found by in-image smoke test)
4. Ubuntu's rc1 Python crashing torch at import
5. `uv.lock` copied but unused — deps floated instead of being pinned

## 9. The gateway, precisely

What it is: the service layer a company runs when *their* users consume the
endpoint. What each piece buys:

| Feature | Problem it solves |
|---|---|
| API-key auth | Your RunPod key never leaves the server; per-client keys, revocable |
| Idempotency keys | A user's retry doesn't bill a second generation |
| Reconciler + job store | RunPod deletes results 30 min after completion; the gateway remembers forever |
| Circuit breaker + 429 | An upstream wobble or traffic burst doesn't cascade |
| Image proxy | Users fetch images by URL without holding storage credentials |

What it is **not**: needed for testing (curl suffices), needed for grading, or
a replacement for RunPod's queue (it sits in front of it, on cheap CPU).

## 10. The deployment plan, step by step

**You, first (blockers):**

1. Money on the RunPod account — credits from hailong.yang or ~$20 self-fund
2. HF account: accept the FLUX.1-dev licence, create a read token,
   `make weights-check` to prove it (downloads nothing)
3. GHCR: a personal access token with `write:packages`

**Then, from this Mac (~30 min total):**

4. `docker login ghcr.io` → build with the real tag → push (~compressed 2GB up)
5. Make the GHCR package public (or add registry credentials on RunPod)
6. `python scripts/apply_endpoint.py --config deploy/endpoints/cached.yaml --tag <tag>`
   — sets Model field `black-forest-labs/FLUX.1-dev` + HF token, L40S→A100,
   min workers 1 for the demo window
7. Watch the endpoint stage the model (unbilled), then first `/runsync`:

```bash
curl -s -X POST "https://api.runpod.ai/v2/$ENDPOINT_ID/runsync" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input": {"prompt": "a red fox in falling snow"}}' \
  | jq -r '.output.image_base64' | base64 -d > fox.png
```

8. Commit 2-3 sample images with their prompts and seeds
9. Benchmark run (§11) → `BENCHMARKS.md`
10. Endpoint ID + working curl into the README → submit

If cached models misbehaves (it's beta): `volume.yaml` is the fallback — one
throwaway Pod for ~15 min to populate the volume, everything else identical.

## 11. What the benchmark will measure

One run, after everything works. Rules: N≥10 per config, p50/p95 not means,
fixed seeds, one variable at a time, every figure carries hardware + date +
image tag.

1. **Cold start, decomposed** — pull vs stage vs load vs first inference;
   true cold vs FlashBoot resume
2. **Latency sweeps** — steps (4→50) and resolution (512²→1536²)
3. **GPU comparison** — L40S vs A100 80GB (+4090 as the VRAM-floor proof:
   expected to fail at load, and where it fails is a result)
4. **Cost per image** — derived from measured seconds × published rates,
   cross-checked against the invoice
5. **Throughput/concurrency** — queue wait vs execution under load
6. **Payload ceilings** — does 1536² PNG survive the response path, or does
   JPEG become the high-res default
7. **Weight delivery comparison** — cached vs volume, with the sha256
   precondition proving both run identical weights
8. **Claims audit** — the "CFG costs 2×" and "FlashBoot beats cold start"
   assertions get measured or retracted
9. **Quality vs steps** — fixed-seed image grid: if 20 steps looks like 28,
   the default is wrong and the benchmark changed a decision

## 12. How this maps to the grading

| Criterion | Where it's answered |
|---|---|
| Platform usage | Queue endpoint, cached models, FlashBoot, GPU priorities, progress updates, `refresh_worker`, config-as-code, SDK test harness — each chosen over an alternative, with the alternative named |
| Handler + model integration | `handler.py` + the tested pipeline/resolver chain; proven in-image before any deploy |
| Working deployment | The endpoint + one-curl demo + committed samples (pending funds) |
| Documentation | README (call it in 60 seconds) → specs (every decision) → BENCHMARKS.md (every number measured) |

## 13. Open cleanups (small, known, not blocking)

| Item | State |
|---|---|
| `docs/RUNBOOK.md` | Still says "do not build locally / 45GB / build on a Pod" — stale since the image hit 2.92GB. Needs a rewrite to the laptop flow |
| `make build-baked` + `deploy/endpoints/baked.yaml` | Vestigial after dropping the baked variant. Either delete, or keep explicitly labelled as the documented-but-rejected option |
| `README` build section | Verify it matches the final no-Pod flow |
| cu130 torch wheel | Needs CUDA-13-capable hosts. Settled at first deploy; two known fixes if it bites (endpoint CUDA filter, or repin to cu126) |

## 14. Risks that remain

| Risk | Likelihood | Plan |
|---|---|---|
| cu130 wheel vs host drivers | Real | CUDA version filter on the endpoint, or repin torch — one commit either way |
| Cached models (beta) misbehaves | Low | Volume fallback, config-only |
| L40S scarce at demo time | Low | A100 fallback is already in the GPU priority list |
| 1536² PNG exceeds response limit | Possible | JPEG fallback; measured, not guessed |
| Credits never arrive | Moot | $20 self-fund was the decision already |

---

*Everything above the deployment line is done and verified. The distance to a
live demo is: account funds, two tokens, one push, one YAML apply, one curl.*
