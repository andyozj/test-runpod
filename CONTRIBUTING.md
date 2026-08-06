# Contributing

Style, coverage and complexity rules live in [`STANDARDS.md`](STANDARDS.md) and are not repeated here. This file covers setup and the gates.

## Prerequisites

| Tool | Why | Version |
|---|---|---|
| `uv` | Packaging and environments. Also provisions the interpreter | Any recent release (developed on 0.10.8) |
| Python 3.11 | `requires-python = ">=3.11,<3.12"` in both `pyproject.toml` files | `uv sync` installs it; no system Python needed |
| GNU Make | Every gate has a target | 3.81 works (macOS system make) |
| Docker with buildx | Image builds only (`make build-slim`, `compose.yaml`). Not needed for tests | Any release with `buildx` |

`uvx` is used for the CLI-tool lint and pins `ruff@0.7.4` — nothing to install by hand.

## Setup

```bash
make install   # uv sync --all-groups in worker/ and gateway/
make check     # the full gate
```

There is no root virtualenv. The two packages resolve independently, which is what stops a cross-package import (`STANDARDS.md` §2), so every command below runs from inside `worker/` or `gateway/`.

`make check` runs, in order:

| Target | Command |
|---|---|
| `format-check` | `uv run ruff format --check .` per package |
| `lint` | `uv run ruff check .` per package, plus `uvx ruff@0.7.4` over `client/ scripts/ benchmarks/` |
| `types` | `uv run mypy src/` per package |
| `imports` | `uv run lint-imports` (gateway only — the `core/` layering contract) |
| `test` | `uv run pytest -q --cov --cov-fail-under=80` per package |
| `doctest` | `--doctest-modules` over 3 worker modules and 5 gateway modules |
| `types-tools` | `uvx --with types-PyYAML mypy scripts client benchmarks --ignore-missing-imports` |
| `test-tools` | `pytest -q scripts/tests` |

`make help` lists every target.

## Pre-commit

```bash
pre-commit install
```

Hooks in [`.pre-commit-config.yaml`](.pre-commit-config.yaml):

- `check-json`, `check-yaml`, `check-toml`, `check-merge-conflict`, `detect-private-key`, `end-of-file-fixer`, `trailing-whitespace`
- `ruff --fix` and `ruff-format` (v0.7.4) over `worker|gateway|client|scripts|benchmarks`
- local: `mypy (worker)`, `mypy (gateway)`, `import-linter (gateway)`

The hooks are a subset of `make check`. Passing them does not mean CI passes; run `make check` before pushing.

## Tests

```bash
cd worker   && uv run pytest -q --cov --cov-fail-under=80   # 97 selected, 7 gpu deselected
cd gateway  && uv run pytest -q --cov --cov-fail-under=80   # 240 collected
make test-tools                                             # scripts/tests
```

Both packages set `addopts = "-m 'not gpu' --strict-markers"`. The GPU-marked suite is `worker/tests/e2e/test_endpoint.py` (module-level `pytestmark = pytest.mark.gpu`) and runs against the live endpoint:

```bash
cd worker && RUNPOD_API_KEY=... RUNPOD_ENDPOINT_ID=... uv run pytest -m gpu tests/e2e
```

No test in `tests/unit/` or `tests/integration/` may require a GPU, model weights, or an external network service (`STANDARDS.md` §9).

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push, every pull request, and as a `workflow_call` from `deploy.yml`. Two jobs, both required:

- **`check`** — matrix over `worker` and `gateway`: sync, format, lint, types, `lint-imports` (gateway), tests with the 80% coverage gate, doctests.
- **`tools`** — `make lint-tools`, mypy over `scripts client benchmarks`, `pytest scripts/tests`.

CI builds no image. Third-party actions are pinned to commit SHAs.

## Commits

Conventional Commits, scoped where it helps. Real examples from the log:

```
fix(gateway): stop the reconciler double-submitting a job mid-submit
ci: pin third-party actions to commit SHAs, wire tools mypy and tests
test(scripts): add apply_endpoint.py pure-function tests
```

State what changed and why. No AI attribution of any kind (`STANDARDS.md` §13).

Docs change in the same commit as the code they describe.
