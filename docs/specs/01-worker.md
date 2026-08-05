# 01 — Worker

Tier 1. Runs on RunPod Serverless. This is the graded deliverable.

> **Diagram:** worker lifecycle, cold vs warm — *pending Excalidraw*

## Modules

| Module | Responsibility |
|---|---|
| `handler.py` | RunPod entrypoint. Parse → guard → delegate → serialise. No inference logic. |
| `pipeline.py` | Lazy accessor and the `ImagePipeline` protocol. |
| `inference.py` | `generate(request, pipeline) -> GenerationResult`. Pure, injectable. |
| `guardrails.py` | Cheap model-free prompt check. See [04](04-guardrails.md). |
| `storage.py` | Upload to S3-compatible storage, return a reference. Base64 fallback behind the same interface. |
| `schemas.py` | `GenerationRequest`, `GenerationResult`, error envelope. |
| `settings.py` | `pydantic-settings`. The only module reading the environment. |
| `errors.py` | Domain exceptions and error codes. |

## The lazy pipeline

Two requirements that a naive implementation cannot satisfy together: the pipeline must load once per worker rather than once per job, and `handler.py` must be importable without a GPU.

```python
_pipeline: ImagePipeline | None = None

def get_pipeline() -> ImagePipeline:
    """Return the process-wide FLUX pipeline, loading it on first use."""
    global _pipeline
    if _pipeline is None:
        _pipeline = _load_pipeline()
    return _pipeline

def set_pipeline(pipeline: ImagePipeline | None) -> None:
    """Replace the process-wide pipeline. Tests only."""
    global _pipeline
    _pipeline = pipeline
```

A module-level constant satisfies the first and breaks the second. The accessor satisfies both.

**Warm-up** happens in `if __name__ == "__main__":`, before `runpod.serverless.start()`. Production workers load during container start rather than during the first billed job; importing the module — which is what tests do — never touches a GPU. No configuration flag: the entrypoint and the import path are simply different.

`ImagePipeline` is a locally-declared `Protocol`, not a direct `FluxPipeline` dependency. `diffusers` is not usefully typed, so under `mypy strict` everything from it arrives as `Any` and trips `warn_return_any` at the first boundary crossing. The protocol is what makes strict mode viable, and it is what tests fake.

## Input contract

| Field | Type | Default | Constraint |
|---|---|---|---|
| `prompt` | `str` | required | non-blank; length bounded by the T5 token limit (below) |
| `width` | `int` | 1024 | 256–1536, snapped down to ×16 |
| `height` | `int` | 1024 | 256–1536, snapped down to ×16 |
| `num_inference_steps` | `int` | 28 | 1–50 |
| `guidance_scale` | `float` | 3.5 | 0–20 |
| `seed` | `int \| None` | `None` | randomised when absent, always echoed |
| `output_format` | `"png" \| "jpeg"` | `"png"` | — |
| `correlation_id` | `str \| None` | `None` | bound into the log context |

### Why there is no negative prompt

Not because the pipeline refuses one — current `FluxPipeline.__call__` accepts `negative_prompt`, `negative_prompt_2`, and `true_cfg_scale`. The reason is cost and coherence.

Standard Stable Diffusion performs classifier-free guidance by running **two forward passes per step**, one conditioned on the prompt and one on the negative, then extrapolating away from the negative. FLUX.1-dev is **guidance-distilled**: it was trained to internalise that behaviour, takes `guidance_scale` as an embedding input, and runs **one pass per step**. The distillation is the source of its speed, and it leaves no unconditional pass for a negative prompt to act against.

Setting `true_cfg_scale > 1` restores real CFG, and with it:

- **Roughly 2× latency and 2× cost** — the distillation speedup is surrendered entirely.
- **Two interacting guidance controls.** The distilled `guidance_scale` embedding and real `true_cfg_scale` operate at once. They are not independent and there is no principled way to tune both.
- **No reliable quality gain.** The model was not trained to be sampled with real CFG at inference; output can degrade rather than improve.

So the field is omitted, and this is the reason. Adding it later is a schema change and a pass-through, not a redesign.

[09](09-benchmarks.md) measures the 2× claim rather than repeating it.

### Prompt length

Non-blank, capped at 2000 characters.

FLUX runs T5 at `max_sequence_length=512` tokens. A character cap is a proxy for that, not a substitute: an unusually dense 2000-character prompt can still exceed 512 tokens, and `diffusers` truncates the excess silently.

