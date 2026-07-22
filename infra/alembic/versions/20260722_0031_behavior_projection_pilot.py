"""add prospective behavior projection pilot persistence"""

from __future__ import annotations

from alembic import op

revision = "20260722_0031"
down_revision = "20260721_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS behavior_projection_pilot_assignments (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            trial_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            variant TEXT NOT NULL,
            situation_type TEXT NOT NULL,
            situation_summary TEXT NOT NULL,
            objective_text TEXT NOT NULL,
            request_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            context_sha256 TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            citations_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            injected_tokens INTEGER NOT NULL DEFAULT 0,
            retrieval_latency_ms INTEGER NOT NULL DEFAULT 0,
            redaction_applied BOOLEAN NOT NULL DEFAULT false,
            assigned_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1',
            UNIQUE(workspace_id, subject_user_id, trial_key)
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_projection_pilot_scope_assigned
            ON behavior_projection_pilot_assignments (
                workspace_id, subject_user_id, assigned_at DESC
            );
        CREATE INDEX IF NOT EXISTS idx_behavior_projection_pilot_variant_assigned
            ON behavior_projection_pilot_assignments (
                workspace_id, subject_user_id, variant, assigned_at DESC
            );

        CREATE TABLE IF NOT EXISTS behavior_projection_pilot_outcomes (
            id UUID PRIMARY KEY,
            assignment_id UUID NOT NULL UNIQUE REFERENCES behavior_projection_pilot_assignments(id)
                ON DELETE CASCADE,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            reporter_id TEXT NOT NULL,
            outcome_digest TEXT NOT NULL,
            agent_choice TEXT NULL,
            top3_choices_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            actual_choice TEXT NOT NULL,
            agent_confidence REAL NOT NULL DEFAULT 0.0,
            abstained BOOLEAN NOT NULL DEFAULT false,
            action_similarity REAL NOT NULL DEFAULT 0.0,
            workflow_similarity REAL NOT NULL DEFAULT 0.0,
            correction_required BOOLEAN NOT NULL DEFAULT false,
            outcome_regret BOOLEAN NOT NULL DEFAULT false,
            irrelevant_personalization BOOLEAN NOT NULL DEFAULT false,
            malicious_memory_activated BOOLEAN NOT NULL DEFAULT false,
            stale_evidence_used BOOLEAN NOT NULL DEFAULT false,
            used_evidence_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            notes TEXT NOT NULL DEFAULT '',
            redaction_applied BOOLEAN NOT NULL DEFAULT false,
            reported_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_projection_pilot_outcome_scope_reported
            ON behavior_projection_pilot_outcomes (
                workspace_id, subject_user_id, reported_at DESC
            );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS behavior_projection_pilot_outcomes")
    op.execute("DROP TABLE IF EXISTS behavior_projection_pilot_assignments")
