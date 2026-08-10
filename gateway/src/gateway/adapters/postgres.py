"""Postgres job repository: the memory adapter's invariants, enforced in SQL."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

from gateway.core.metrics import CompletedTiming
from gateway.core.models import (
    ErrorCode,
    GenerationParams,
    Job,
    JobResult,
    JobStatus,
    Progress,
    RequestContext,
)
from gateway.core.protocols import Clock, IdempotencyConflictError

TERMINAL = [status.value for status in JobStatus if status.terminal]

_INSERT = """
INSERT INTO jobs (
    id, status, api_key_id, correlation_id, idempotency_key, request_hash,
    params, runpod_job_id, result, progress, error_code, error_message,
    created_at, updated_at, completed_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
ON CONFLICT (api_key_id, idempotency_key) WHERE idempotency_key IS NOT NULL
DO NOTHING
RETURNING *
"""

_SELECT_BY_KEY = """
SELECT * FROM jobs WHERE api_key_id = $1 AND idempotency_key = $2
"""

# Preview frames are transient telemetry; terminal rows keep the step counters
# but never the frame. jsonb `- text` on NULL stays NULL, so this is safe on
# jobs that never reported progress.
_STRIP_PREVIEW = "progress = (progress - 'preview_b64'::text) - 'preview_format'::text"

_CLAIM = """
WITH picked AS (
    SELECT id FROM jobs
    WHERE NOT (status = ANY($1))
      AND (lease_expires_at IS NULL OR lease_expires_at <= $2)
      AND (runpod_job_id IS NOT NULL OR created_at <= $3)
    ORDER BY updated_at
    LIMIT $4
    FOR UPDATE SKIP LOCKED
)
UPDATE jobs SET lease_expires_at = $5
FROM picked WHERE jobs.id = picked.id
RETURNING jobs.*
"""

# Three numbers per completed job, never the multi-MB result row.
_RECENT_TIMINGS = """
SELECT created_at, completed_at,
       (result->>'inference_seconds')::float8 AS inference_seconds
FROM jobs
WHERE api_key_id = $1 AND status = $2
  AND completed_at IS NOT NULL AND result IS NOT NULL
ORDER BY completed_at DESC
LIMIT $3
"""

# Three-argument date_trunc (PostgreSQL 14+) truncates in UTC regardless of
# the session timezone, so bucket keys match the service's UTC hour floors.
_COMPLETED_BY_HOUR = """
SELECT date_trunc('hour', completed_at, 'UTC') AS hour, count(*) AS n
FROM jobs
WHERE api_key_id = $1 AND status = $2 AND completed_at >= $3
GROUP BY hour
"""


async def _init_connection(connection: asyncpg.Connection) -> None:
    """Register JSON codecs so jsonb columns round-trip as dicts.

    Args:
        connection: A freshly opened pool connection.
    """
    await connection.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


@dataclass
class PostgresJobRepository:
    """Job storage in Postgres via asyncpg.

    The memory adapter's invariants, moved into the database so they hold
    across processes:

    - The idempotent insert is one statement: a partial unique index on
      `(api_key_id, idempotency_key)` plus `ON CONFLICT ... DO NOTHING`, so
      two concurrent identical requests cannot both insert.
    - `claim_unresolved` is `FOR UPDATE SKIP LOCKED` plus a `lease_expires_at`
      column: concurrent ticks take disjoint rows, and a claimer that dies
      leaves rows that expire back into the pool.
    - `count_active` is race-safe by ordering, not locking: the service
      inserts the triggering job before counting, so a concurrent submitter
      can only inflate the count and shed early — never admit past the cap.

    Attributes:
        dsn: `postgresql://` connection string.
        clock: Injected wall time; every lease and grace comparison uses it,
            never the database's `now()`, so the adapter is assertable under
            a frozen clock.
    """

    dsn: str
    clock: Clock
    _pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Open the connection pool. Must run before any repository call."""
        self._pool = await asyncpg.create_pool(self.dsn, init=_init_connection)

    async def close(self) -> None:
        """Close the pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        """The open pool.

        Returns:
            The pool created by `connect`.

        Raises:
            RuntimeError: `connect` has not been called.
        """
        if self._pool is None:
            msg = "PostgresJobRepository.connect() has not been called"
            raise RuntimeError(msg)
        return self._pool

    async def create(self, job: Job) -> Job:
        """Insert a job, replaying on a repeated idempotency key.

        Args:
            job: The job to store.

        Returns:
            The stored job, or the existing one on a matching replay.

        Raises:
            IdempotencyConflictError: The key was reused with a different body.
        """
        row = await self.pool.fetchrow(_INSERT, *_insert_args(job))
        if row is not None:
            return _to_job(row)
        # The insert lost to an existing key: fetch the winner. A vanishing
        # winner (key released between the two statements) retries the insert.
        existing = await self.pool.fetchrow(
            _SELECT_BY_KEY, job.context.api_key_id, job.context.idempotency_key
        )
        if existing is None:
            return await self.create(job)
        if existing["request_hash"] != job.request_hash:
            raise IdempotencyConflictError
        return _to_job(existing)

    async def get(self, job_id: UUID) -> Job | None:
        """Fetch one job.

        Args:
            job_id: The id to look up.

        Returns:
            The job, or None if unknown.
        """
        row = await self.pool.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
        return _to_job(row) if row else None

    async def attach_runpod_id(self, job_id: UUID, runpod_job_id: str) -> Job:
        """Record the upstream job id, terminal status included.

        Args:
            job_id: The job to update.
            runpod_job_id: The upstream identifier.

        Returns:
            The updated job.

        Raises:
            KeyError: The job id is unknown.
        """
        row = await self.pool.fetchrow(
            "UPDATE jobs SET runpod_job_id = $2, updated_at = $3 "
            "WHERE id = $1 RETURNING *",
            job_id,
            runpod_job_id,
            self.clock.now(),
        )
        if row is None:
            raise KeyError(job_id)
        return _to_job(row)

    async def mark_in_progress(self, job_id: UUID, progress: Progress | None) -> Job:
        """Advance a job to running, storing progress when reported.

        Args:
            job_id: The job to update.
            progress: Latest progress, if reported.

        Returns:
            The updated job, or the unchanged job when already terminal.
        """
        return await self._transition(
            job_id,
            "status = $2, progress = $3, updated_at = $4",
            JobStatus.IN_PROGRESS.value,
            asdict(progress) if progress else None,
            self.clock.now(),
        )

    async def mark_completed(self, job_id: UUID, result: JobResult) -> Job:
        """Record a successful result.

        Args:
            job_id: The job to update.
            result: What the worker produced.

        Returns:
            The updated job, or the unchanged job when already terminal.
        """
        now = self.clock.now()
        return await self._transition(
            job_id,
            f"status = $2, result = $3, updated_at = $4, completed_at = $5, "
            f"{_STRIP_PREVIEW}",
            JobStatus.COMPLETED.value,
            asdict(result),
            now,
            now,
        )

    async def mark_failed(
        self, job_id: UUID, code: ErrorCode, message: str, status: JobStatus
    ) -> Job:
        """Record a terminal failure.

        Args:
            job_id: The job to update.
            code: The stable error code.
            message: Caller-safe description.
            status: The terminal status to write.

        Returns:
            The updated job, or the unchanged job when already terminal.
        """
        now = self.clock.now()
        return await self._transition(
            job_id,
            f"status = $2, error_code = $3, error_message = $4, "
            f"updated_at = $5, completed_at = $6, {_STRIP_PREVIEW}",
            status.value,
            code.value,
            message,
            now,
            now,
        )

    async def release_idempotency_key(self, job_id: UUID) -> None:
        """Drop this job's key binding, leaving the job row itself in place.

        Args:
            job_id: The job whose key binding is dropped.
        """
        await self.pool.execute(
            "UPDATE jobs SET idempotency_key = NULL WHERE id = $1", job_id
        )

    async def claim_unresolved(
        self, limit: int, lease_s: float, submit_grace_s: float
    ) -> list[Job]:
        """Lease non-terminal jobs, oldest first.

        One statement: `FOR UPDATE SKIP LOCKED` picks rows no concurrent
        claimer holds, and the update stamps the lease before the rows are
        returned, so two overlapping ticks cannot claim the same job.

        Args:
            limit: Maximum jobs to claim.
            lease_s: How long the claim excludes other callers.
            submit_grace_s: How long an id-less job is left to its submitter.

        Returns:
            The claimed jobs, oldest `updated_at` first.
        """
        now = self.clock.now()
        rows = await self.pool.fetch(
            _CLAIM,
            TERMINAL,
            now,
            now - timedelta(seconds=submit_grace_s),
            limit,
            now + timedelta(seconds=lease_s),
        )
        jobs = [_to_job(row) for row in rows]
        # UPDATE ... RETURNING gives no ordering guarantee; restore it here.
        jobs.sort(key=lambda job: (job.updated_at, job.created_at, str(job.id)))
        return jobs

    async def release_claim(self, job_id: UUID) -> None:
        """Drop a lease so the next tick can claim the job immediately.

        Args:
            job_id: The claimed job.
        """
        await self.pool.execute(
            "UPDATE jobs SET lease_expires_at = NULL WHERE id = $1", job_id
        )

    async def count_active(self, api_key_id: str) -> int:
        """Count a caller's non-terminal jobs, for the per-key active cap.

        Args:
            api_key_id: The caller to count.

        Returns:
            How many of that caller's jobs are not yet terminal.
        """
        count = await self.pool.fetchval(
            "SELECT count(*) FROM jobs "
            "WHERE api_key_id = $1 AND NOT (status = ANY($2))",
            api_key_id,
            TERMINAL,
        )
        return int(count)

    async def list_recent(self, api_key_id: str, limit: int) -> list[Job]:
        """List one caller's jobs, newest first.

        Args:
            api_key_id: The caller whose jobs are listed.
            limit: Maximum jobs to return.

        Returns:
            Up to `limit` jobs, newest `created_at` first.
        """
        rows = await self.pool.fetch(
            "SELECT * FROM jobs WHERE api_key_id = $1 "
            "ORDER BY created_at DESC, id LIMIT $2",
            api_key_id,
            max(limit, 0),
        )
        return [_to_job(row) for row in rows]

    async def count_by_status(self, api_key_id: str) -> dict[JobStatus, int]:
        """Count one caller's jobs per status via a single GROUP BY.

        Args:
            api_key_id: The caller whose jobs are counted.

        Returns:
            A count for every `JobStatus`, zero where the caller has none.
        """
        rows = await self.pool.fetch(
            "SELECT status, count(*) AS n FROM jobs "
            "WHERE api_key_id = $1 GROUP BY status",
            api_key_id,
        )
        counts = dict.fromkeys(JobStatus, 0)
        for row in rows:
            counts[JobStatus(row["status"])] = int(row["n"])
        return counts

    async def count_created_since(self, api_key_id: str, since: datetime) -> int:
        """Count one caller's jobs created at or after an instant.

        Args:
            api_key_id: The caller whose jobs are counted.
            since: Inclusive lower bound on `created_at`.

        Returns:
            How many of the caller's jobs were created in the interval.
        """
        count = await self.pool.fetchval(
            "SELECT count(*) FROM jobs WHERE api_key_id = $1 AND created_at >= $2",
            api_key_id,
            since,
        )
        return int(count)

    async def recent_completed_timings(
        self, api_key_id: str, limit: int
    ) -> list[CompletedTiming]:
        """Fetch timings of the caller's most recently completed jobs.

        The query projects three columns — `inference_seconds` is extracted
        from the result jsonb in SQL — so the window never loads result rows.

        Args:
            api_key_id: The caller whose completions are read.
            limit: Maximum timings to return.

        Returns:
            Up to `limit` timings, newest `completed_at` first.
        """
        rows = await self.pool.fetch(
            _RECENT_TIMINGS, api_key_id, JobStatus.COMPLETED.value, max(limit, 0)
        )
        return [
            CompletedTiming(
                created_at=row["created_at"],
                completed_at=row["completed_at"],
                inference_seconds=float(row["inference_seconds"]),
            )
            for row in rows
        ]

    async def count_completed_by_hour(
        self, api_key_id: str, since: datetime
    ) -> dict[datetime, int]:
        """Count the caller's completions per UTC hour via `date_trunc`.

        Args:
            api_key_id: The caller whose completions are counted.
            since: Inclusive lower bound on `completed_at`.

        Returns:
            Completions keyed by UTC hour start; hours with none are absent.
        """
        rows = await self.pool.fetch(
            _COMPLETED_BY_HOUR, api_key_id, JobStatus.COMPLETED.value, since
        )
        return {row["hour"]: int(row["n"]) for row in rows}

    async def _transition(self, job_id: UUID, assignments: str, *args: Any) -> Job:
        """Apply a guarded update; terminal rows are returned unchanged.

        Args:
            job_id: The job to update.
            assignments: SET clause with placeholders starting at `$2`.
            *args: Values for those placeholders. `Any`: forwarded verbatim to
                asyncpg, which owns the per-type encoding.

        Returns:
            The updated job, or the unchanged job when already terminal.

        Raises:
            KeyError: The job id is unknown.
        """
        terminal_position = len(args) + 2
        row = await self.pool.fetchrow(
            f"UPDATE jobs SET {assignments} WHERE id = $1 "  # noqa: S608 - assignments are module literals, never caller input
            f"AND NOT (status = ANY(${terminal_position})) RETURNING *",
            job_id,
            *args,
            TERMINAL,
        )
        if row is not None:
            return _to_job(row)
        current = await self.get(job_id)
        if current is None:
            raise KeyError(job_id)
        return current


def _insert_args(job: Job) -> list[Any]:
    """Render a job as the `_INSERT` parameter list.

    Args:
        job: The job to store.

    Returns:
        Values for `$1` through `$15`, in column order. `Any`: heterogeneous
        SQL parameters, encoded by asyncpg.
    """
    return [
        job.id,
        job.status.value,
        job.context.api_key_id,
        job.context.correlation_id,
        job.context.idempotency_key,
        job.request_hash,
        asdict(job.params),
        job.runpod_job_id,
        asdict(job.result) if job.result else None,
        asdict(job.progress) if job.progress else None,
        job.error_code.value if job.error_code else None,
        job.error_message,
        job.created_at,
        job.updated_at,
        job.completed_at,
    ]


def _to_job(row: asyncpg.Record) -> Job:
    """Map a row back to the domain type.

    Args:
        row: A full `jobs` row.

    Returns:
        The equivalent `Job`.
    """
    return Job(
        id=row["id"],
        status=JobStatus(row["status"]),
        params=GenerationParams(**row["params"]),
        context=RequestContext(
            api_key_id=row["api_key_id"],
            correlation_id=row["correlation_id"],
            idempotency_key=row["idempotency_key"],
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        runpod_job_id=row["runpod_job_id"],
        result=JobResult(**row["result"]) if row["result"] else None,
        progress=Progress(**row["progress"]) if row["progress"] else None,
        error_code=ErrorCode(row["error_code"]) if row["error_code"] else None,
        error_message=row["error_message"],
        completed_at=row["completed_at"],
        request_hash=row["request_hash"],
    )
