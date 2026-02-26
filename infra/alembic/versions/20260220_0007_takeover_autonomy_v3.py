"""takeover autonomy v3 state/session extensions"""

from __future__ import annotations

from alembic import op

revision = "20260220_0007"
down_revision = "20260219_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE takeover_sessions ADD COLUMN IF NOT EXISTS objective_hash TEXT")
    op.execute("ALTER TABLE takeover_sessions ADD COLUMN IF NOT EXISTS working_set_json JSONB NOT NULL DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE takeover_sessions ADD COLUMN IF NOT EXISTS last_deliberation_at TIMESTAMPTZ")
    op.execute("ALTER TABLE takeover_sessions ADD COLUMN IF NOT EXISTS recent_outcomes_json JSONB NOT NULL DEFAULT '[]'::jsonb")
    op.execute("ALTER TABLE takeover_sessions ADD COLUMN IF NOT EXISTS autonomy_score REAL NOT NULL DEFAULT 0.5")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS takeover_action_log (
          id UUID PRIMARY KEY,
          session_id TEXT NOT NULL,
          workspace_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          turn INTEGER NOT NULL DEFAULT 0,
          objective_hash TEXT NULL,
          action_kind TEXT NOT NULL,
          result TEXT NOT NULL,
          latency_ms INTEGER NOT NULL DEFAULT 0,
          meta JSONB NOT NULL DEFAULT '{}'::jsonb,
          ts TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_takeover_action_log_scope "
        "ON takeover_action_log (workspace_id, user_id, ts DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_takeover_action_log_session "
        "ON takeover_action_log (session_id, ts DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_takeover_action_log_session")
    op.execute("DROP INDEX IF EXISTS idx_takeover_action_log_scope")
    op.execute("DROP TABLE IF EXISTS takeover_action_log")
    op.execute("ALTER TABLE takeover_sessions DROP COLUMN IF EXISTS autonomy_score")
    op.execute("ALTER TABLE takeover_sessions DROP COLUMN IF EXISTS recent_outcomes_json")
    op.execute("ALTER TABLE takeover_sessions DROP COLUMN IF EXISTS last_deliberation_at")
    op.execute("ALTER TABLE takeover_sessions DROP COLUMN IF EXISTS working_set_json")
    op.execute("ALTER TABLE takeover_sessions DROP COLUMN IF EXISTS objective_hash")
