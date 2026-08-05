# Engineering Standards

Binding conventions for this repository. Applies to both packages (`worker/`, `gateway/`).

## 1. Precedence

When rules conflict, higher wins:

1. `~/.claude/CLAUDE.md` (personal global instructions)
2. This document
3. Team standards skills (`coding-standards`, `testing-standards`, `documentation-standards`)

### Resolved conflicts

| Topic | Team standard | Resolution here | Reason |
|---|---|---|---|
| Docstrings | Google-style with Args/Returns/Raises/Examples | **Team standard applies in full.** See §10 | Explicit direction for this repo, overriding the one-line default in `CLAUDE.md` |
| Comments | Explain WHY not WHAT | Unchanged, but **default to none** | `CLAUDE.md` is stricter. Docstrings carry the explanation; inline comments stay rare |
| Commit messages | — | No `Claude-Session:` trailer, no AI attribution | `CLAUDE.md` |

## 2. Toolchain

| Concern | Tool | Non-negotiable |
|---|---|---|
| Packaging / envs | `uv` | Yes |
| Format + lint | `ruff` | Yes |
| Types | `mypy` | Yes |
| Tests | `pytest` | Yes |
| Pre-commit | `pre-commit` | Yes — every package |
| Python | 3.11 | Yes — matches `target-version`, and is the last version with clean `torch`/`diffusers` wheel coverage at time of writing |

Each package owns its `pyproject.toml`. There is no shared dependency set: the worker must never depend on FastAPI or psycopg, and the gateway must never depend on `torch`. A cross-package import is a build failure, not a code smell.

### Ruff

```toml
[tool.ruff]
line-length = 88
target-version = "py311"

[tool.ruff.lint]
select = ["E", "W", "F", "I", "N", "UP", "B", "C4", "SIM", "D"]
ignore = [
    "E501",  # line length — the formatter owns this
    "D107",  # __init__ — Google convention documents Args in the class docstring
]

[tool.ruff.lint.per-file-ignores]
"__init__.py" = ["F401"]
"tests/**" = ["D"]

[tool.ruff.lint.isort]
known-first-party = ["worker"]   # "gateway" in the gateway package

[tool.ruff.lint.pydocstyle]
convention = "google"
```

### mypy

```toml
[tool.mypy]
python_version = "3.11"
strict = true
warn_unreachable = true
plugins = ["pydantic.mypy"]

[[tool.mypy.overrides]]
module = ["diffusers.*", "runpod.*", "transformers.*"]
ignore_missing_imports = true
```

`strict = true` from the first commit. Retrofitting strictness onto a finished codebase does not happen.

## 3. Repository structure

- `src/` layout. The package is importable only after install, which prevents accidental reliance on CWD.
- `tests/` mirrors `src/`, split by pyramid tier: `tests/unit/`, `tests/integration/`, `tests/e2e/`.
- One domain per module. A module over ~200 lines is a prompt to re-read its boundary.
- No wildcard imports. No circular imports.
- Every package has a `README.md`, a `pyproject.toml`, and a `Dockerfile` if it ships as an image.

### Layering

Dependencies point inward. `core/` is the innermost ring.

```
api/  adapters/  workers/     ← may import core/
        │
        ▼
      core/                   ← imports nothing from the outer rings
```

`gateway/src/gateway/core/` must not import FastAPI, psycopg, or httpx. It defines `Protocol` interfaces; `adapters/` implements them. This is what makes an additional transport facade a small change rather than a rewrite, and it is the single structural rule most worth enforcing in review.

## 4. Types

- Complete annotations on every function signature, including `-> None`.
- Modern syntax: `str | None`, `list[dict[str, int]]`. No `Optional`, no `typing.List`.
- `Any` requires an inline justification comment.
- Public boundaries (`core/` interfaces, API schemas, handler I/O) carry full type coverage; `strict` mode enforces it.

## 5. Data structures

