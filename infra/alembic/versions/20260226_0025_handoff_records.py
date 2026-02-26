"""add handoff_records canonical table"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260226_0025"
down_revision = "20260225_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS handoff_records (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            directive_id UUID NULL,
            ts TIMESTAMPTZ NOT NULL,
            title TEXT NOT NULL,
            decision TEXT NOT NULL,
            next_step TEXT NOT NULL,
            status TEXT NOT NULL,
            files_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            anchors_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            git_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            event_id UUID NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1',
            redaction_applied BOOLEAN NOT NULL DEFAULT FALSE,
            expires_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_handoff_records_workspace_owner_ts
        ON handoff_records (workspace_id, owner_id, ts DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_handoff_records_workspace_ts
        ON handoff_records (workspace_id, ts DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_handoff_records_workspace_session_ts
        ON handoff_records (workspace_id, session_id, ts DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_handoff_records_expires
        ON handoff_records (expires_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_handoff_records_expires")
    op.execute("DROP INDEX IF EXISTS idx_handoff_records_workspace_session_ts")
    op.execute("DROP INDEX IF EXISTS idx_handoff_records_workspace_ts")
    op.execute("DROP INDEX IF EXISTS idx_handoff_records_workspace_owner_ts")
    op.execute("DROP TABLE IF EXISTS handoff_records")
