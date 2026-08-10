"""One `repository` fixture, parameterized over every JobRepository adapter.

The memory adapter always runs. The Postgres adapter runs when
`DATABASE_URL_TEST` points at a disposable database (CI service container, or
locally e.g. `docker run --rm -d -p 5433:5432 -e POSTGRES_PASSWORD=test
postgres:16` and `DATABASE_URL_TEST=postgresql://postgres:test@localhost:5433/postgres`)
and skips cleanly otherwise. `-m postgres` selects only the Postgres run.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest

from gateway.adapters.memory import InMemoryJobRepository
from gateway.adapters.postgres import PostgresJobRepository
from gateway.adapters.schema import upgrade_to_head
from gateway.core.protocols import JobRepository
from tests.conftest import FrozenClock

_MIGRATED: set[str] = set()


async def _postgres_repository(clock: FrozenClock) -> PostgresJobRepository:
    dsn = os.environ.get("DATABASE_URL_TEST", "")
    if not dsn:
        pytest.skip("DATABASE_URL_TEST is not set; Postgres contract run skipped")
    if dsn not in _MIGRATED:
        # env.py calls asyncio.run; a thread keeps it off this event loop.
        await asyncio.to_thread(upgrade_to_head, dsn)
        _MIGRATED.add(dsn)
    repository = PostgresJobRepository(dsn=dsn, clock=clock)
    await repository.connect()
    await repository.pool.execute("TRUNCATE jobs")
    return repository


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
async def repository(
    request: pytest.FixtureRequest, clock: FrozenClock
) -> AsyncIterator[JobRepository]:
    if request.param == "memory":
        yield InMemoryJobRepository(clock=clock)
        return
    postgres = await _postgres_repository(clock)
    yield postgres
    await postgres.close()
