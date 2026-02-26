"""extend handoff_records for resume packet v1"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260226_0026"
down_revision = "20260226_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE handoff_records ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'native'")
    op.execute(
        "ALTER TABLE handoff_records ADD COLUMN IF NOT EXISTS change_summary_json JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute("ALTER TABLE handoff_records ADD COLUMN IF NOT EXISTS objective_text TEXT NOT NULL DEFAULT ''")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_handoff_records_workspace_owner_objective
        ON handoff_records (workspace_id, owner_id, objective_text)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_handoff_records_workspace_owner_objective")
    op.execute("ALTER TABLE handoff_records DROP COLUMN IF EXISTS objective_text")
    op.execute("ALTER TABLE handoff_records DROP COLUMN IF EXISTS change_summary_json")
    op.execute("ALTER TABLE handoff_records DROP COLUMN IF EXISTS source")
