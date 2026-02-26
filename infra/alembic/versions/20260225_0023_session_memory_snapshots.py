"""session memory snapshots for takeover rehydration"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260225_0023"
down_revision = "20260224_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS session_memory_snapshots (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            objective_hash TEXT NULL,
            snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_session_memory_snapshots_lookup
        ON session_memory_snapshots (workspace_id, user_id, session_id, objective_hash, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_session_memory_snapshots_created
        ON session_memory_snapshots (created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_session_memory_snapshots_created")
    op.execute("DROP INDEX IF EXISTS idx_session_memory_snapshots_lookup")
    op.execute("DROP TABLE IF EXISTS session_memory_snapshots")