| Use | When |
|---|---|
| Pydantic `BaseModel` | Validation, parsing, API schemas, settings |
| `@dataclass(frozen=True)` | Internal containers with no validation need |
| Plain class | State plus multiple methods operating on it |

Do not write a class for a single method called once. Use a function.

All external input — RunPod job payloads, HTTP request bodies, environment — is parsed into a Pydantic model at the boundary. Nothing downstream of the boundary handles a raw `dict`.

Configuration is `pydantic-settings`, loaded once, injected. No `os.getenv` outside `settings.py`.

## 6. Complexity

| Metric | Limit |
|---|---|
| Function length | 15–20 lines target, 50 hard |
| Parameters | 5 |
| Branches | 12 |
| Module length | ~200 lines soft |

Early returns over nested conditionals. Named constants over magic numbers. Dict lookup over `if`/`elif` chains. No mutable default arguments.

## 7. Error handling

Fail fast. Validate in `__init__` and at boundaries, not deep in call stacks.

- Raise for invalid input, contract violations, unrecoverable state.
- Return `None` for expected absence.
- Specific exception types only. Catch narrowly. `raise ... from e` to preserve the chain.
- `finally` for cleanup.

### Domain exceptions

Each package defines its exceptions in one module (`core/errors.py`, `worker/errors.py`). No bare `Exception` raises.

### Error contract

Every error surfaced to a caller carries a stable machine-readable `code`, a human `message`, and — where a next action exists — a `suggestion`. Callers include automated agents, which cannot parse prose.

```python
{"error": {"code": "INVALID_DIMENSIONS", "message": "...", "suggestion": "..."}}
```

Never expose stack traces, internal paths, or credentials to a caller. Log the detail, return the code plus a correlation ID.

### GPU-specific

`torch.cuda.OutOfMemoryError` is caught explicitly, VRAM cleared, and the job returned with `refresh_worker: True`. A worker that has OOM'd is not trusted with the next job — VRAM fragmentation outlives `empty_cache()`.

## 8. Logging

`structlog`, JSON renderer, flat key-value pairs. No nested objects. Event names are `snake_case`.

Every entry carries `timestamp`, `level`, `event`, `correlation_id`.

The correlation ID originates at the gateway (`X-Correlation-ID` header or generated), is passed to the worker in the job input, and is bound into the worker's log context. One ID traces a request from HTTP call through the GPU and back. This is the only practical way to debug a distributed serverless path and is not optional.

Log: request completion with duration, state transitions, errors with context, resource metrics.
Never log: tokens, API keys, credentials, connection strings. `HF_TOKEN` and `RUNPOD_API_KEY` must never appear in output.

Prompts are user content. Log a length and a hash, not the text.

### Serverless constraint

RunPod serverless supports no sidecars. There is no log shipper, no metrics agent. The handler emits to stdout and that is the entire observability surface from inside the container. Anything not emitted there is only observable via the RunPod API from outside. Design accordingly.

## 9. Testing

Pyramid: 70% unit, 20% integration, 10% E2E.

Coverage: 80% minimum on new code, 100% on input validation and the error-code contract.

Naming: `test_<unit>_<scenario>_<expected_result>`. Arrange-Act-Assert. `pytest.mark.parametrize` over duplicated test bodies. Mock at boundaries — the RunPod API, the database, the diffusion pipeline — never internals.

### The GPU rule

**No test in `tests/unit/` or `tests/integration/` may require a GPU, network access, or model weights.** CI has none of the three.

This is a design constraint, not a testing preference: it forces the pipeline behind a lazily-initialised, injectable accessor rather than a module-level global. A `handler.py` that cannot be imported on a laptop is a `handler.py` that cannot be tested, and the fix is architectural.

GPU-dependent checks live in `tests/e2e/`, marked `@pytest.mark.gpu`, deselected by default, run manually against a live endpoint.

