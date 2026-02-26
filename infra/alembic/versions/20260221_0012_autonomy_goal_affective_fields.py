"""autonomy goal affective fields and embedding/indexes"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260221_0012"
down_revision = "20260221_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE autonomy_goals ADD COLUMN IF NOT EXISTS goal_kind TEXT NOT NULL DEFAULT 'normal'")
    op.execute("ALTER TABLE autonomy_goals ADD COLUMN IF NOT EXISTS affective_scores JSONB NOT NULL DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE autonomy_goals ADD COLUMN IF NOT EXISTS selection_score REAL NOT NULL DEFAULT 0.0")
    op.execute("ALTER TABLE autonomy_goals ADD COLUMN IF NOT EXISTS goal_embedding vector(1024)")
    op.execute("ALTER TABLE autonomy_goals ADD COLUMN IF NOT EXISTS goal_signature TEXT")
    op.execute("ALTER TABLE autonomy_goals ADD COLUMN IF NOT EXISTS parent_goal_id UUID")
    op.execute("ALTER TABLE autonomy_goals ADD COLUMN IF NOT EXISTS cache_hit BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE autonomy_goals ADD COLUMN IF NOT EXISTS cache_source TEXT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_goals_scope_selection "
        "ON autonomy_goals (session_id, workspace_id, user_id, selection_score DESC, updated_at DESC)"
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_autonomy_goals_goal_embedding_ivfflat
        ON autonomy_goals USING ivfflat (goal_embedding vector_cosine_ops)
        WITH (lists = 10)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_autonomy_goals_goal_embedding_ivfflat")
    op.execute("DROP INDEX IF EXISTS idx_autonomy_goals_scope_selection")
    op.execute("ALTER TABLE autonomy_goals DROP COLUMN IF EXISTS cache_source")
    op.execute("ALTER TABLE autonomy_goals DROP COLUMN IF EXISTS cache_hit")
    op.execute("ALTER TABLE autonomy_goals DROP COLUMN IF EXISTS parent_goal_id")
    op.execute("ALTER TABLE autonomy_goals DROP COLUMN IF EXISTS goal_signature")
    op.execute("ALTER TABLE autonomy_goals DROP COLUMN IF EXISTS goal_embedding")
    op.execute("ALTER TABLE autonomy_goals DROP COLUMN IF EXISTS selection_score")
    op.execute("ALTER TABLE autonomy_goals DROP COLUMN IF EXISTS affective_scores")
    op.execute("ALTER TABLE autonomy_goals DROP COLUMN IF EXISTS goal_kind")
