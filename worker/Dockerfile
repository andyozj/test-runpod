# syntax=docker/dockerfile:1.7
#
# Layers are ordered slowest-changing first. Weights sit BELOW application code
# so a code change never invalidates the ~33GB layer — reversing those two costs
# an hour per build.
#
# Build (baked, the default and what the brief asks for):
#   docker buildx build --secret id=hf_token,env=HF_TOKEN \
#     -t ghcr.io/OWNER/flux-worker:0.1.0-SHA-baked .
#
# Build (network volume variant, ~10GB):
#   docker buildx build --build-arg BAKE_WEIGHTS=false \
#     -t ghcr.io/OWNER/flux-worker:0.1.0-SHA-volume .

FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

# runtime, not devel: the devel image adds several GB of compiler toolchain
# that nothing at inference time uses.

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_DISABLE_TELEMETRY=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# No apt Python at all. Ubuntu 22.04's python3.11 package is frozen at
# 3.11.0rc1, which predates sys.get_int_max_str_digits and crashes
# torch._dynamo on import. uv installs a current standalone CPython, and the
# venv on PATH is the only interpreter in the image — nothing to mis-target.
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN uv python install 3.11 && uv venv /opt/venv --python 3.11
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# Python dependencies before weights and before code, so neither invalidates
# this layer. Build context is the repo root (`make build-*` passes
# `-f worker/Dockerfile .`), so paths are repo-relative.
# The lock is exported rather than hand-listing ranges: the image runs what
# `uv.lock` pins, or the build fails. The trailing import is the proof — a
# broken interpreter/deps combination fails the build here, not the endpoint.
COPY worker/pyproject.toml worker/uv.lock ./
RUN uv export --frozen --extra gpu --no-dev --no-emit-project \
        -o /tmp/requirements.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache \
        -r /tmp/requirements.txt \
    && python -c "import torch, diffusers, transformers, runpod"

# Weights. HF_TOKEN arrives as a BuildKit secret and is never an ARG or ENV —
# an ARG-passed token is recoverable from image history by anyone who pulls.
ARG BAKE_WEIGHTS=true
ENV WEIGHTS_PATH=/opt/weights
COPY contracts/ /app/contracts/
COPY worker/scripts/ /app/scripts/
RUN --mount=type=secret,id=hf_token \
    if [ "$BAKE_WEIGHTS" = "true" ]; then \
        HF_TOKEN="$(cat /run/secrets/hf_token)" \
        HUGGING_FACE_HUB_TOKEN="$(cat /run/secrets/hf_token)" \
        python /app/scripts/fetch_weights.py --dest "$WEIGHTS_PATH"; \
    else \
        echo "skipping weights; expecting a network volume at $WEIGHTS_PATH"; \
    fi

# Application code last: this is the only layer that changes per commit.
# test_input.json lands in the workdir so `python -m worker.handler` on a Pod
# runs one real job through the SDK's local mode — the pre-endpoint smoke test.
COPY worker/src/ /app/src/
COPY worker/test_input.json ./
ENV PYTHONPATH=/app/src

ARG MODEL_REVISION=main
ENV MODEL_REVISION=${MODEL_REVISION}

CMD ["python", "-m", "worker.handler"]
