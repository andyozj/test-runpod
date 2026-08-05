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
    UV_SYSTEM_PYTHON=1 \
    HF_HUB_DISABLE_TELEMETRY=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Python dependencies before weights and before code, so neither invalidates
# this layer.
COPY pyproject.toml uv.lock ./
RUN uv pip install --system --no-cache \
        "torch>=2.4" "diffusers>=0.31" "transformers>=4.44" "accelerate>=0.34" \
        "sentencepiece>=0.2" "protobuf>=5.28" \
        "pydantic>=2.9" "pydantic-settings>=2.5" "structlog>=24.4" \
        "pillow>=10.4" "runpod>=1.7" "huggingface-hub>=0.25"

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
COPY worker/src/ /app/src/
ENV PYTHONPATH=/app/src

ARG MODEL_REVISION=main
ENV MODEL_REVISION=${MODEL_REVISION}

CMD ["python", "-m", "worker.handler"]
