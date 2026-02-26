"""takeover session state table"""

from __future__ import annotations

from alembic import op

revision = "20260219_0004"
down_revision = "20260218_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS takeover_sessions (
          session_id TEXT NOT NULL,
          workspace_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          active BOOLEAN NOT NULL DEFAULT false,
          mode TEXT NOT NULL DEFAULT 'takeover',
          persona_mode TEXT NOT NULL DEFAULT 'normal',
          activation_keywords TEXT NOT NULL DEFAULT '',
          stop_keywords TEXT NOT NULL DEFAULT '',
          expires_at TIMESTAMPTZ NULL,
          activated_at TIMESTAMPTZ NULL,
          last_message_at TIMESTAMPTZ NULL,
          takeover_context JSONB NOT NULL DEFAULT '{}'::jsonb,
          last_classification TEXT NULL,
          last_safety_decision TEXT NOT NULL DEFAULT 'allow',
          updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (session_id, workspace_id, user_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_takeover_sessions_scope ON takeover_sessions (workspace_id, user_id, updated_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS takeover_sessions")

