"""decision observations superseded linkage"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260224_0022"
down_revision = "20260224_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS superseded_by UUID")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decision_observations_superseded
        ON decision_observations (workspace_id, superseded_by, ts DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_decision_observations_superseded")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS superseded_by")
