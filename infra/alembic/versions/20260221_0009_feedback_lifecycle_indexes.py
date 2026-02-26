"""clone feedback indexes and lifecycle runtime defaults"""

from __future__ import annotations

from alembic import op

revision = "20260221_0009"
down_revision = "20260221_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS idx_clone_feedback_obs ON clone_feedback (observation_id, ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_clone_feedback_session ON clone_feedback (session_id, ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log (ts)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_agent_interactions_ts ON agent_interactions (ts DESC)")
    op.execute(
        """
        INSERT INTO runtime_settings (key, value, updated_at)
        VALUES (
            'event_retention',
            '{"retention_days": 180, "archive_enabled": true, "archive_path": "/data/archives"}'::jsonb,
            now()
        )
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_interactions_ts")
    op.execute("DROP INDEX IF EXISTS idx_audit_log_ts")
    op.execute("DROP INDEX IF EXISTS idx_clone_feedback_session")
    op.execute("DROP INDEX IF EXISTS idx_clone_feedback_obs")
