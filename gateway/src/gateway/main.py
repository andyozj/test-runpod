"""Composition root. The only place that decides which implementations are used."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import structlog
from fastapi import FastAPI

from gateway.adapters.guardrails import BlocklistGuardrail
from gateway.adapters.images import FilesystemImageStore
from gateway.adapters.memory import InMemoryJobRepository, SystemClock
from gateway.adapters.postgres import PostgresJobRepository
from gateway.adapters.ratelimit import TokenBucketRateLimiter
from gateway.adapters.runpod_client import HttpRunPodClient, submit_envelope_s
from gateway.adapters.schema import upgrade_to_head
from gateway.api.app import Deps, create_app
from gateway.core.service import JobService
from gateway.settings import Settings, get_settings
from gateway.workers.reconciler import Reconciler

REQUEST_TIMEOUT_S = 30.0
SUBMIT_MAX_ATTEMPTS = 3


def submit_grace_s(settings: Settings) -> float:
    """Return how long an id-less job is left to its submitter.

    Derived from the client's own retry envelope rather than configured
    independently. The reconciler adopting a job whose submit is still
    retrying is exactly the double-submit the grace period exists to prevent,
    and a configured value shorter than the envelope reopens it silently. The
    setting is a floor, not the answer.

    Args:
        settings: Runtime configuration.

    Returns:
        Seconds, never below the worst case a submit can occupy.
    """
    return max(
        settings.submit_grace_s,
        submit_envelope_s(SUBMIT_MAX_ATTEMPTS, REQUEST_TIMEOUT_S),
    )


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
    `DATABASE_URL` picks the persistence pair: set, jobs live in Postgres and
    completed images on disk; unset, both stay in memory and results keep
    their inline base64. Migrations run and the pool opens inside the
    lifespan, so building the app touches no database.

    Args:
        settings: Override for configuration. Tests only.

    Returns:
        The configured application, with the reconciler bound to its lifespan.
    """
    settings = settings or get_settings()
    configure_logging()

    clock = SystemClock()
    http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)

    postgres: PostgresJobRepository | None = None
    image_store: FilesystemImageStore | None = None
    if settings.database_url:
        postgres = PostgresJobRepository(dsn=settings.database_url, clock=clock)
        image_store = FilesystemImageStore(
            root=Path(settings.gateway_image_dir),
            max_bytes=settings.gateway_image_store_max_bytes,
        )

    service = JobService(
        repository=postgres or InMemoryJobRepository(clock=clock),
        runpod=HttpRunPodClient(
            endpoint_id=settings.runpod_endpoint_id,
            api_key=settings.runpod_api_key,
            client=http,
            max_attempts=SUBMIT_MAX_ATTEMPTS,
        ),
        guardrail=BlocklistGuardrail.from_contract(),
        clock=clock,
        image_store=image_store,
        rate_limiter=TokenBucketRateLimiter(
            clock=clock,
            rpm=settings.gateway_rate_limit_rpm,
            burst=settings.gateway_rate_limit_burst,
        ),
        job_deadline_s=settings.job_deadline_s,
        max_queue_wait_s=settings.max_queue_wait_s,
        avg_job_s=settings.avg_job_s,
        submit_grace_s=submit_grace_s(settings),
        health_max_age_s=settings.health_max_age_s,
        max_active_jobs_per_key=settings.max_active_jobs_per_key,
        metrics_window=settings.gateway_metrics_window,
        gpu_rate_usd_hr=settings.gateway_gpu_rate_usd_hr,
    )
    reconciler = Reconciler(
        service=service,
        interval_s=settings.reconcile_interval_s,
        idle_interval_s=settings.reconcile_idle_interval_s,
        batch=settings.reconcile_batch,
    )

    @asynccontextmanager
    async def lifespan() -> AsyncIterator[None]:
        if postgres is not None:
            # `upgrade_to_head` calls `asyncio.run` internally; a thread keeps
            # it off this event loop.
            await asyncio.to_thread(upgrade_to_head, postgres.dsn)
            await postgres.connect()
        async with reconciler.running():
            yield
        await http.aclose()
        if postgres is not None:
            await postgres.close()

    return create_app(
        Deps(
            service=service,
            settings=settings,
            image_store=image_store,
            reconciler_age=lambda: reconciler.seconds_since_last_run,
        ),
        on_startup=lifespan,
    )


app = build()
