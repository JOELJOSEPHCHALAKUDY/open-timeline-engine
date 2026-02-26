"""autonomy notices and directive execution lifecycle"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260222_0014"
down_revision = "20260221_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_notices (
            id UUID PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            goal_id UUID NULL,
            title TEXT NOT NULL,
            reason TEXT NOT NULL,
            priority REAL NOT NULL DEFAULT 0.0,
            expires_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL,
            acknowledged_at TIMESTAMPTZ NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS directive_executions (
            directive_id UUID PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            goal_id UUID NULL,
            objective_hash TEXT NULL,
            action_kind TEXT NOT NULL DEFAULT 'execute',
            attempt INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'pending',
            requires_permit BOOLEAN NOT NULL DEFAULT false,
            permit_id UUID NULL,
            claimed_by TEXT NULL,
            started_at TIMESTAMPTZ NULL,
            finished_at TIMESTAMPTZ NULL,
            expires_at TIMESTAMPTZ NULL,
            failure_class TEXT NULL,
            failure_reason TEXT NULL,
            retry_strategy TEXT NULL,
            meta JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_notices_session_priority_created "
        "ON autonomy_notices (session_id, acknowledged_at, priority DESC, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_directive_executions_session_state_started "
        "ON directive_executions (session_id, state, started_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_directive_executions_workspace_user_created "
        "ON directive_executions (workspace_id, user_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_directive_executions_workspace_user_created")
    op.execute("DROP INDEX IF EXISTS idx_directive_executions_session_state_started")
    op.execute("DROP INDEX IF EXISTS idx_autonomy_notices_session_priority_created")
    op.execute("DROP TABLE IF EXISTS directive_executions")
    op.execute("DROP TABLE IF EXISTS autonomy_notices")