Validating exactly would mean loading the T5 tokenizer, which is a dependency to inject and fake in tests for a case that is rare in practice. For this scope the character cap plus a documented note is the right trade. The limitation is recorded in [08](08-production-readiness.md) rather than hidden.

### Dimension snapping

Dimensions snap down to a multiple of 16 — the FLUX latent space is 16× downsampled — and the effective values are returned on the result. Recent `diffusers` already rounds and warns; verify at implementation time and delegate rather than duplicate.

Snapping is silent by design, unlike prompt truncation: the difference between 1000px and 992px is invisible, whereas the difference between a full prompt and half of one is the whole request.

## Output contract

```json
{
  "image_url": "https://.../8f3a2c.png",
  "image_base64": null,
  "format": "png",
  "seed": 42,
  "width": 1024, "height": 1024,
  "num_inference_steps": 28, "guidance_scale": 3.5,
  "model_version": "black-forest-labs/FLUX.1-dev@<revision>",
  "timings": {"inference_s": 21.4, "upload_s": 0.4}
}
```

**A storage reference is the default, and the reason is architectural rather than about payload size.**

A ~200-byte result can be *pushed*. A 2.7MB base64 blob cannot — it is unusable in an SSE event, hostile in a webhook body, and wasteful in a polling response that a client may fetch several times before the job finishes. Returning a reference is what keeps every event-driven facade in [03](03-facades.md) available; returning base64 forecloses all of them at the point the worker serialises its result, which is the worst place to make that decision.

It also decouples result *delivery* from result *notification*. The URL can be handed to a browser, a CDN, or another service without the gateway proxying image bytes it has no reason to touch.

Exactly one of `image_url` and `image_base64` is populated. Base64 remains as the zero-infrastructure fallback for a local run with no bucket configured, which keeps the worker demonstrable in isolation.

### Storage backend

RunPod S3-compatible network volume storage.

Chosen because it keeps everything on one platform — no additional vendor, no extra signup, no cross-cloud egress — and because a network volume is already being provisioned for the deployment variant in [06](06-build-deploy.md), so the storage story and the weights story share one piece of infrastructure.

The cost is the same region pinning the volume variant carries. `storage.py` speaks the S3 API through a narrow interface, so swapping to Cloudflare R2 or S3 is a settings change: bucket, endpoint, credentials.

Objects are written under a content-addressed key and treated as ephemeral. Retention policy is out of scope and recorded in [08](08-production-readiness.md).

`model_version` pins the output to what produced it, including the resolved revision. Without it, an image cannot be correlated to a model version after the fact. The revision is pinned at build time by `fetch_weights.py` and baked in, so it is a fact rather than a label.

## Errors

Codes are defined in [02](02-gateway-core.md#error-codes) and shared across tiers.

`torch.cuda.OutOfMemoryError` is caught explicitly: log with context, `torch.cuda.empty_cache()`, return the envelope with `refresh_worker: True`. VRAM fragmentation outlives `empty_cache()`, so the worker is retired rather than trusted with the next job.

Any other pipeline exception maps to `INFERENCE_FAILED` with the detail logged and not returned.

## Endpoint configuration

| Setting | Value | Reason |
|---|---|---|
| GPU | L40S 48GB | bf16 FLUX needs ~24-26GB steady, more at 1536². 24GB is too tight to be safe. |
| Workers | min 0, max 3 | Scale to zero. 3 demonstrates concurrency. |
| FlashBoot | on | Keeps VRAM resident between jobs. |
| Idle timeout | 60s | Long enough that a demo sequence stays warm. |
| Execution timeout | 300s | Above worst-case 50-step 1536². |
| `concurrency_modifier` | 1 | GPU-bound; a second concurrent job only causes VRAM contention. |

**Provisional.** L40S is the reasoned starting point. The final recommendation comes from measured Phase 2b numbers across at least L40S and A100 80GB.

## Open items

| Item | Resolved in |
|---|---|
| Does current `diffusers` already snap dimensions? | Phase 1, by reading the installed source |
| Cold start, warm latency, cost per image | Phase 2b, measured |
| Cost of `true_cfg_scale > 1` versus the 2× estimate | Phase 2b, measured |

## Weight path

The worker resolves weights from `WEIGHTS_PATH` in settings. It does not know or care which deployment variant it is running under — baked into the image or mounted from a network volume ([06](06-build-deploy.md)). One code path, two deployments.

Startup fails fast if `WEIGHTS_PATH` does not exist. Without that check a misconfigured volume mount falls through to downloading 33GB from HuggingFace on every cold start, which presents as "slow" rather than "broken" and can survive a whole benchmark run undetected.
