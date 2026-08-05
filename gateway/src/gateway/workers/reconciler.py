"""Background reconciliation loop.

Nothing tells us when a job finishes — webhooks are documented, not built — so
the only way to learn an outcome is to ask. This asks.

Distinct from the async facade: the client polls *us*, this polls *RunPod*.
Neither is aware of the other.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import structlog

from gateway.core.service import JobService

logger = structlog.get_logger()

JITTER = 0.2


@dataclass
class Reconciler:
    """Polls RunPod on an interval until cancelled.

    Attributes:
        service: The domain service.
        interval_s: Tick interval while work is outstanding.
        idle_interval_s: Tick interval when nothing is unresolved.
        batch: Jobs claimed per tick.
    """

    service: JobService
    interval_s: float = 2.0
    idle_interval_s: float = 10.0
    batch: int = 50
    _task: asyncio.Task[None] | None = field(default=None, init=False)
    _last_run_monotonic: float | None = field(default=None, init=False)

    async def run_forever(self) -> None:
        """Tick until cancelled.

        Jitter matters with more than one replica: without it, two instances
        started together tick in lockstep forever.
        """
        while True:
            advanced = 0
            try:
                advanced = await self.service.reconcile(self.batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive a tick
                logger.warning("reconcile_tick_failed", error=str(exc))
            self._last_run_monotonic = asyncio.get_running_loop().time()

            base = self.interval_s if advanced else self.idle_interval_s
            await asyncio.sleep(base * random.uniform(1 - JITTER, 1 + JITTER))  # noqa: S311

    @property
    def seconds_since_last_run(self) -> float | None:
        """How long since the last completed tick.

        Catches the failure that is otherwise invisible: a background task that
        has silently died while the process stays perfectly alive.

        Returns:
            Seconds, or None if it has not run yet.
        """
        if self._last_run_monotonic is None:
            return None
        return asyncio.get_running_loop().time() - self._last_run_monotonic

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Run the loop for the lifetime of the context.

        On exit the task is cancelled and awaited, so an in-flight tick
        finishes rather than dying mid-write.

        Yields:
            None, while the loop runs.
        """
        self._task = asyncio.create_task(self.run_forever())
        try:
            yield
        finally:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
