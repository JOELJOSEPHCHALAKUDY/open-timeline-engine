"""memory rules and tombstones"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260223_0019"
down_revision = "20260223_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_rules (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            scope JSONB NOT NULL DEFAULT '{}'::jsonb,
            rule_type TEXT NOT NULL,
            statement TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 2,
            active BOOLEAN NOT NULL DEFAULT true,
            source_episode_id UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_tombstones (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
            reason TEXT NOT NULL DEFAULT 'user_requested',
            requested_by TEXT NOT NULL,
            deleted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            meta JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_rules_scope_priority
        ON memory_rules (workspace_id, user_id, active, priority, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_tombstones_scope_deleted
        ON memory_tombstones (workspace_id, user_id, deleted_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_memory_tombstones_scope_deleted")
    op.execute("DROP INDEX IF EXISTS idx_memory_rules_scope_priority")
    op.execute("DROP TABLE IF EXISTS memory_tombstones")
    op.execute("DROP TABLE IF EXISTS memory_rules")

