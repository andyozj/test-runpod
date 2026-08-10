"""Initial jobs table.

Revision ID: 0001
Revises: None
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# The terminal set mirrors gateway.core.models.JobStatus.terminal. Inlined:
# a migration must stay frozen even if the enum later changes.
_TERMINAL = "('COMPLETED', 'FAILED', 'TIMED_OUT', 'CANCELLED', 'BLOCKED')"


def upgrade() -> None:
    """Create the jobs table and its three working indexes."""
    op.execute(
        """
        CREATE TABLE jobs (
            id UUID PRIMARY KEY,
            status TEXT NOT NULL,
            api_key_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            idempotency_key TEXT,
            request_hash TEXT NOT NULL,
            params JSONB NOT NULL,
            runpod_job_id TEXT,
            result JSONB,
            progress JSONB,
            error_code TEXT,
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ,
            lease_expires_at TIMESTAMPTZ
        )
        """
    )
    # The atomic idempotent insert: partial and unique, so keyless jobs are
    # unconstrained and a replayed key conflicts in one statement.
    op.execute(
        "CREATE UNIQUE INDEX jobs_idempotency ON jobs (api_key_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    # Serves claim_unresolved's oldest-first scan over the non-terminal rows.
    op.execute(
        "CREATE INDEX jobs_unresolved ON jobs (updated_at) "
        f"WHERE status NOT IN {_TERMINAL}"
    )
    # Serves list_recent and count_active.
    op.execute("CREATE INDEX jobs_by_caller ON jobs (api_key_id, created_at DESC)")


def downgrade() -> None:
    """Drop the jobs table."""
    op.execute("DROP TABLE jobs")
