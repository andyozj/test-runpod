# syntax=docker/dockerfile:1.7
#
# Hub build. The Hub builder reads a Dockerfile from .runpod/ or the repo root
# only — hub.json has no Dockerfile-path field (publishing guide, checked
# 2026-08-09) — so this is worker/Dockerfile with the weight-bake stage
# removed: Hub CI supplies no HF_TOKEN BuildKit secret, and the listing runs
# the slim variant with weights from RunPod's cached models. Assumes a
# repo-root build context (undocumented; the natural context when overriding a
# root Dockerfile). Keep in sync with worker/Dockerfile.

FROM ubuntu:22.04

# Not nvidia/cuda: the torch wheel bundles its own CUDA libraries and the
# driver arrives from the host via the container runtime.
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_DISABLE_TELEMETRY=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# No apt Python: Ubuntu 22.04's python3.11 is frozen at 3.11.0rc1, which
# crashes torch._dynamo on import. uv installs a current standalone CPython.
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN uv python install 3.11 && uv venv /opt/venv --python 3.11
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

COPY worker/pyproject.toml worker/uv.lock ./
RUN uv export --frozen --extra gpu --no-dev --no-emit-project \
        -o /tmp/requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache \
        -r /tmp/requirements.txt \
    && python -c "import torch, diffusers, transformers, runpod"

# No weights baked. WEIGHTS_PATH stays the code default; the directory does
# not exist in this image, so weights.resolve() falls through to the cache.
ENV WEIGHTS_PATH=/opt/weights
COPY contracts/ /app/contracts/
COPY worker/src/ /app/src/
COPY worker/test_input.json ./
ENV PYTHONPATH=/app/src

# Never reach for the network at load time: a misconfiguration fails loudly
# instead of quietly re-downloading and looking like a slow cold start.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

RUN useradd --uid 1000 --create-home worker
USER worker

CMD ["python", "-m", "worker.handler"]
