"""add deterministic event summary tiers"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260313_0027"
down_revision = "20260226_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS summary_l0 TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS summary_l1_json JSONB NOT NULL DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS summary_version TEXT NOT NULL DEFAULT 'v1'")
    op.execute(
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS summary_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS summary_updated_at")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS summary_version")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS summary_l1_json")
    op.execute("ALTER TABLE events DROP COLUMN IF EXISTS summary_l0")
