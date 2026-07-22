"""add behavioral control-plane records"""

from __future__ import annotations

from alembic import op

revision = "20260721_0029"
down_revision = "20260721_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS capability_grants (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            directive_id UUID NULL,
            permit_id UUID NULL,
            capability TEXT NOT NULL,
            action TEXT NOT NULL,
            resource TEXT NOT NULL,
            action_digest TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            risk_tier TEXT NOT NULL,
            mutating BOOLEAN NOT NULL DEFAULT false,
            redaction_applied BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ NULL
        );
        CREATE INDEX IF NOT EXISTS idx_capability_grants_scope_status
            ON capability_grants (workspace_id, owner_id, status, expires_at DESC);

        CREATE TABLE IF NOT EXISTS behavior_process_models (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            process_signature TEXT NOT NULL,
            name TEXT NOT NULL,
            steps_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            transitions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            support INTEGER NOT NULL DEFAULT 0,
            success_rate REAL NOT NULL DEFAULT 0.0,
            reliability REAL NOT NULL DEFAULT 0.0,
            source_sessions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            evidence_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            status TEXT NOT NULL DEFAULT 'candidate',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            schema_version TEXT NOT NULL DEFAULT 'v1',
            UNIQUE(workspace_id, subject_user_id, process_signature)
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_process_models_scope_status
            ON behavior_process_models (workspace_id, subject_user_id, status, reliability DESC);

        CREATE TABLE IF NOT EXISTS behavior_shadow_predictions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            observation_id UUID NULL,
            predicted_choice TEXT NULL,
            actual_choice TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.0,
            abstained BOOLEAN NOT NULL DEFAULT true,
            correct BOOLEAN NULL,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            query_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            citations_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            schema_version TEXT NOT NULL DEFAULT 'v1'
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_shadow_scope_created
            ON behavior_shadow_predictions (workspace_id, subject_user_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS behavior_memory_reviews (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id UUID NOT NULL,
            title TEXT NOT NULL,
            rationale TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            proposed_action TEXT NOT NULL DEFAULT 'promote',
            source TEXT NOT NULL,
            score REAL NOT NULL DEFAULT 0.0,
            reviewer_id TEXT NULL,
            review_note TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at TIMESTAMPTZ NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1',
            UNIQUE(workspace_id, subject_user_id, target_type, target_id)
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_memory_reviews_scope_status
            ON behavior_memory_reviews (workspace_id, subject_user_id, status, created_at DESC);

        CREATE TABLE IF NOT EXISTS behavior_counterfactuals (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            observation_id UUID NULL,
            session_id TEXT NOT NULL,
            directive_id UUID NULL,
            decision TEXT NOT NULL,
            alternative TEXT NOT NULL,
            expected_outcome TEXT NOT NULL,
            assumptions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            confidence REAL NOT NULL DEFAULT 0.5,
            status TEXT NOT NULL DEFAULT 'open',
            assessment TEXT NULL,
            observed_outcome TEXT NOT NULL DEFAULT '',
            lesson TEXT NOT NULL DEFAULT '',
            regret_score REAL NULL,
            redaction_applied BOOLEAN NOT NULL DEFAULT false,
            review_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at TIMESTAMPTZ NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_counterfactuals_scope_status
            ON behavior_counterfactuals (workspace_id, subject_user_id, status, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS behavior_counterfactuals")
    op.execute("DROP TABLE IF EXISTS behavior_memory_reviews")
    op.execute("DROP TABLE IF EXISTS behavior_shadow_predictions")
    op.execute("DROP TABLE IF EXISTS behavior_process_models")
    op.execute("DROP TABLE IF EXISTS capability_grants")
