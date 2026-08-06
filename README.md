# FLUX.1-dev on RunPod Serverless

A serverless text-to-image endpoint running `black-forest-labs/FLUX.1-dev`, deployed on RunPod using cached models — the platform pre-stages the weights on host machines. The baked-weights image is a one-command build target (`make build-baked`), not deployed — see [Weight delivery](#weight-delivery).

> **Status:** live and verified 2026-08-06 — endpoint `<endpoint-id — supplied in the submission email>`, image `0.1.0-72e537d-slim`, 48GB GPU tier, weights via RunPod's model store. 7/7 e2e cases pass against it; committed samples with seeds in [`samples/`](samples/); measured results in [`BENCHMARKS.md`](BENCHMARKS.md) (156 records, raw data committed).

## What's here

Three tiers, in the order worth reading:

| Tier | Where | What it is |
|---|---|---|
| **The deliverable** | [`worker/`](worker/), [`worker/Dockerfile`](worker/Dockerfile), [`deploy/endpoints/`](deploy/endpoints/), [`scripts/apply_endpoint.py`](scripts/apply_endpoint.py), [`docs/RUNBOOK.md`](docs/RUNBOOK.md), [`samples/`](samples/), [`client/generate.py`](client/generate.py) | The brief: handler, image, deployed endpoint, demo client, operations |
| **The measurements** | [`BENCHMARKS.md`](BENCHMARKS.md), [`benchmarks/`](benchmarks/) | What the endpoint actually does under one variable at a time — steps, resolution, payload, concurrency, cold starts — harness, config and raw JSONL included |
| **Beyond the brief** | [`gateway/`](gateway/) | A production-shaped API tier in front of the endpoint: auth, idempotency, job store, reconciler. Fenced off in [`gateway/README.md`](gateway/README.md) |

Design record in [`docs/specs/`](docs/specs/00-overview.md). Engineering conventions in [`STANDARDS.md`](STANDARDS.md) — that file doubles as the working guide for the agentic coding process used to build this repo, which is why it spells out rules a human reviewer would take as given.

## Call it

One command against a warm worker, prompt in, image out:

```bash
export RUNPOD_API_KEY=...        # your RunPod key
export RUNPOD_ENDPOINT_ID=...    # the deployed endpoint

curl -s -X POST "https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/runsync" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input": {"prompt": "a red fox in falling snow, cinematic lighting"}}' \
  | jq -r '.output.image_base64' | base64 -d > fox.png
```

`/runsync` holds the connection and returns the finished output, so a warm job
(~22s at the defaults, ~1.7MB of base64 — measured, `BENCHMARKS.md`) completes
in a single call. A **cold** worker takes longer than the hold: ~15s p50 when
FlashBoot resumes an idle worker, but minutes when staging lands on a fresh
host (measured p50 304s, max 518s). Warm the endpoint with one throwaway
request before demonstrating.

For anything real, submit and poll — that is what the client does, with live
progress and observed wall time:

```bash
pip install -r client/requirements.txt      # RunPod's official SDK
python client/generate.py "a red fox in falling snow" --out fox.png
# job 7f3a-...
#   IN_QUEUE
#   IN_PROGRESS
#   COMPLETED
# saved fox.png  seed=918273  1024x1024  inference=21.4s  observed=24.8s
```

The client uses `runpod.Endpoint` rather than raw HTTP — the SDK owns the
polling loop, the `{"input": ...}` envelope and the retry semantics, so ~15
lines replace ~150. The same operations by hand:

```bash
curl -s -X POST "https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/run" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input": {"prompt": "a red fox in falling snow"}}'
# -> {"id": "abc-123", "status": "IN_QUEUE"}

curl -s "https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/status/abc-123" \
  -H "Authorization: Bearer $RUNPOD_API_KEY"
```

**Results expire.** RunPod retains a finished job's output for **30 minutes**
after `/run`, and **1 minute** after `/runsync`. Anything polling — the client,
the gateway reconciler — must fetch inside that window; afterwards `/status`
has nothing left to return.

`GET /status/{id}` returns the handler's output verbatim, so the completed response carries the image inline:

```json
{
  "status": "COMPLETED",
  "output": {
    "image_base64": "iVBORw0KGgo...",
    "format": "png",
    "seed": 918273,
    "width": 1024,
    "height": 1024,
    "num_inference_steps": 28,
    "guidance_scale": 3.5,
    "model_version": "black-forest-labs/FLUX.1-dev@<revision>",
    "timings": {"inference_s": 21.4, "encode_s": 0.3}
  }
}
```

While a job runs, `status` polls return progress, throttled to ~10% strides
(each report costs a platform round trip, so per-step updates would tax the
GPU loop for nothing):

```json
{"status": "IN_PROGRESS", "output": {"step": 12, "total": 28, "percent": 43}}
```

## Input

Only `prompt` is required.

| Field | Type | Default | Range |
|---|---|---|---|
| `prompt` | string | — | 1–2000 chars, non-blank |
| `width` | int | 1024 | 256–1536, snapped down to ×16 |
| `height` | int | 1024 | 256–1536, snapped down to ×16 |
| `num_inference_steps` | int | 28 | 1–50 |
| `guidance_scale` | float | 3.5 | 0–20 |
| `seed` | int \| null | random | 0–2³¹−1, always echoed back |
| `output_format` | `"png"` \| `"jpeg"` | `"png"` | — |
| `correlation_id` | string \| null | null | bound into worker logs |

**No negative prompt.** FLUX.1-dev is guidance-distilled: it runs one forward pass per step with guidance as an embedding input. Real classifier-free guidance (`true_cfg_scale > 1`) restores negative prompts but runs a second pass per step — roughly double the latency and cost — while adding a second guidance control that interacts with the distilled one, for no reliable quality gain.

**Dimensions snap down** to a multiple of 16, because FLUX latents are 16× downsampled. The effective values are returned, so a request for 1000px reports 992.

**The seed is always returned**, including when randomly chosen, so every image is reproducible.

## Errors

A rejected or failed job reports `"status": "FAILED"`, and the structured envelope arrives **JSON-encoded inside the platform's `error` string** — decode it to get code, message and suggestion:

```json
{"status": "FAILED",
 "error": "{\"code\": \"PROMPT_BLOCKED\", \"message\": \"Prompt matched a blocked term.\", \"suggestion\": \"Rephrase the prompt and resubmit.\"}"}
```

The encoding is forced by the platform: RunPod carries a job's error only as a string, and a dict returned there is silently dropped — the caller would see a completed job with no output at all. Found by the e2e suite against the live endpoint; the string round-trip preserves the envelope for humans and agents alike.

| Code | Meaning |
|---|---|
| `INVALID_PROMPT` | Blank, or over 2000 characters |
| `INVALID_DIMENSIONS` | Outside 256–1536 |
| `INVALID_STEPS` | Outside 1–50 |
| `PROMPT_BLOCKED` | Rejected by the prompt guardrail |
| `IMAGE_BLOCKED` | Rejected by the image guardrail |
| `OOM` | VRAM exhausted; the worker is retired via `refresh_worker` |
| `INFERENCE_FAILED` | Unclassified pipeline failure |

Every error carries a `suggestion` naming the next valid action, because callers include agents that cannot infer recovery from prose.

## Develop

```bash
make install       # sync both package environments
make check         # format, lint, types, imports, tests + coverage gate
make doctest       # the executable examples
make weights-check # verify the weight filter against HF. Downloads nothing.
```

CI runs the same gates as `make check` — both packages, the import-linter
layering contract, and the CLI-tool lint — plus the doctests. **No unit or
integration test requires a GPU, model weights, or an external network
service** — that is a design constraint, not a preference, and it is what
forces the pipeline behind an injectable accessor rather than a module-level
global.

## Build and deploy

The deployed image is ~2.9GB — no weights, and no CUDA base image, since the torch wheel carries its own libraries. **Build it locally**; there is no Pod in the procedure. Full steps in [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

```bash
make build-slim       # tags ghcr.io/andyozj/flux-worker:<version>-<sha>-slim
docker push ghcr.io/andyozj/flux-worker:$(make -s print-tag)-slim
```

Versioning is tag-driven: the most recent `v*` git tag names the version, the commit SHA makes the image tag immutable, and `make print-tag` shows the result. Override `IMAGE`/`TAG` on the command line for another registry. `--platform linux/amd64` is set in the Makefile — without it an arm64 build produces an image RunPod cannot run, and the failure presents as a worker that starts and immediately dies.

CD: `.github/workflows/deploy.yml` runs CI, builds, pushes and applies from one button (rollback = re-run with the previous tag); a `v*` tag push publishes the image without deploying. Details in the [RUNBOOK](docs/RUNBOOK.md).

### Weight delivery

The brief says *"Build a Docker image that includes your serverless handler and the model."* Both images build from one `Dockerfile` via `BAKE_WEIGHTS` — but **the deployed endpoint uses RunPod's cached models**, which pre-stage the repository on host machines before a worker starts. The baked image remains the documented fallback: buildable on demand, not published.

| | Baked (~45GB) | **Cached (~2.9GB)** |
|---|---|---|
| Fresh-worker scale-up | Pull 45GB | Pull 2.9GB; host already holds the model |
| Build and push | 30-60 min, and may exceed registry layer caps | Minutes |
| Storage cost | Registry only | **None** |
| Weight transfer | Billed at build | **Unbilled, pre-staged** |
| Maturity | Stable | **Shipped 2026-08** — no beta label; limits: one model per endpoint, all quantizations stage together |

A deliberate deviation, stated rather than hidden. Staging pulls the whole repo, so the ~24GB of duplicate single-file weights come along — unbilled, and not our disk.

**A network volume was tried and dropped.** Its cost is a datacenter pin that narrows the GPU pool exactly when scaling up under load — the moment it was meant to help — plus a per-GB bill and a population step. It is not kept as a fallback, because a volume is only a fallback if it is *already populated*, and populating one costs everything removing it avoided. The fallback is the baked image, already a build target.

`weights.resolve()` tries the configured path, then the model cache — so a deployment picks a mechanism by configuration alone. The staged snapshot is identified through the cache's `refs/main`, and the revision it actually holds is reported on every result; the worker refuses to start only when several snapshots coexist with no ref naming the staged one. RunPod's own example picks an arbitrary snapshot in that case, which would misattribute every image.

The deployed (cached) variant is what [`BENCHMARKS.md`](BENCHMARKS.md) measures; the baked variant is built but not deployed, so it carries no numbers.

Two details that will otherwise cost you an hour each:

- **`HF_TOKEN` is a BuildKit secret, never an `ARG`.** An `ARG`-passed token is recoverable from image history by anyone who pulls the image.
- **The repo ships duplicate weights.** It contains both the `diffusers` sharded layout *and* the standalone `flux1-dev.safetensors` (23.8GB) plus `ae.safetensors`. A naive `snapshot_download` pulls ~56GB instead of ~33GB. `make weights-check` proves the filter works without downloading anything.

## Design

| | |
|---|---|
| Model | FLUX.1-dev, bf16, unquantized. The loaded revision is discovered from the staged snapshot and reported on every result — cached models offer no revision control, so it is a reported fact, not a pin |
| Inference | `diffusers.FluxPipeline`. No `torch.compile` — 3-10 min of compilation on every cold start is net slower for serverless traffic |
| Weights | **Cached models** (deployed). Baked image kept as the build-target fallback; network volume evaluated and dropped — see *Weight delivery* above |
| GPU | 48GB tier. This worker keeps everything resident in bf16 (~34GB: 23.8GB transformer + ~9.5GB T5-XXL) for the fastest warm latency — under *that* policy 48GB is the floor. Smaller cards run FLUX fine with CPU offload (~27GB peak, fits 40GB) or fp8 quantization (fits 24GB), trading seconds per job for VRAM |
| Concurrency | `concurrency_modifier = 1`. The worker is GPU-bound; a second concurrent job causes VRAM contention, not throughput |

Full reasoning, including what is deliberately *not* built and what production would cost, is in [`docs/specs/`](docs/specs/00-overview.md).

### Diagrams

Each opens in Excalidraw and is editable.

| Diagram | Shows |
|---|---|
| [System context](https://excalidraw.com/#json=uZeWZkY3mvbnTlHe4SFAE,Jz6Rkp2yaUftbLUiqkydew) | Two tiers, one direction of dependency |
| [Worker lifecycle](https://excalidraw.com/#json=kwh-XFq8_sxRUP9eEND3M,7JVRVZVNXn7KKsg89QYG3Q) | Cold start vs warm, and why the pipeline is lazy |
| [Job state machine](https://excalidraw.com/#json=GpbKfRPNbJuct2xSpLadJ,RQKzqy43XOY0h7AxpzIIlg) | Terminal states, and what the reconciler may not do |
| [Guardrail chain](https://excalidraw.com/#json=qfBT0mQYdPnR_iafmHXQW,Ob3GBEjScWmywFHqppcU3Q) | Two prompt checkpoints, and why both are needed |
| [Correlation](https://excalidraw.com/#json=Uig_Bds2I3M_Cq9NVyszm,_Y0E4L_-BuLpMwEW6chxlA) | One id from HTTP through the GPU and back |
| [Image layers](https://excalidraw.com/#json=KhbTALYghWAfdJV8d5Wcs,9OZHA523fBKl-aOIJQc6Ow) | Why weights sit below application code |

## Repository

```
contracts/           shared source of truth for both tiers
worker/              the serverless worker — the graded deliverable
  src/worker/        handler, pipeline, inference, guardrails, schemas
  scripts/           fetch_weights.py
  Dockerfile
client/generate.py   demo client, on RunPod's Python SDK
BENCHMARKS.md        measured results — rendered, never hand-written
benchmarks/          harness.py, config.json, raw.jsonl (the evidence)
samples/             committed generations with their seeds
gateway/             FastAPI tier (beyond the brief) — see gateway/README.md
deploy/endpoints/    endpoint configuration as code
scripts/             apply_endpoint.py
docs/RUNBOOK.md      build, deploy, rollback, diagnosis
docs/specs/          design, 10 documents
STANDARDS.md         engineering conventions; guide for the agentic workflow
```

## Current state

| | |
|---|---|
| Endpoint | **Live** since 2026-08-06 — 7/7 e2e cases pass, three samples committed with seeds |
| Image | `ghcr.io/andyozj/flux-worker:0.1.0-72e537d-slim`, 2.9GB, public, no secrets in any layer |
| Worker | 70 unit tests (no GPU required) + 7 e2e against the live endpoint |
| `BENCHMARKS.md` | Measured 2026-08-06 — 156 records incl. an A100 cross-tier run, raw JSONL committed, methodology in [`docs/specs/09-benchmarks.md`](docs/specs/09-benchmarks.md) |
| Gateway | Beyond-brief tier: core, async API, reconciler, 86 tests; containerised and in CI alongside the worker |

## Licence

FLUX.1-dev is released under a non-commercial licence. Fine for evaluation; commercial use requires FLUX.1-schnell (Apache-2.0) or a commercial licence from Black Forest Labs.
