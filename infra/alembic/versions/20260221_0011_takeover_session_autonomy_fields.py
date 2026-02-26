"""takeover sessions autonomy profile/goal tracking fields"""

from __future__ import annotations

from alembic import op

revision = "20260221_0011"
down_revision = "20260221_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE takeover_sessions "
        "ADD COLUMN IF NOT EXISTS autonomy_policy_profile TEXT NOT NULL DEFAULT 'human_consultative'"
    )
    op.execute("ALTER TABLE takeover_sessions ADD COLUMN IF NOT EXISTS active_goal_id UUID")
    op.execute("ALTER TABLE takeover_sessions ADD COLUMN IF NOT EXISTS goal_queue_size INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE takeover_sessions ADD COLUMN IF NOT EXISTS last_discovery_at TIMESTAMPTZ")
    op.execute("ALTER TABLE takeover_sessions ADD COLUMN IF NOT EXISTS continuity_violation_count INTEGER NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE takeover_sessions DROP COLUMN IF EXISTS continuity_violation_count")
    op.execute("ALTER TABLE takeover_sessions DROP COLUMN IF EXISTS last_discovery_at")
    op.execute("ALTER TABLE takeover_sessions DROP COLUMN IF EXISTS goal_queue_size")
    op.execute("ALTER TABLE takeover_sessions DROP COLUMN IF EXISTS active_goal_id")
    op.execute("ALTER TABLE takeover_sessions DROP COLUMN IF EXISTS autonomy_policy_profile")
