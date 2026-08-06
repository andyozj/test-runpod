PACKAGES := worker gateway

# Versioning: the most recent v* tag names the version; the commit SHA makes
# the image tag immutable (apply_endpoint.py refuses moving tags). Bump by
# tagging: `git tag -a v0.2.0 -m ... && git push --tags`.
# Override on the command line for other registries or explicit versions:
#   make build-slim IMAGE=ghcr.io/you/flux-worker TAG=0.2.0-abc1234
VERSION ?= $(shell git describe --tags --abbrev=0 --match 'v*' 2>/dev/null | sed 's/^v//' || true)
ifeq ($(VERSION),)
VERSION := 0.0.0-untagged
endif
IMAGE ?= ghcr.io/andyozj/flux-worker
TAG ?= $(VERSION)-$(shell git rev-parse --short HEAD)

.DEFAULT_GOAL := help

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: print-tag
print-tag: ## Print the derived image tag (version-sha)
	@echo $(TAG)

.PHONY: install
install: ## Sync both package environments
	@for p in $(PACKAGES); do \
		[ -f $$p/pyproject.toml ] || continue; \
		echo "== $$p"; (cd $$p && uv sync --all-groups) || exit 1; \
	done

.PHONY: check
check: format-check lint types imports test ## Everything CI runs

.PHONY: format
format: ## Apply formatting
	@for p in $(PACKAGES); do \
		[ -f $$p/pyproject.toml ] || continue; \
		(cd $$p && uv run ruff format .) || exit 1; \
	done

.PHONY: format-check
format-check:
	@for p in $(PACKAGES); do \
		[ -f $$p/pyproject.toml ] || continue; \
		echo "== format $$p"; (cd $$p && uv run ruff format --check .) || exit 1; \
	done

.PHONY: lint
lint: lint-tools
	@for p in $(PACKAGES); do \
		[ -f $$p/pyproject.toml ] || continue; \
		echo "== lint $$p"; (cd $$p && uv run ruff check .) || exit 1; \
	done

.PHONY: lint-tools
lint-tools: ## Lint the CLI tools outside both packages
	@echo "== lint client/ scripts/ benchmarks/"
	@uvx ruff@0.7.4 format --check client scripts benchmarks || exit 1
	@uvx ruff@0.7.4 check client scripts benchmarks || exit 1

.PHONY: types
types:
	@for p in $(PACKAGES); do \
		[ -f $$p/pyproject.toml ] || continue; \
		echo "== mypy $$p"; (cd $$p && uv run mypy src/) || exit 1; \
	done

.PHONY: imports
imports: ## Enforce the core/ layering contract
	@if [ -f gateway/pyproject.toml ]; then \
		echo "== import-linter gateway"; \
		(cd gateway && uv run lint-imports) || exit 1; \
	fi

.PHONY: test
test:
	@for p in $(PACKAGES); do \
		[ -f $$p/pyproject.toml ] || continue; \
		echo "== pytest $$p"; \
		(cd $$p && uv run pytest -q --cov --cov-fail-under=80) || exit 1; \
	done

.PHONY: doctest
doctest: ## Run the executable examples
	cd worker && uv run pytest --doctest-modules \
		src/worker/schemas.py src/worker/guardrails.py src/worker/errors.py -q
	cd gateway && uv run pytest --doctest-modules \
		src/gateway/core/models.py src/gateway/core/protocols.py \
		src/gateway/adapters/runpod_client.py src/gateway/adapters/guardrails.py -q

.PHONY: weights-check
weights-check: ## Verify the weight filter against the live manifest. No download.
	cd worker && uv run python scripts/fetch_weights.py --check

# --platform linux/amd64 is not optional. Built on an arm64 Mac without it,
# the image is one RunPod cannot run, and the failure presents as a worker that
# starts and immediately dies.

.PHONY: build-slim
build-slim: ## Build the deployed image (~2.9GB, no weights). Runs locally.
	docker buildx build --platform linux/amd64 \
		--build-arg BAKE_WEIGHTS=false \
		-f worker/Dockerfile -t $(IMAGE):$(TAG)-slim .

.PHONY: build-baked
build-baked: ## Build the weights-in-image variant (~45GB). Documented, not deployed.
	docker buildx build --platform linux/amd64 \
		--secret id=hf_token,env=HF_TOKEN \
		-f worker/Dockerfile -t $(IMAGE):$(TAG)-baked .
