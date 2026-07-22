"""add durable handoff outbox, completion obligations, and continuity pilot"""

from __future__ import annotations

from alembic import op

revision = "20260721_0030"
down_revision = "20260721_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS handoff_outbox (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            behavior_subject_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            directive_id UUID NULL,
            completion_key TEXT NOT NULL,
            terminal_state TEXT NOT NULL,
            milestone_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            source TEXT NOT NULL DEFAULT 'native',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_error TEXT NULL,
            event_id UUID NOT NULL,
            handoff_record_id UUID NOT NULL,
            redaction_applied BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            delivered_at TIMESTAMPTZ NULL,
            UNIQUE(workspace_id, owner_id, completion_key)
        );
        CREATE INDEX IF NOT EXISTS idx_handoff_outbox_delivery
            ON handoff_outbox (status, next_attempt_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_handoff_outbox_scope
            ON handoff_outbox (workspace_id, owner_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS continuity_resume_attempts (
            id UUID PRIMARY KEY,
            packet_id UUID NOT NULL UNIQUE,
            workspace_id TEXT NOT NULL,
            requesting_owner_id TEXT NOT NULL,
            target_owner_id TEXT NOT NULL,
            selected_record_id UUID NOT NULL,
            query_text TEXT NOT NULL,
            top_file TEXT NULL,
            requested_at TIMESTAMPTZ NOT NULL,
            returned_at TIMESTAMPTZ NOT NULL,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            time_since_handoff_ms BIGINT NOT NULL DEFAULT 0,
            opened_file TEXT NULL,
            correct_file BOOLEAN NULL,
            correction_required BOOLEAN NULL,
            correction_reason TEXT NOT NULL DEFAULT '',
            feedback_at TIMESTAMPTZ NULL
        );
        CREATE INDEX IF NOT EXISTS idx_continuity_resume_scope
            ON continuity_resume_attempts (workspace_id, requesting_owner_id, requested_at DESC);

        ALTER TABLE capability_grants
            ADD COLUMN IF NOT EXISTS completion_required BOOLEAN NOT NULL DEFAULT false;
        ALTER TABLE capability_grants
            ADD COLUMN IF NOT EXISTS completion_outbox_id UUID NULL;
        ALTER TABLE capability_grants
            ADD COLUMN IF NOT EXISTS completion_recorded_at TIMESTAMPTZ NULL;
        CREATE INDEX IF NOT EXISTS idx_capability_grants_completion_obligation
            ON capability_grants (workspace_id, owner_id, session_id, completion_required, completion_recorded_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_capability_grants_completion_obligation")
    op.execute("ALTER TABLE capability_grants DROP COLUMN IF EXISTS completion_recorded_at")
    op.execute("ALTER TABLE capability_grants DROP COLUMN IF EXISTS completion_outbox_id")
    op.execute("ALTER TABLE capability_grants DROP COLUMN IF EXISTS completion_required")
    op.execute("DROP TABLE IF EXISTS continuity_resume_attempts")
    op.execute("DROP TABLE IF EXISTS handoff_outbox")
