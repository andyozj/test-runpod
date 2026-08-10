"""Programmatic Alembic upgrade, run at startup before the pool opens."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

# src/gateway/adapters/schema.py -> the directory holding alembic.ini and
# migrations/ (gateway/ in the repo, /app in the image).
_PACKAGE_ROOT = Path(__file__).resolve().parents[3]


def upgrade_to_head(dsn: str) -> None:
    """Apply every pending migration.

    Synchronous, and `migrations/env.py` calls `asyncio.run` internally — so
    from async code this must be dispatched with `asyncio.to_thread`, never
    called directly on the event loop.

    Args:
        dsn: `postgresql://` connection string.
    """
    config = Config(str(_PACKAGE_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PACKAGE_ROOT / "migrations"))
    # set_main_option interpolates `%`; a URL-encoded password must survive it.
    config.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
    command.upgrade(config, "head")