```toml
[tool.pytest.ini_options]
addopts = "-m 'not gpu' --strict-markers"
markers = ["gpu: requires a live GPU or deployed endpoint"]
```

## 10. Documentation

### Docstrings

Full Google-style on every public module, class, and function, and on any non-trivial private function. Sections required when applicable: summary line, extended description, `Args`, `Returns`, `Raises`, `Example`.

```python
def generate(request: GenerationRequest, pipeline: FluxPipeline) -> GenerationResult:
    """Run FLUX.1-dev inference for a single prompt.

    Dimensions are snapped down to the nearest multiple of 16 before inference,
    since the FLUX latent space is 16x downsampled. The snapped values are
    returned on the result so callers see what was actually rendered.

    Args:
        request: Validated generation parameters. `seed` is populated by the
            caller when absent so the value is always recorded.
        pipeline: An initialised FLUX pipeline already resident on the GPU.

    Returns:
        The rendered image with the effective seed, dimensions, and wall-clock
        inference duration.

    Raises:
        torch.cuda.OutOfMemoryError: VRAM exhausted. The caller is responsible
            for clearing the cache and requesting a worker refresh.

    Example:
        >>> result = generate(GenerationRequest(prompt="a red fox"), pipeline)
        >>> result.width
        1024
    """
```

Rules:

- Summary line is one sentence, imperative mood, ends with a period (`D401`, `D415`).
- Document every parameter. `D417` is enabled and will fail the build on an omission.
- `Example` is required on anything a consumer calls directly — API schemas, `core/` interfaces, the handler contract. It is optional on internal helpers.
- Omit a section only when it does not apply. A function returning `None` has no `Returns`.
- Optionally enable ruff's `DOC` rules (`DOC201` missing `Returns`, `DOC501` missing `Raises`) for machine-enforced completeness. They are preview-gated, so treat them as advisory until stable.

### Other

- **Comments:** only a non-obvious *why*. Default to none — the docstring is where explanation belongs.
- **TODOs:** `# TODO(name, YYYY-MM-DD): actionable description`. No bare `TODO`.
- **README:** every package. Root `README.md` is the submission artifact — what it is, how to run it, measured results.
- **API docs:** generated from FastAPI/Pydantic. Every endpoint carries a `response_model` and realistic `examples` in `Field`. A schema without an example is incomplete.
- Docs change in the same commit as the code they describe.

### Numbers

Every latency, throughput, cost, and size figure in any document is either **measured** and labelled with the hardware and date it was measured on, or explicitly labelled an estimate. No unattributed numbers. A wrong performance figure is worse than an absent one.

## 11. Secrets and reproducibility

- Secrets reach the build via BuildKit `--mount=type=secret`, never `ARG`, never `ENV`, never a `COPY`'d file. An `ARG`-passed token is recoverable from image history.
- Secrets reach runtime via the RunPod endpoint environment, never the image.
- `.env` is git-ignored. `.env.example` is committed and lists every variable with a description and no value.
- Images are tagged with an immutable version. `latest` is never deployed to an endpoint.
- `uv.lock` is committed. Builds are reproducible or they are not builds.
- Generation accepts an explicit `seed` and echoes back the seed actually used, including when it was randomly chosen. Every output is reproducible.

## 12. Definition of done

A change is complete when all of the following pass. This is also the CI gate.

```
uv run ruff format --check .
uv run ruff check .
uv run mypy src/
uv run pytest --cov --cov-fail-under=80
```

Plus: docs updated in the same commit, no new `Any` without justification, no secret in the diff.

CI runs lint, types, and non-GPU tests only. It does not build the worker image — a ~45GB build exceeds the ~14GB disk on standard GitHub runners. Image builds happen on a RunPod GPU pod; the runbook documents the procedure.

## 13. Commits

Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `build:`, `ci:`.

State what changed and why. No AI attribution of any kind — no `Co-Authored-By`, no session URL, no "Generated with".
