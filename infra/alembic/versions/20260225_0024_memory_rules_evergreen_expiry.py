"""memory rule evergreen and expiry controls"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260225_0024"
down_revision = "20260225_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE memory_rules ADD COLUMN IF NOT EXISTS evergreen BOOLEAN NOT NULL DEFAULT true")
    op.execute("ALTER TABLE memory_rules ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NULL")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_rules_expiry
        ON memory_rules (workspace_id, user_id, active, evergreen, expires_at, updated_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_memory_rules_expiry")
    op.execute("ALTER TABLE memory_rules DROP COLUMN IF EXISTS expires_at")
    op.execute("ALTER TABLE memory_rules DROP COLUMN IF EXISTS evergreen")
