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
  "image_base64": "iVBORw0KGgoAAAANS...",
  "storage_key": null,
  "format": "png",
  "seed": 42,
  "width": 1024, "height": 1024,
  "num_inference_steps": 28, "guidance_scale": 3.5,
  "model_version": "black-forest-labs/FLUX.1-dev@<revision>",
  "timings": {"inference_s": 21.4, "encode_s": 0.3}
}
```

**Base64 is the default, and the platform decides it.**

RunPod serverless exposes a fixed surface — `/run`, `/status/{job_id}`, `/stream`, `/cancel`, `/health`. Custom routes cannot be added. So `GET /status/{job_id}` returns **the handler's return value verbatim**, and there is nowhere else a caller can obtain a result from.

That makes the rule simple: whatever the handler does not put in its output does not exist as far as a direct caller is concerned. A storage key in the output hands the reviewer a key they cannot resolve, because resolving it requires storage credentials or a gateway they are not running. The brief asks the endpoint to *return a generated image*; only base64 satisfies that unconditionally.

At 1024² PNG this is ~2MB, ~2.7MB encoded. Measured against the response ceiling in [09](09-benchmarks.md) at 1536², with JPEG as the fallback if it binds.

### Storage: opt-in, not default

When `STORAGE_ENABLED` is set, the worker additionally uploads to a RunPod network volume via its S3-compatible API and populates `storage_key`. `image_base64` is then omitted, since the caller has a reference and a 2.7MB duplicate helps nobody.

This exists because a ~200-byte result can be *pushed* and a 2.7MB blob cannot — it is unusable in an SSE event, hostile in a webhook body, and wasteful in a polling response fetched repeatedly. Keeping the capability preserves every event-driven facade in [03](03-facades.md) as a real option.

But it is **off by default**, because it only works for a caller who can resolve the key: the gateway, using server-side credentials, via `GET /v1/jobs/{id}/image`. A direct caller cannot. Making the graded path depend on the ungraded tier was the wrong trade, and the default now reflects that.

`storage.py` speaks S3 through a narrow interface, so Cloudflare R2 or AWS S3 is a settings change: bucket, endpoint, credentials. Objects are content-addressed and ephemeral; retention is recorded in [08](08-production-readiness.md).

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

## Progress reporting

Owning the image means the worker is not a black box for 20-25 seconds. `diffusers` fires `callback_on_step_end` after each denoising step; `runpod.serverless.progress_update(job, ...)` writes into the job record mid-execution, where `GET /status/{id}` can read it while the job is still `IN_PROGRESS`.

```python
def _on_step(pipe, step: int, timestep: int, kwargs: dict[str, Any]) -> dict[str, Any]:
    runpod.serverless.progress_update(
        job, {"step": step + 1, "total": total_steps,
              "percent": round(100 * (step + 1) / total_steps)}
    )
    return kwargs
```

Without this, every poll during generation returns `IN_PROGRESS` and nothing else — the client cannot distinguish a job three seconds in from one about to finish, and cannot show a progress bar or estimate anything.

It also changes what the downstream facades are worth. A stream carrying one completion event is decorative; a stream carrying live progress is the reason SSE exists ([03](03-facades.md)).

Progress is emitted at most once per step and is a small dict. The callback runs between steps on the GPU thread, so it must stay trivial — anything expensive here is paid 28 times per image.

### Not built: preview images

The `/stream/{job_id}` endpoint plus a generator handler could yield intermediate images — VAE-decode the latents every few steps into a small JPEG, giving progressive previews.

It is not built because of a structural obstacle rather than a lack of appetite. `pipe(...)` is a blocking call and `callback_on_step_end` fires inside it, so **a generator handler cannot yield from the callback**. Real streaming needs the pipeline running on a thread with the callback pushing to a queue that the generator drains — plus a VAE decode costing ~50-150ms per preview on the same GPU doing the work.

`progress_update` has no such constraint: it is an ordinary call, invocable from anywhere, which is why it is the version that ships.

## Weight path

The worker resolves weights from `WEIGHTS_PATH` in settings. It does not know or care which deployment variant it is running under — baked into the image or mounted from a network volume ([06](06-build-deploy.md)). One code path, two deployments.

Startup fails fast if `WEIGHTS_PATH` does not exist. Without that check a misconfigured volume mount falls through to downloading 33GB from HuggingFace on every cold start, which presents as "slow" rather than "broken" and can survive a whole benchmark run undetected.
