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
| Commit messages | - | No `Claude-Session:` trailer, no AI attribution | `CLAUDE.md` |

## 2. Toolchain

| Concern | Tool | Non-negotiable |
|---|---|---|
| Packaging / envs | `uv` | Yes |
| Format + lint | `ruff` | Yes |
| Types | `mypy` | Yes |
| Tests | `pytest` | Yes |
| Pre-commit | `pre-commit` | Yes, every package |
| Python | 3.11 | Yes. Pinned: the worker image installs 3.11 via `uv` on `ubuntu:22.04`, matching the team `target-version`. Verify wheel availability for the pinned `torch`/`diffusers` before changing it |

Each package owns its `pyproject.toml`. There is no shared dependency set: the worker must never depend on FastAPI or psycopg, and the gateway must never depend on `torch`.

This is enforced by construction, not by convention: the two packages resolve into separate virtualenvs, so a cross-package import fails at collection time rather than passing review and breaking in the image. Do not add a root-level shared dependency group; it would silently remove the enforcement.

### Ruff

```toml
[tool.ruff]
line-length = 88
target-version = "py311"

[tool.ruff.lint]
select = [
    "E", "W", "F", "I", "N", "UP", "B", "C4", "SIM",  # style, bugs, imports
    "D",        # pydocstyle — see §10
    "T20",      # no print — see below
    "S",        # bandit — see §11
    "C90",      # cyclomatic complexity — see §6
    "PLR0913",  # too many arguments
    "PLR0915",  # too many statements
    "PLR2004",  # magic value comparison
]
ignore = [
    "E501",  # line length — the formatter owns this
    "D107",  # __init__ — Google convention documents Args in the class docstring
]

[tool.ruff.lint.per-file-ignores]
"__init__.py" = ["F401"]
"tests/**" = [
    "D",        # test names carry the intent; an Args block on a test is noise
    "S101",     # assert is the point of a test
    "PLR2004",  # literal expected values are clearer than named constants here
]

[tool.ruff.lint.mccabe]
max-complexity = 12

[tool.ruff.lint.pylint]
max-args = 5
max-statements = 50

[tool.ruff.lint.isort]
known-first-party = ["worker"]   # "gateway" in the gateway package

[tool.ruff.lint.pydocstyle]
convention = "google"
```

The `mccabe` and `pylint` limits are the §6 thresholds made executable. They are configuration, not aspiration: a function over 50 statements or 5 parameters fails the build.

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

`T20` bans `print`. In a serverless worker, stdout *is* the observability channel (§8): an unstructured `print` is a log line that no query will ever find.

`S` (bandit) enforces §11 by tooling rather than by review: hardcoded credentials, unsafe subprocess use, weak hashing. `S101` is exempted in tests, where `assert` is the mechanism rather than a smell.

The `ignore_missing_imports` overrides are a deliberate, contained leak: `diffusers`, `runpod`, and `transformers` are not usefully typed, so everything from them arrives as `Any`, and under `strict` that surfaces as `warn_return_any` errors the moment such a value crosses a function boundary. The fix is a local `Protocol` wrapping the third-party surface actually used, not `type: ignore` at the call sites. This is why §4's rule against unjustified `Any` holds even though these three modules are exempted from import checking.

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

`gateway/src/gateway/core/` must not import FastAPI, psycopg, or httpx. It defines `Protocol` interfaces; `adapters/` implements them. This is what makes an additional transport facade a small change rather than a rewrite.

It is enforced by `import-linter`, not by review. A stray `from fastapi import HTTPException` in `core/service.py` passes ruff, mypy, and every test while quietly welding the domain logic to one transport; the cost surfaces only when the second facade turns out to be a rewrite.

```toml
[tool.importlinter]
root_package = "gateway"

[[tool.importlinter.contracts]]
name = "core depends on nothing outward"
type = "forbidden"
source_modules = ["gateway.core"]
forbidden_modules = [
    "gateway.api", "gateway.adapters", "gateway.workers",
    "fastapi", "httpx", "psycopg", "sqlalchemy",
]
```

`lint-imports` runs as part of `make check` (§12).

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

All external input (RunPod job payloads, HTTP request bodies, environment) is parsed into a Pydantic model at the boundary. Nothing downstream of the boundary handles a raw `dict`.

