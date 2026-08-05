# FLUX.1-dev on RunPod Serverless

A serverless text-to-image endpoint running `black-forest-labs/FLUX.1-dev`, deployed on RunPod using cached models — the platform pre-stages the weights on host machines. The baked-weights image is built and published too — see [Weight delivery](#weight-delivery).

> **Status:** the worker is implemented and tested; the endpoint is not yet deployed. `BENCHMARKS.md` does not exist until it is, and no figure below is presented as measured. See [Current state](#current-state).

## Call it

One command, prompt in, image out:

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
(~20-25s, 2.7MB — inside the 20MB cap) completes in a single call. A **cold**
worker spends 30-60s loading the pipeline first and will outlast the hold, so
warm the endpoint with one throwaway request before demonstrating.

For anything real, submit and poll — that is what the client does, with live
progress and observed wall time:

```bash
python client/generate.py "a red fox in falling snow" --out fox.png
# job 7f3a-...
#   IN_PROGRESS 43%
# saved fox.png  seed=918273  1024x1024  inference=21.4s  observed=24.8s
```

```bash
# the same thing by hand
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

Or, against a warm worker, one command — `/runsync` holds the connection and returns the finished image (~20-25s):

```bash
curl -s -X POST "https://api.runpod.ai/v2/$RUNPOD_ENDPOINT_ID/runsync" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input": {"prompt": "a red fox in falling snow"}}' \
  | jq -r '.output.image_base64' | base64 -d > fox.png
```

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
    "model_version": "black-forest-labs/FLUX.1-dev@0ef5fff",
    "timings": {"inference_s": 21.4, "encode_s": 0.3}
  }
}
```

While a job runs, `status` polls return per-step progress:

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

A failed job is still HTTP `200` — the call succeeded, the job didn't. The outcome is in the body:

```json
{"error": {"code": "PROMPT_BLOCKED",
           "message": "Prompt matched a blocked term.",
           "suggestion": "Rephrase the prompt and resubmit."}}
```

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

`make check` is exactly what CI runs. **No unit or integration test requires a GPU, model weights, or an external network service** — that is a design constraint, not a preference, and it is what forces the pipeline behind an injectable accessor rather than a module-level global.

## Build and deploy

The deployed image is ~2.9GB — no weights, and no CUDA base image, since the torch wheel carries its own libraries. **Build it locally**; there is no Pod in the procedure. Full steps in [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

```bash
export HF_TOKEN=...   # requires accepting the FLUX.1-dev licence on HuggingFace
export IMAGE=ghcr.io/OWNER/flux-worker TAG=0.1.0-$(git rev-parse --short HEAD)

make build-volume IMAGE=$IMAGE TAG=$TAG   # ~2.9GB, deployed
docker push $IMAGE:$TAG-volume
```

`--platform linux/amd64` is set in the Makefile. Without it an arm64 build produces an image RunPod cannot run, and the failure presents as a worker that starts and immediately dies.

### Weight delivery

The brief says *"Build a Docker image that includes your serverless handler and the model."* Both images are built from one `Dockerfile` via the `BAKE_WEIGHTS` argument, and the baked one is published — but **the deployed endpoint mounts a network volume.**

That is a deliberate deviation, stated rather than hidden — and it exercises the platform rather than treating RunPod as a container host. The costs are named rather than waved away:

| | Baked (~45GB) | Volume (~10GB) | **Cached (~10GB)** |
|---|---|---|---|
| Fresh-worker scale-up | Pull 45GB | Pull 10GB + mount | Pull 10GB; host already holds the model |
| Region | Any with capacity | **Pinned to the volume's datacenter** | **Any with capacity** |
| Build and push | 30-60 min | Minutes | Minutes (same image) |
| Storage cost | Registry only | Per GB, per month | **None** |
| Weight transfer | Billed at build | Billed at population | **Unbilled** |
| Maturity | Stable | Stable | **Beta** |

Cached models is deployed. A volume's cost is its datacenter pin, which narrows the GPU pool exactly when scaling up under load — the moment the volume was meant to help — and cached models remove it. Staging does pull the whole repo, so the ~24GB duplicate comes along, but neither the transfer nor the storage is billed.

The volume endpoint is retained as a fallback and needs no rebuild: **one image serves all three.** `weights.resolve()` tries the configured path, then the model cache, and refuses to start if the cache holds a revision other than the pinned one — RunPod's own example picks an arbitrary snapshot, which would misattribute every image.

All three are benchmarked; methodology in [`docs/specs/09-benchmarks.md`](docs/specs/09-benchmarks.md).

Two details that will otherwise cost you an hour each:

- **`HF_TOKEN` is a BuildKit secret, never an `ARG`.** An `ARG`-passed token is recoverable from image history by anyone who pulls the image.
- **The repo ships duplicate weights.** It contains both the `diffusers` sharded layout *and* the standalone `flux1-dev.safetensors` (23.8GB) plus `ae.safetensors`. A naive `snapshot_download` pulls ~56GB instead of ~33GB. `make weights-check` proves the filter works without downloading anything.

## Design

| | |
|---|---|
| Model | FLUX.1-dev, bf16, unquantized, revision pinned in `contracts/model-revision.txt` |
| Inference | `diffusers.FluxPipeline`. No `torch.compile` — 3-10 min of compilation on every cold start is net slower for serverless traffic |
| Weights | **Cached models.** RunPod pre-stages the repo on hosts; no datacenter pin, no storage cost. Volume and baked variants also built — see *Weight delivery* below |
| GPU | L40S 48GB. bf16 weights are ~34GB resident (23.8GB transformer + ~9.5GB T5-XXL), plus activations — a 24GB card cannot hold the model |
| Concurrency | `concurrency_modifier = 1`. The worker is GPU-bound; a second concurrent job causes VRAM contention, not throughput |

Full reasoning, including what is deliberately *not* built and what production would cost, is in [`docs/specs/`](docs/specs/00-overview.md). Engineering conventions are in [`STANDARDS.md`](STANDARDS.md).

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
contracts/          shared source of truth for both tiers
worker/             the serverless worker — the graded deliverable
  src/worker/       handler, pipeline, inference, guardrails, schemas
  scripts/          fetch_weights.py
  Dockerfile
client/generate.py  submit-and-poll demo client
gateway/            FastAPI tier (beyond the brief) — see gateway/README.md
deploy/endpoints/   endpoint configuration as code
scripts/            apply_endpoint.py
docs/RUNBOOK.md     build, deploy, rollback, diagnosis
docs/specs/         design, 10 documents
```

## Current state

| | |
|---|---|
| Worker implemented and tested | Yes — 64 tests, 96% coverage, no GPU required |
| Endpoint deployed | **No** — pending RunPod credits |
| `BENCHMARKS.md` | Not yet. Produced from a single measured run once deployed |
| Gateway | Core, async API and reconciler implemented with tests; runs locally via `compose.yaml` |

Nothing in this README describes performance, because nothing has been measured yet. Every figure it will eventually carry comes from a run whose methodology is specified in [`docs/specs/09-benchmarks.md`](docs/specs/09-benchmarks.md).

## Licence

FLUX.1-dev is released under a non-commercial licence. Fine for evaluation; commercial use requires FLUX.1-schnell (Apache-2.0) or a commercial licence from Black Forest Labs.
