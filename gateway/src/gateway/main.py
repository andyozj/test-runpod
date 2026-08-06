"""Composition root. The only place that decides which implementations are used."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI

from gateway.adapters.guardrails import BlocklistGuardrail
from gateway.adapters.memory import InMemoryJobRepository, SystemClock
from gateway.adapters.runpod_client import HttpRunPodClient
from gateway.api.app import Deps, create_app
from gateway.core.service import JobService
from gateway.settings import Settings, get_settings
from gateway.workers.reconciler import Reconciler

REQUEST_TIMEOUT_S = 30.0


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog for JSON output on stdout.

    Args:
        level: Root log level name.
    """
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )


def build(settings: Settings | None = None) -> FastAPI:
    """Assemble the application from concrete implementations.

    This is the only module that names both a protocol and an implementation.
    Swapping `InMemoryJobRepository` for the Postgres one is a change here and
    nowhere else.

    Args:
        settings: Override for configuration. Tests only.

    Returns:
        The configured application, with the reconciler bound to its lifespan.
    """
    settings = settings or get_settings()
    configure_logging()
    if settings.gateway_api_keys == Settings.model_fields["gateway_api_keys"].default:
        structlog.get_logger().warning(
            "default_api_keys_in_use",
            detail="GATEWAY_API_KEYS is unset; the documented dev key works",
        )

    clock = SystemClock()
    http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
    service = JobService(
        repository=InMemoryJobRepository(clock=clock),
        runpod=HttpRunPodClient(
            endpoint_id=settings.runpod_endpoint_id,
            api_key=settings.runpod_api_key,
            client=http,
        ),
        guardrail=BlocklistGuardrail.from_contract(),
        clock=clock,
        job_deadline_s=settings.job_deadline_s,
        max_queue_wait_s=settings.max_queue_wait_s,
        avg_job_s=settings.avg_job_s,
    )
    reconciler = Reconciler(
        service=service,
        interval_s=settings.reconcile_interval_s,
        idle_interval_s=settings.reconcile_idle_interval_s,
        batch=settings.reconcile_batch,
    )

    @asynccontextmanager
    async def lifespan() -> AsyncIterator[None]:
        async with reconciler.running():
            yield
        await http.aclose()

    return create_app(
        Deps(
            service=service,
            settings=settings,
            reconciler_age=lambda: reconciler.seconds_since_last_run,
        ),
        on_startup=lifespan,
    )


app = build()