Configuration is `pydantic-settings`, loaded once, injected. No `os.getenv` outside `settings.py`.

## 6. Complexity

| Metric | Limit | Enforced by |
|---|---|---|
| Function length | 15-20 lines target, 50 hard | `PLR0915` (`max-statements = 50`) |
| Parameters | 5 | `PLR0913` (`max-args = 5`) |
| Cyclomatic complexity | 12 | `C901` (`max-complexity = 12`) |
| Magic numbers | none | `PLR2004` |
| Module length | ~200 lines soft | Review |

Early returns over nested conditionals. Named constants over magic numbers. Dict lookup over `if`/`elif` chains. No mutable default arguments.

Only the module-length guideline is review-enforced; the rest fail the build.

## 7. Error handling

Fail fast. Validate in `__init__` and at boundaries, not deep in call stacks.

- Raise for invalid input, contract violations, unrecoverable state.
- Return `None` for expected absence.
- Specific exception types only. Catch narrowly. `raise ... from e` to preserve the chain.
- `finally` for cleanup.

### Domain exceptions

Each package defines its exceptions in one module (`core/errors.py`, `worker/errors.py`). No bare `Exception` raises.

### Error contract

Every error surfaced to a caller carries a stable machine-readable `code`, a human `message`, and, where a next action exists, a `suggestion`. Callers include automated agents, which cannot parse prose.

```python
{"error": {"code": "INVALID_DIMENSIONS", "message": "...", "suggestion": "..."}}
```

Never expose stack traces, internal paths, or credentials to a caller. Log the detail, return the code plus a correlation ID.

### GPU-specific

`torch.cuda.OutOfMemoryError` is caught explicitly, VRAM cleared, and the job returned with `refresh_worker: True`. A worker that has OOM'd is not trusted with the next job: VRAM fragmentation outlives `empty_cache()`.

## 8. Logging

`structlog`, JSON renderer, flat key-value pairs. No nested objects. Event names are `snake_case`.

Every entry carries `timestamp`, `level`, `event`, `correlation_id`.

The correlation ID originates at the gateway (`X-Correlation-ID` header or generated), is passed to the worker in the job input, and is bound into the worker's log context. One ID traces a request from HTTP call through the GPU and back. This is the only practical way to debug a distributed serverless path and is not optional.

Log: request completion with duration, state transitions, errors with context, resource metrics.
Never log: tokens, API keys, credentials, connection strings. `HF_TOKEN` and `RUNPOD_API_KEY` must never appear in output.

Prompts are user content, but this is an image-generation service: a bug report about a bad image is unanswerable if the prompt was never recorded. Log the first 80 characters plus the full length: enough to reproduce and triage, short enough that a long prompt cannot smuggle a wall of personal data into the log store.

```python
logger.info(
    "generation_started",
    prompt_preview=prompt[:80],
    prompt_length=len(prompt),
)
```

### Serverless constraint

RunPod serverless supports no sidecars: the handler emits to stdout and that is the entire observability surface from inside the container. Anything not emitted there is only observable via the RunPod API from outside. Design accordingly.

## 9. Testing

Pyramid: 70% unit, 20% integration, 10% E2E.

Coverage: 80% minimum on new code, 100% on input validation and the error-code contract.

Naming: `test_<unit>_<scenario>_<expected_result>`. Arrange-Act-Assert. `pytest.mark.parametrize` over duplicated test bodies. Mock at boundaries (the RunPod API, the database, the diffusion pipeline), never internals.

### The GPU rule

**No test in `tests/unit/` or `tests/integration/` may require a GPU, model weights, or a call to an external network service.** CI has none of the three.

"External network" is the operative word. Integration tests may start local containers (Postgres via testcontainers is expected) because that is a local dependency CI can provide. What they may not do is reach HuggingFace, the RunPod API, or any other third party: those make the suite slow, flaky, and dependent on someone else's uptime and on credentials CI should not hold.

The GPU half is a design constraint, not a testing preference: it forces the pipeline behind a lazily-initialised, injectable accessor rather than a module-level global ([`docs/DESIGN.md`](docs/DESIGN.md) §16).

GPU-dependent checks live in `tests/e2e/`, marked `@pytest.mark.gpu`, deselected by default, run manually against a live endpoint.

