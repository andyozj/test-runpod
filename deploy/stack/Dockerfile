# The full-stack pod image: frontend build + gateway + postgres 16 under
# s6-overlay v3. RunPod CPU pods run one image and have no compose, so both
# processes ship in one container. Everything durable lives on the volume
# disk at /workspace (pgdata, images); the container disk is disposable.
#
# Build from the repo root (same context as gateway/Dockerfile):
#   make build-stack

# --- Stage 1: frontend build -------------------------------------------------
# Pinned to the build host's architecture: the output is platform-independent
# JS, and running tsc/vite under qemu emulation segfaults (exit 139).
FROM --platform=$BUILDPLATFORM node:22-slim AS frontend

WORKDIR /frontend
COPY frontend/ ./
RUN npm ci && npm run build

# --- Stage 2: runtime --------------------------------------------------------
FROM python:3.11-slim

# Reported by /health. Baked at build time because the running container has no
# git and no package metadata to derive it from.
ARG VERSION=0.1.0
ARG S6_OVERLAY_VERSION=3.2.0.2

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    VERSION=${VERSION} \
    # A service that fails to come up must kill the container: a half-up pod
    # behind the proxy is a 502 nobody can diagnose from outside.
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0

# postgres 16 comes from PGDG (Debian's own suite ships another major);
# xz-utils unpacks s6. The PGDG suite must match the base image's Debian
# release (python:3.11-slim moved bookworm -> trixie), so read it from
# os-release instead of pinning.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-16 \
    && rm -rf /var/lib/apt/lists/*

# s6-overlay v3: PID 1, supervision, dependency-ordered start and stop.
# Arch tarball follows the build platform (uname -m matches s6's release
# names: x86_64, aarch64), so native arm64 builds boot too.
RUN curl -fsSL -o /tmp/s6-noarch.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" \
    && curl -fsSL -o "/tmp/s6-$(uname -m).tar.xz" \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-$(uname -m).tar.xz" \
    && tar -C / -Jxpf /tmp/s6-noarch.tar.xz \
    && tar -C / -Jxpf "/tmp/s6-$(uname -m).tar.xz" \
    && rm /tmp/s6-noarch.tar.xz "/tmp/s6-$(uname -m).tar.xz"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install from the exported lock, not hand-listed ranges: the image runs the
# same resolved set the tests ran against, or the build fails. Mirrors
# gateway/Dockerfile exactly.
COPY gateway/pyproject.toml gateway/uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && python -c "import fastapi, uvicorn, httpx, pydantic, structlog, asyncpg, alembic, PIL"

COPY contracts/ /app/contracts/
COPY gateway/src/ /app/src/
# Migrations run at startup when DATABASE_URL is set; adapters/schema.py
# resolves alembic.ini and migrations/ relative to the package, i.e. /app.
COPY gateway/alembic.ini /app/alembic.ini
COPY gateway/migrations/ /app/migrations/
COPY --from=frontend /frontend/dist /app/frontend
COPY deploy/stack/serve.py /app/serve.py

# Same uid policy as gateway/Dockerfile; the postgres user comes with the
# package. s6 runs as root and drops per service (s6-setuidgid), so pg-init
# can chown /workspace after the volume mounts.
RUN useradd --uid 1000 --create-home gateway

COPY deploy/stack/rootfs/ /

# Pod-shape defaults; deploy/pods/stack.yaml sets the same values explicitly.
ENV DATABASE_URL=postgresql://gateway:gateway@localhost:5432/gateway \
    GATEWAY_IMAGE_DIR=/workspace/images \
    FRONTEND_DIST=/app/frontend

EXPOSE 8000
# start-period covers first-boot initdb + migrations.
HEALTHCHECK --interval=10s --timeout=3s --start-period=60s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

ENTRYPOINT ["/init"]
