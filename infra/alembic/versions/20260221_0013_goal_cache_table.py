"""autonomy goal cache table"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260221_0013"
down_revision = "20260221_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_goal_cache (
            cache_key TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            cache_version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            last_accessed_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_goal_cache_scope_expires "
        "ON autonomy_goal_cache (session_id, workspace_id, user_id, expires_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_autonomy_goal_cache_scope_expires")
    op.execute("DROP TABLE IF EXISTS autonomy_goal_cache")
