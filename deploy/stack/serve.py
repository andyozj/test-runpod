"""ASGI wrapper: frontend static files in front of the untouched gateway app.

The pod image's uvicorn target. API paths (and the lifespan protocol, which
runs migrations and the reconciler) delegate to `gateway.main:app`; everything
else is served from the frontend build with an index.html fallback at `/`.
Exists so the gateway package needs no static-file knowledge.
"""

from __future__ import annotations

import os
from typing import Any

from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.staticfiles import StaticFiles

from gateway.main import app as gateway

API_PREFIXES = ("/v1", "/health", "/docs", "/redoc", "/openapi.json")

static = StaticFiles(
    directory=os.environ.get("FRONTEND_DIST", "/app/frontend"), html=True
)


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Route API traffic to the gateway, everything else to the static build."""
    if scope["type"] != "http" or scope["path"].startswith(API_PREFIXES):
        await gateway(scope, receive, send)
        return
    try:
        await static(scope, receive, send)
    except HTTPException as exc:
        # Bare StaticFiles raises instead of responding; there is no outer
        # Starlette app here to catch it, so convert to a plain response.
        response = PlainTextResponse(exc.detail or "Not Found", exc.status_code)
        await response(scope, receive, send)
