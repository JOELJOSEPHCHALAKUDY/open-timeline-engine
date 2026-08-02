"""ordered plan step index for autonomy goals"""

from __future__ import annotations

from alembic import op

revision = "20260802_0034"
down_revision = "20260801_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no default: metadata-only on PG16, so no table rewrite.
    # NULL means "not part of a plan", which is every pre-existing row.
    op.execute("ALTER TABLE autonomy_goals ADD COLUMN IF NOT EXISTS step_index INTEGER")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_autonomy_goals_plan_step "
        "ON autonomy_goals (session_id, workspace_id, user_id, parent_goal_id, step_index)"
    )


def downgrade() -> None:
    # Index first: CI runs a real downgrade/upgrade cycle, and dropping the column
    # out from under its own index fails there even when upgrade() is fine.
    op.execute("DROP INDEX IF EXISTS idx_autonomy_goals_plan_step")
    op.execute("ALTER TABLE autonomy_goals DROP COLUMN IF EXISTS step_index")
