"""takeover session enforcement counters and tick fields"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260222_0015"
down_revision = "20260222_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE takeover_sessions "
        "ADD COLUMN IF NOT EXISTS enforcement_mode TEXT NOT NULL DEFAULT 'strict_takeover'"
    )
    op.execute("ALTER TABLE takeover_sessions ADD COLUMN IF NOT EXISTS last_tick_at TIMESTAMPTZ")
    op.execute(
        "ALTER TABLE takeover_sessions "
        "ADD COLUMN IF NOT EXISTS pending_directive_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE takeover_sessions "
        "ADD COLUMN IF NOT EXISTS retry_backlog_count INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE takeover_sessions DROP COLUMN IF EXISTS retry_backlog_count")
    op.execute("ALTER TABLE takeover_sessions DROP COLUMN IF EXISTS pending_directive_count")
    op.execute("ALTER TABLE takeover_sessions DROP COLUMN IF EXISTS last_tick_at")
    op.execute("ALTER TABLE takeover_sessions DROP COLUMN IF EXISTS enforcement_mode")
