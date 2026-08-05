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
| `storage.py` | Object-storage upload, base64 fallback. |
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

FLUX.1-dev is guidance-distilled and takes no negative prompt. The schema omits the field rather than accepting and silently ignoring it.

### Prompt length is a token limit, not a character limit

FLUX runs T5 at `max_sequence_length=512` **tokens**. A character cap is the wrong unit: a prompt under any plausible character limit can still exceed 512 tokens, and `diffusers` truncates it silently. The user then gets an image missing half their intent and no indication why.

Therefore: validate with the actual tokenizer, and when the prompt exceeds the limit, **fail with `INVALID_PROMPT`** rather than truncate. Silent truncation is the failure mode; an explicit error is recoverable. The error carries the token count and the limit so a caller can shorten deterministically.

The tokenizer is small and CPU-only. It loads independently of the diffusion pipeline, so the check stays inside the GPU-free import path and remains unit-testable.

### Dimension snapping

Dimensions snap down to a multiple of 16 — the FLUX latent space is 16× downsampled — and the effective values are returned on the result. Recent `diffusers` already rounds and warns; verify at implementation time and delegate rather than duplicate.

Snapping is silent by design, unlike prompt truncation: the difference between 1000px and 992px is invisible, whereas the difference between a full prompt and half of one is the whole request.

## Output contract

```json
{
  "image_url": "https://.../8f3a.png",
  "image_base64": null,
  "format": "png",
  "seed": 42,
  "width": 1024, "height": 1024,
  "num_inference_steps": 28, "guidance_scale": 3.5,
  "model_version": "black-forest-labs/FLUX.1-dev@<revision>",
  "timings": {"inference_s": 21.4, "upload_s": 0.4}
}
```

Exactly one of `image_url` and `image_base64` is populated. `image_url` when object storage is configured — the default, and what keeps the response ~200 bytes. `image_base64` is the zero-infrastructure fallback for a local demo with no bucket.

`model_version` pins the output to what produced it. Without it, an image cannot be correlated to a model revision after the fact.

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
| Exact T5 token limit behaviour and tokenizer load cost | Phase 1 |
| Cold start, warm latency, cost per image | Phase 2b, measured |
