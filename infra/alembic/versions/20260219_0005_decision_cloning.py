"""decision cloning tables: decision_observations + behavioral_fingerprints"""

from __future__ import annotations

from alembic import op

revision = "20260219_0005"
down_revision = "20260219_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_observations (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          consumer_id TEXT NOT NULL,
          workspace_id TEXT NOT NULL DEFAULT 'default',
          ts TIMESTAMPTZ NOT NULL DEFAULT now(),
          situation_type TEXT NOT NULL,
          situation_summary TEXT NOT NULL,
          context_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
          user_response TEXT NOT NULL,
          response_reasoning TEXT NULL,
          outcome TEXT NULL,
          outcome_sentiment TEXT NULL,
          source_event_ids UUID[] NOT NULL DEFAULT '{}',
          confidence REAL NOT NULL DEFAULT 1.0
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_obs_consumer ON decision_observations (consumer_id, workspace_id, situation_type, ts DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS behavioral_fingerprints (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          consumer_id TEXT NOT NULL,
          workspace_id TEXT NOT NULL DEFAULT 'default',
          fingerprint JSONB NOT NULL DEFAULT '{}'::jsonb,
          observation_count INTEGER NOT NULL DEFAULT 0,
          last_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (consumer_id, workspace_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_fingerprint_consumer ON behavioral_fingerprints (consumer_id, workspace_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS clone_feedback (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          observation_id UUID REFERENCES decision_observations(id) ON DELETE CASCADE,
          session_id TEXT NOT NULL,
          feedback_type TEXT NOT NULL,
          correction_text TEXT NULL,
          ts TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS clone_feedback")
    op.execute("DROP TABLE IF EXISTS behavioral_fingerprints")
    op.execute("DROP TABLE IF EXISTS decision_observations")
