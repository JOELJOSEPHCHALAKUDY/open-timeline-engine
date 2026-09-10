"""trust boundary: directive lease fencing, permit binding, scoped continuity"""

from __future__ import annotations

from alembic import op

revision = "20260909_0035"
down_revision = "20260802_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # directive_executions: lease/fencing + verification + report idempotency
    op.execute("ALTER TABLE directive_executions ADD COLUMN IF NOT EXISTS lease_generation INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE directive_executions ADD COLUMN IF NOT EXISTS claimed_executor TEXT")
    op.execute("ALTER TABLE directive_executions ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ")
    op.execute("ALTER TABLE directive_executions ADD COLUMN IF NOT EXISTS verification_state TEXT NOT NULL DEFAULT 'unverified'")
    op.execute("ALTER TABLE directive_executions ADD COLUMN IF NOT EXISTS report_idempotency_key TEXT")
    op.execute("ALTER TABLE directive_executions ADD COLUMN IF NOT EXISTS report_payload_hash TEXT")
    op.execute("ALTER TABLE directive_executions ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ")
    op.execute("ALTER TABLE directive_executions ADD COLUMN IF NOT EXISTS cancel_reason TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS idx_directive_executions_report_idem ON directive_executions (workspace_id, report_idempotency_key)")
    # execution_permits: bind to user / directive / attempt / objective / scope
    op.execute("ALTER TABLE execution_permits ADD COLUMN IF NOT EXISTS user_id TEXT")
    op.execute("ALTER TABLE execution_permits ADD COLUMN IF NOT EXISTS requested_by TEXT")
    op.execute("ALTER TABLE execution_permits ADD COLUMN IF NOT EXISTS directive_id UUID")
    op.execute("ALTER TABLE execution_permits ADD COLUMN IF NOT EXISTS attempt INTEGER")
    op.execute("ALTER TABLE execution_permits ADD COLUMN IF NOT EXISTS objective_hash TEXT")
    op.execute("ALTER TABLE execution_permits ADD COLUMN IF NOT EXISTS policy_revision TEXT")
    op.execute("ALTER TABLE execution_permits ADD COLUMN IF NOT EXISTS scope_digest TEXT")
    op.execute("ALTER TABLE execution_permits ADD COLUMN IF NOT EXISTS resolved_by TEXT")
    # continuity: project / executor binding and reader-vs-source session split
    op.execute("ALTER TABLE handoff_records ADD COLUMN IF NOT EXISTS project_id TEXT")
    op.execute("ALTER TABLE handoff_records ADD COLUMN IF NOT EXISTS git_remote TEXT")
    op.execute("ALTER TABLE handoff_records ADD COLUMN IF NOT EXISTS executor_id TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS idx_handoff_records_workspace_project_ts ON handoff_records (workspace_id, project_id, ts DESC)")
    op.execute("ALTER TABLE handoff_outbox ADD COLUMN IF NOT EXISTS executor_id TEXT")
    op.execute("ALTER TABLE handoff_outbox ADD COLUMN IF NOT EXISTS payload_hash TEXT")
    op.execute("ALTER TABLE continuity_resume_attempts ADD COLUMN IF NOT EXISTS source_session_id TEXT")


def downgrade() -> None:
    # Indexes first: CI runs a real downgrade/upgrade cycle.
    op.execute("DROP INDEX IF EXISTS idx_handoff_records_workspace_project_ts")
    op.execute("DROP INDEX IF EXISTS idx_directive_executions_report_idem")
    op.execute("ALTER TABLE continuity_resume_attempts DROP COLUMN IF EXISTS source_session_id")
    op.execute("ALTER TABLE handoff_outbox DROP COLUMN IF EXISTS payload_hash")
    op.execute("ALTER TABLE handoff_outbox DROP COLUMN IF EXISTS executor_id")
    for column in ("executor_id", "git_remote", "project_id"):
        op.execute(f"ALTER TABLE handoff_records DROP COLUMN IF EXISTS {column}")
    for column in ("resolved_by", "scope_digest", "policy_revision", "objective_hash", "attempt", "directive_id", "requested_by", "user_id"):
        op.execute(f"ALTER TABLE execution_permits DROP COLUMN IF EXISTS {column}")
    for column in ("cancel_reason", "cancelled_at", "report_payload_hash", "report_idempotency_key", "verification_state", "lease_expires_at", "claimed_executor", "lease_generation"):
        op.execute(f"ALTER TABLE directive_executions DROP COLUMN IF EXISTS {column}")
