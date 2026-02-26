"""dashboard human-level score snapshot storage"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260222_0016"
down_revision = "20260222_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_human_score_snapshots (
            id UUID PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            score INTEGER NOT NULL,
            band TEXT NOT NULL,
            subscores_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            inputs_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dashboard_human_score_scope_created
        ON dashboard_human_score_snapshots (workspace_id, user_id, session_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_dashboard_human_score_scope_created")
    op.execute("DROP TABLE IF EXISTS dashboard_human_score_snapshots")
