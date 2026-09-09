"""trusted capture: input receipts, decision opportunities, human resolutions, prospective shadow predictions"""

from __future__ import annotations

from alembic import op

revision = "20260909_0036"
down_revision = "20260909_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS trusted_input_receipts (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            host_session_id TEXT NOT NULL,
            sequence BIGINT,
            prompt_id TEXT,
            delivery_key TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            origin_kind TEXT NOT NULL,
            capture_principal TEXT NOT NULL,
            host_client TEXT NOT NULL DEFAULT 'claude',
            event_id UUID,
            project_id TEXT,
            observed_at TIMESTAMPTZ NOT NULL,
            ingested_at TIMESTAMPTZ NOT NULL,
            original_char_count INTEGER NOT NULL DEFAULT 0,
            content_truncated BOOLEAN NOT NULL DEFAULT false,
            redaction_applied_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            spool_depth INTEGER NOT NULL DEFAULT 0,
            spool_failures INTEGER NOT NULL DEFAULT 0,
            gap_since TIMESTAMPTZ,
            queue_state TEXT NOT NULL DEFAULT 'inline',
            extraction_state TEXT NOT NULL DEFAULT 'pending',
            extraction_lease_until TIMESTAMPTZ,
            extraction_attempts INTEGER NOT NULL DEFAULT 0,
            extraction_last_error TEXT,
            extraction_version_done TEXT,
            next_extraction_at TIMESTAMPTZ,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_trusted_input_receipts_delivery ON trusted_input_receipts (workspace_id, owner_id, delivery_key)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_trusted_input_receipts_subject_ingested ON trusted_input_receipts (workspace_id, subject_user_id, ingested_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_trusted_input_receipts_extraction ON trusted_input_receipts (extraction_state, next_extraction_at)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_opportunities (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            owner_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL,
            turn INTEGER,
            objective_hash TEXT,
            task_id TEXT,
            project_id TEXT,
            decision_family TEXT NOT NULL,
            situation_type TEXT NOT NULL DEFAULT 'choice_required',
            question_text TEXT NOT NULL DEFAULT '',
            alternatives_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            pre_answer_snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            evidence_cutoff_at TIMESTAMPTZ,
            evidence_revision TEXT,
            advice_exposure_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            shadow_prediction_id UUID,
            source_event_id UUID,
            status TEXT NOT NULL DEFAULT 'open',
            relayed_answer TEXT,
            relayed_at TIMESTAMPTZ,
            resolved_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            frozen_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_decision_opportunities_open ON decision_opportunities (workspace_id, subject_user_id, status, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_decision_opportunities_session ON decision_opportunities (session_id, status)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_candidates (
            id UUID PRIMARY KEY,
            receipt_id UUID NOT NULL,
            source_event_id UUID,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            opportunity_id UUID,
            candidate_kind TEXT NOT NULL,
            supporting_span TEXT NOT NULL,
            span_sha256 TEXT NOT NULL,
            observed_alternatives_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            selected_option TEXT,
            stated_rationale TEXT,
            is_negated BOOLEAN NOT NULL DEFAULT false,
            is_correction BOOLEAN NOT NULL DEFAULT false,
            project_id TEXT,
            task_id TEXT,
            origin_kind TEXT NOT NULL,
            extraction_version TEXT NOT NULL,
            promotion TEXT NOT NULL,
            promotion_reason TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            promoted_observation_id UUID,
            review_id UUID,
            created_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_decision_candidates_span ON decision_candidates (receipt_id, extraction_version, span_sha256)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_decision_candidates_opportunity ON decision_candidates (opportunity_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS human_resolutions (
            id UUID PRIMARY KEY,
            opportunity_id UUID NOT NULL,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            receipt_id UUID,
            source_event_id UUID,
            candidate_id UUID,
            selected_choice TEXT NOT NULL DEFAULT '',
            correction_text TEXT NOT NULL DEFAULT '',
            stated_rationale TEXT,
            resolution_source TEXT NOT NULL,
            human_source_ref TEXT NOT NULL,
            observation_id UUID,
            supersedes_resolution_id UUID,
            resolved_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_human_resolutions_opportunity ON human_resolutions (opportunity_id, resolved_at DESC)")
    for ddl in (
        "opportunity_id UUID",
        "session_id TEXT",
        "turn INTEGER",
        "decision_family TEXT",
        "prediction_stage TEXT NOT NULL DEFAULT 'retrospective'",
        "frozen_at TIMESTAMPTZ",
        "evidence_cutoff_at TIMESTAMPTZ",
        "evidence_revision TEXT",
        "prediction_shown_at TIMESTAMPTZ",
        "advice_visible BOOLEAN NOT NULL DEFAULT false",
        "resolution_state TEXT NOT NULL DEFAULT 'resolved'",
        "resolved_at TIMESTAMPTZ",
        "resolution_source TEXT",
        "human_source_ref TEXT",
        "resolution_source_event_id UUID",
        "corrections_json JSONB NOT NULL DEFAULT '[]'::jsonb",
    ):
        op.execute(f"ALTER TABLE behavior_shadow_predictions ADD COLUMN IF NOT EXISTS {ddl}")
    op.execute("CREATE INDEX IF NOT EXISTS idx_behavior_shadow_open ON behavior_shadow_predictions (workspace_id, subject_user_id, resolution_state, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_behavior_shadow_opportunity ON behavior_shadow_predictions (opportunity_id)")
    for ddl in ("opportunity_id UUID", "origin_kind TEXT", "capture_receipt_id UUID", "extraction_version TEXT"):
        op.execute(f"ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS {ddl}")
    op.execute("CREATE INDEX IF NOT EXISTS idx_decision_obs_opportunity ON decision_observations (opportunity_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_decision_obs_opportunity")
    op.execute("DROP INDEX IF EXISTS idx_behavior_shadow_opportunity")
    op.execute("DROP INDEX IF EXISTS idx_behavior_shadow_open")
    op.execute("DROP INDEX IF EXISTS idx_human_resolutions_opportunity")
    op.execute("DROP INDEX IF EXISTS idx_decision_candidates_opportunity")
    op.execute("DROP INDEX IF EXISTS uq_decision_candidates_span")
    op.execute("DROP INDEX IF EXISTS idx_decision_opportunities_session")
    op.execute("DROP INDEX IF EXISTS idx_decision_opportunities_open")
    op.execute("DROP INDEX IF EXISTS idx_trusted_input_receipts_extraction")
    op.execute("DROP INDEX IF EXISTS idx_trusted_input_receipts_subject_ingested")
    op.execute("DROP INDEX IF EXISTS uq_trusted_input_receipts_delivery")
    for column in ("extraction_version", "capture_receipt_id", "origin_kind", "opportunity_id"):
        op.execute(f"ALTER TABLE decision_observations DROP COLUMN IF EXISTS {column}")
    for column in (
        "corrections_json",
        "resolution_source_event_id",
        "human_source_ref",
        "resolution_source",
        "resolved_at",
        "resolution_state",
        "advice_visible",
        "prediction_shown_at",
        "evidence_revision",
        "evidence_cutoff_at",
        "frozen_at",
        "prediction_stage",
        "decision_family",
        "turn",
        "session_id",
        "opportunity_id",
    ):
        op.execute(f"ALTER TABLE behavior_shadow_predictions DROP COLUMN IF EXISTS {column}")
    op.execute("DROP TABLE IF EXISTS human_resolutions")
    op.execute("DROP TABLE IF EXISTS decision_candidates")
    op.execute("DROP TABLE IF EXISTS decision_opportunities")
    op.execute("DROP TABLE IF EXISTS trusted_input_receipts")
