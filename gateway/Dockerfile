FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY gateway/pyproject.toml gateway/uv.lock ./
RUN uv pip install --system --no-cache \
        "fastapi>=0.115" "uvicorn[standard]>=0.32" "httpx>=0.27" \
        "pydantic>=2.9" "pydantic-settings>=2.5" "structlog>=24.4"

COPY contracts/ /app/contracts/
COPY gateway/src/ /app/src/

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
