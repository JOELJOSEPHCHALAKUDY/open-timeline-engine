"""measure active continuity progress separately from handoff age"""

from __future__ import annotations

from alembic import op

revision = "20260723_0032"
down_revision = "20260722_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE continuity_resume_attempts
            ADD COLUMN IF NOT EXISTS session_id TEXT NOT NULL DEFAULT 'default';
        ALTER TABLE continuity_resume_attempts
            ADD COLUMN IF NOT EXISTS recommended_files_json JSONB NOT NULL DEFAULT '[]'::jsonb;
        ALTER TABLE continuity_resume_attempts
            ADD COLUMN IF NOT EXISTS opened_file_rank INTEGER NULL;
        ALTER TABLE continuity_resume_attempts
            ADD COLUMN IF NOT EXISTS first_file_opened_at TIMESTAMPTZ NULL;
        ALTER TABLE continuity_resume_attempts
            ADD COLUMN IF NOT EXISTS productive_at TIMESTAMPTZ NULL;
        ALTER TABLE continuity_resume_attempts
            ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ NULL;
        ALTER TABLE continuity_resume_attempts
            ADD COLUMN IF NOT EXISTS correct_anchor BOOLEAN NULL;
        ALTER TABLE continuity_resume_attempts
            ADD COLUMN IF NOT EXISTS archaeology_tool_calls INTEGER NULL;
        ALTER TABLE continuity_resume_attempts
            ADD COLUMN IF NOT EXISTS archaeology_tokens INTEGER NULL;
        ALTER TABLE continuity_resume_attempts
            ADD COLUMN IF NOT EXISTS outcome_status TEXT NULL;
        ALTER TABLE continuity_resume_attempts
            ADD COLUMN IF NOT EXISTS progress_source TEXT NOT NULL DEFAULT 'resume_packet';
        CREATE INDEX IF NOT EXISTS idx_continuity_resume_session
            ON continuity_resume_attempts (
                workspace_id, requesting_owner_id, session_id, requested_at DESC
            );
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_continuity_resume_session")
    for column in (
        "progress_source",
        "outcome_status",
        "archaeology_tokens",
        "archaeology_tool_calls",
        "correct_anchor",
        "completed_at",
        "productive_at",
        "first_file_opened_at",
        "opened_file_rank",
        "recommended_files_json",
        "session_id",
    ):
        op.execute(f"ALTER TABLE continuity_resume_attempts DROP COLUMN IF EXISTS {column}")
