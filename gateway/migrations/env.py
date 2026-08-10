"""Alembic environment: async engine over asyncpg, DSN injected at runtime.

`asyncio.run` means this must never be invoked from a thread already running
an event loop; `gateway.adapters.schema.upgrade_to_head` documents the
`asyncio.to_thread` contract.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine


def _database_url() -> str:
    """Return the DSN, rewritten onto the asyncpg driver.

    Returns:
        A `postgresql+asyncpg://` URL.

    Raises:
        RuntimeError: No DSN was configured or exported.
    """
    url = context.config.get_main_option("sqlalchemy.url") or os.environ.get(
        "DATABASE_URL", ""
    )
    if not url:
        msg = "Set DATABASE_URL (or sqlalchemy.url) to run migrations."
        raise RuntimeError(msg)
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


def run_migrations_offline() -> None:
    """Emit the migration SQL without a database connection."""
    context.configure(url=_database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    """Run the migrations on an established connection.

    Args:
        connection: A sync-facade connection provided by `run_sync`.
    """
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    """Connect with the async engine and apply the migrations."""
    engine = create_async_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_async())
