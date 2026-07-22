"""add behavior evidence lifecycle and fidelity evaluation"""

from __future__ import annotations

from alembic import op

revision = "20260721_0028"
down_revision = "20260313_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS subject_user_id TEXT NOT NULL DEFAULT ''"
    )
    op.execute("ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS objective_text TEXT NOT NULL DEFAULT ''")
    op.execute(
        "ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS constraints_json JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS available_choices_json JSONB NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute("ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS selected_choice TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS action_taken TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS correction_text TEXT NOT NULL DEFAULT ''")
    op.execute(
        "ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS memory_class TEXT NOT NULL DEFAULT 'decision'"
    )
    op.execute(
        "ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS evidence_source TEXT NOT NULL DEFAULT 'inferred'"
    )
    op.execute(
        "ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS lifecycle_status TEXT NOT NULL DEFAULT 'active'"
    )
    op.execute(
        "ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW()"
    )
    op.execute("ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS valid_until TIMESTAMPTZ NULL")
    op.execute(
        "ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS contradicts_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb"
    )
    op.execute("ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ NULL")
    op.execute(
        "ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS behavior_schema_version TEXT NOT NULL DEFAULT 'v1'"
    )
    op.execute(
        "ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS redaction_applied BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS learning_eligible BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS storage_score REAL NOT NULL DEFAULT 0.0")
    op.execute(
        "ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS storage_decision TEXT NOT NULL DEFAULT 'audit_only'"
    )
    op.execute(
        """
        UPDATE decision_observations
        SET subject_user_id = CASE WHEN subject_user_id = '' THEN consumer_id ELSE subject_user_id END,
            objective_text = situation_summary,
            selected_choice = user_response,
            valid_from = COALESCE(valid_from, ts),
            lifecycle_status = CASE WHEN superseded_by IS NULL THEN 'active' ELSE 'superseded' END
        WHERE subject_user_id = '' OR objective_text = '' OR selected_choice = ''
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decision_observations_behavior_active
        ON decision_observations (workspace_id, subject_user_id, lifecycle_status, learning_eligible, ts DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decision_observations_validity
        ON decision_observations (workspace_id, valid_from, valid_until, ts DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS behavior_fidelity_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            consumer_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status TEXT NOT NULL,
            config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            gate_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            case_results_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_behavior_fidelity_runs_scope_created
        ON behavior_fidelity_runs (workspace_id, subject_user_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_behavior_fidelity_runs_scope_created")
    op.execute("DROP TABLE IF EXISTS behavior_fidelity_runs")
    op.execute("DROP INDEX IF EXISTS idx_decision_observations_validity")
    op.execute("DROP INDEX IF EXISTS idx_decision_observations_behavior_active")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS storage_decision")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS storage_score")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS learning_eligible")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS redaction_applied")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS behavior_schema_version")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS confirmed_at")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS contradicts_ids_json")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS valid_until")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS valid_from")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS lifecycle_status")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS evidence_source")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS memory_class")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS correction_text")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS action_taken")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS selected_choice")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS available_choices_json")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS constraints_json")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS objective_text")
    op.execute("ALTER TABLE decision_observations DROP COLUMN IF EXISTS subject_user_id")
