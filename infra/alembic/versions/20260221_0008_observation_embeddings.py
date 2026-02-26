"""observation embeddings for semantic clone recall"""

from __future__ import annotations

from alembic import op

revision = "20260221_0008"
down_revision = "20260220_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS embedding vector(1024)")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_obs_embedding_ivfflat
        ON decision_observations USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10)
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_obs_workspace_ts ON decision_observations (workspace_id, ts DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_obs_workspace_ts")
    op.execute("DROP INDEX IF EXISTS idx_obs_embedding_ivfflat")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS embedding")