```toml
[tool.pytest.ini_options]
addopts = "-m 'not gpu' --strict-markers"
markers = ["gpu: requires a live GPU or deployed endpoint"]
```

Doctests are a separate invocation, scoped to the modules where examples are required (§10):

```
# gateway
uv run pytest --doctest-modules \
  src/gateway/core/models.py src/gateway/core/protocols.py \
  src/gateway/core/service.py \
  src/gateway/adapters/runpod_client.py src/gateway/adapters/guardrails.py

# worker
uv run pytest --doctest-modules \
  src/worker/schemas.py src/worker/guardrails.py src/worker/errors.py
```

`make doctest` and the CI doctest jobs run exactly these lists; they are kept in step deliberately, so a green `make check` means CI's doctests are green too.

Scoping matters: `--doctest-modules` across the whole package would import GPU-touching modules at collection time and break the rule above. The gateway list covers all of `core/`, `service.py` included, plus the two adapters carrying runnable examples. The worker list is boundary modules only, for the same reason: `pipeline.py`, `inference.py` and `handler.py` reach for `torch`.

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
    """
```

No `Example` here: `generate` needs a GPU-resident pipeline, so any `>>>` block would be a doctest that can never run. A function whose dependencies cannot be constructed in a docstring gets prose in the extended description instead.

Where an example *can* run, it is a real doctest and is executed:

```python
def snap_to_multiple(value: int, multiple: int = 16) -> int:
    """Round a dimension down to the nearest multiple.

    Args:
        value: The requested dimension in pixels.
        multiple: The required factor. FLUX latents are 16x downsampled.

    Returns:
        The largest multiple of `multiple` not exceeding `value`.

    Example:
        >>> snap_to_multiple(1000)
        992
        >>> snap_to_multiple(1024)
        1024
    """
```

Rules:

- Summary line is one sentence, imperative mood, ends with a period (`D401`, `D415`).
- Document every parameter. `D417` is enabled and will fail the build on an omission.
- Omit a section only when it does not apply. A function returning `None` has no `Returns`.
- **`Example` is required only where it is genuinely runnable**: pure functions, schema construction, and validation. It is forbidden where it would be a doctest that cannot execute. An example that never runs is documentation that silently rots.
- Doctests are executed via `--doctest-modules` over the module lists in §9. If it is written as `>>>`, it is verified.
- Optionally enable ruff's `DOC` rules (`DOC201` missing `Returns`, `DOC501` missing `Raises`) for machine-enforced completeness. They are preview-gated, so treat them as advisory until stable.

### Other

- **Comments:** only a non-obvious *why*. Default to none; the docstring is where explanation belongs.
- **TODOs:** `# TODO(name, YYYY-MM-DD): actionable description`. No bare `TODO`.
- **README:** every package. Root `README.md` is the submission artifact: what it is, how to run it, measured results.
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
make check
```

which runs, for each package independently (there is no root virtualenv, so these cannot be run from the repo root):

```
uv run ruff format --check .
uv run ruff check .
uv run mypy src/
uv run lint-imports              # gateway only, see §3
uv run pytest -q --cov --cov-fail-under=80
uv run pytest --doctest-modules <scoped paths>   # see §9
```

plus, over the CLI tools that live outside both packages (`client/`, `scripts/`, `benchmarks/`, which have no virtualenv of their own): `ruff format --check`, `ruff check`, `mypy`, and `pytest scripts/tests`.

Plus: docs updated in the same commit, no new `Any` without justification, no secret in the diff.

`--cov-fail-under=80` measures the whole package, which is stricter than the "80% of new code" in §9 and equivalent to it in a new repository. Keep the whole-package gate; if it ever becomes the binding constraint, that is a signal about the untested code already there, not a reason to relax it. The 100% requirement on validation and the error-code contract is enforced in review, not by the tool.

`ci.yml` runs lint, types, doctests and non-GPU tests only; it builds no image. `deploy.yml` builds and pushes the **slim** worker image (2.92GB) on a standard GitHub runner, then applies the endpoint config and runs the e2e suite against it. The **baked** image is still never built in CI: ~45GB exceeds the ~14GB of runner disk, so it stays `make build-baked` on a machine that can hold it.

## 13. Commits

Conventional Commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `build:`, `ci:`.

State what changed and why. No AI attribution of any kind: no `Co-Authored-By`, no session URL, no "Generated with".
