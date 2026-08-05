PACKAGES := worker gateway

.DEFAULT_GOAL := help

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

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
lint:
	@for p in $(PACKAGES); do \
		[ -f $$p/pyproject.toml ] || continue; \
		echo "== lint $$p"; (cd $$p && uv run ruff check .) || exit 1; \
	done

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

.PHONY: weights-check
weights-check: ## Verify the weight filter against the live manifest. No download.
	cd worker && uv run python scripts/fetch_weights.py --check

.PHONY: build-baked
build-baked: ## Build the baked-weights image. Run on a RunPod Pod.
	docker buildx build --secret id=hf_token,env=HF_TOKEN \
		--build-arg MODEL_REVISION=$$(cat contracts/model-revision.txt) \
		-f worker/Dockerfile -t $(IMAGE):$(TAG)-baked .

.PHONY: build-volume
build-volume: ## Build the network-volume image. Run on a RunPod Pod.
	docker buildx build --build-arg BAKE_WEIGHTS=false \
		--build-arg MODEL_REVISION=$$(cat contracts/model-revision.txt) \
		-f worker/Dockerfile -t $(IMAGE):$(TAG)-volume .
