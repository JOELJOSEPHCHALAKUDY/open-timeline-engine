"""autonomy goals and execution permits"""

from __future__ import annotations

from alembic import op

revision = "20260221_0010"
down_revision = "20260221_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_goals (
          id UUID PRIMARY KEY,
          session_id TEXT NOT NULL,
          workspace_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          title TEXT NOT NULL,
          description TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT 'open_discovery',
          priority_score REAL NOT NULL DEFAULT 0.0,
          risk_tier TEXT NOT NULL DEFAULT 'medium',
          confidence REAL NOT NULL DEFAULT 0.0,
          reasoning TEXT NOT NULL DEFAULT '',
          evidence_event_ids UUID[] NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT 'candidate',
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_permits (
          id UUID PRIMARY KEY,
          session_id TEXT NOT NULL,
          workspace_id TEXT NOT NULL,
          action_kind TEXT NOT NULL,
          target_paths JSONB NOT NULL DEFAULT '[]'::jsonb,
          command_preview TEXT NULL,
          estimated_change_size INTEGER NOT NULL DEFAULT 0,
          decision TEXT NOT NULL DEFAULT 'allow',
          reason TEXT NOT NULL DEFAULT '',
          confirmed_by TEXT NULL,
          expires_at TIMESTAMPTZ NULL,
          created_at TIMESTAMPTZ NOT NULL,
          resolved_at TIMESTAMPTZ NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_goals_session_status_priority "
        "ON autonomy_goals (session_id, status, priority_score DESC, updated_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_permits_session_decision_expires "
        "ON execution_permits (session_id, decision, expires_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_execution_permits_session_decision_expires")
    op.execute("DROP INDEX IF EXISTS idx_autonomy_goals_session_status_priority")
    op.execute("DROP TABLE IF EXISTS execution_permits")
    op.execute("DROP TABLE IF EXISTS autonomy_goals")
