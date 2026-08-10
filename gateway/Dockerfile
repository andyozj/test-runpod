FROM python:3.11-slim

# Reported by /health. Baked at build time because the running container has no
# git and no package metadata to derive it from.
ARG VERSION=0.1.0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    VERSION=${VERSION}

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install from the exported lock, not hand-listed ranges: the image runs the
# same resolved set the tests ran against, or the build fails.
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

# Non-root: matches worker/Dockerfile's policy. The image dir must exist owned
# by the app user before a volume lands on it, or the mount arrives root-owned
# and the first save fails.
RUN useradd --uid 1000 --create-home gateway \
    && mkdir -p /data/images && chown gateway /data/images
USER gateway

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
