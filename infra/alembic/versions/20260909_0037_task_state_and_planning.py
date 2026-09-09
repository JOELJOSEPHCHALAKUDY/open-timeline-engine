"""durable task state: versioned projection, immutable task state events, planning jobs, verifications

Additive only. Every CREATE is IF NOT EXISTS and every ALTER is ADD COLUMN IF NOT EXISTS, so a
re-run is a no-op and a rolled-back deploy leaves readable rows behind.

Two tables this revision deliberately does NOT touch: ``directive_executions`` and
``execution_permits``. Every column an earlier draft added to them had no producer — the mint
INSERT is driven by a frozen column list, and invalidation keys on ``session_id`` +
``objective_hash``, both of which already exist and are already indexed. A declared column that
nothing writes is a column every future reader has to be told to ignore.
"""

from __future__ import annotations

from alembic import op

revision = "20260909_0037"
down_revision = "20260909_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_states (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            project_id TEXT,
            revision INTEGER NOT NULL DEFAULT 0,
            contract_revision INTEGER NOT NULL DEFAULT 0,
            highest_seq BIGINT NOT NULL DEFAULT 0,
            objective_text TEXT NOT NULL DEFAULT '',
            objective_hash TEXT,
            objective_set_at TIMESTAMPTZ,
            objective_set_seq BIGINT NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'awaiting_objective',
            next_permitted_action TEXT NOT NULL DEFAULT 'await_owner_objective',
            plan_state TEXT NOT NULL DEFAULT 'absent',
            plan_producer TEXT,
            plan_root_goal_id UUID,
            planning_job_id UUID,
            constraints_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            open_decisions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            plan_json JSONB,
            unresolved_effects_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            latest_verification_json JSONB,
            citations_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            source_revision TEXT NOT NULL DEFAULT 'empty',
            cancelled_at TIMESTAMPTZ,
            cancelled_seq BIGINT NOT NULL DEFAULT 0,
            last_cancel_seq BIGINT NOT NULL DEFAULT 0,
            cancel_reason TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_task_states_identity "
        "ON task_states (workspace_id, owner_id, task_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_states_session "
        "ON task_states (workspace_id, session_id, updated_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_state_events (
            id UUID PRIMARY KEY,
            task_state_id UUID NOT NULL,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            seq BIGINT NOT NULL,
            kind TEXT NOT NULL,
            contract_revision INTEGER NOT NULL DEFAULT 0,
            payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            source_event_id UUID,
            directive_id UUID,
            goal_id UUID,
            actor TEXT NOT NULL DEFAULT '',
            occurred_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_task_state_events_seq ON task_state_events (task_state_id, seq)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_state_events_task ON task_state_events (workspace_id, task_id, seq)"
    )
    # Load-bearing for truncation pinning: the window query finds the latest OBJECTIVE_SET in
    # one index hit instead of scanning a 3000-event task.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_state_events_kind "
        "ON task_state_events (task_state_id, kind, seq DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS planning_jobs (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            task_state_id UUID,
            job_kind TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            input_revision TEXT NOT NULL DEFAULT '',
            contract_revision INTEGER NOT NULL DEFAULT 0,
            objective_hash TEXT,
            objective_text TEXT NOT NULL DEFAULT '',
            charter_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            scope_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            state TEXT NOT NULL DEFAULT 'pending',
            lease_owner TEXT,
            lease_until TIMESTAMPTZ,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            next_attempt_at TIMESTAMPTZ,
            last_error TEXT,
            producer TEXT,
            result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            queue_state TEXT NOT NULL DEFAULT 'inline',
            rq_job_id TEXT,
            cancel_requested BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_planning_jobs_idem ON planning_jobs (workspace_id, idempotency_key)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_planning_jobs_dispatch ON planning_jobs (state, next_attempt_at)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_planning_jobs_task ON planning_jobs (workspace_id, task_id, created_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_verifications (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            directive_id UUID,
            state TEXT NOT NULL DEFAULT 'unverified',
            method TEXT NOT NULL DEFAULT 'none',
            summary TEXT NOT NULL DEFAULT '',
            evidence_event_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            recorded_by TEXT NOT NULL DEFAULT '',
            recorded_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1',
            contract_revision INTEGER NOT NULL DEFAULT 0,
            plan_id TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_verifications_task "
        "ON task_verifications (workspace_id, task_id, recorded_at DESC)"
    )

    # The plan's step rows live in autonomy_goals, so the plan identity has to reach them.
    # `mutating` is new here, so there is no prior default to flip: the default IS false,
    # because the default producer is the read-only diagnosis plan and a wrong default would
    # be a silently wrong record.
    for statement in (
        "ALTER TABLE autonomy_goals ADD COLUMN IF NOT EXISTS plan_contract_revision INTEGER",
        "ALTER TABLE autonomy_goals ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE autonomy_goals ADD COLUMN IF NOT EXISTS blocked_reason TEXT",
        "ALTER TABLE autonomy_goals ADD COLUMN IF NOT EXISTS depends_on_json JSONB NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE autonomy_goals ADD COLUMN IF NOT EXISTS mutating BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE handoff_records ADD COLUMN IF NOT EXISTS task_id TEXT",
        "ALTER TABLE handoff_records ADD COLUMN IF NOT EXISTS task_state_revision INTEGER",
        "ALTER TABLE handoff_records ADD COLUMN IF NOT EXISTS contract_revision INTEGER",
        "ALTER TABLE handoff_records ADD COLUMN IF NOT EXISTS verification_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE handoff_records ADD COLUMN IF NOT EXISTS unresolved_effects_json JSONB NOT NULL DEFAULT '[]'::jsonb",
    ):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_task_verifications_task")
    op.execute("DROP INDEX IF EXISTS idx_planning_jobs_task")
    op.execute("DROP INDEX IF EXISTS idx_planning_jobs_dispatch")
    op.execute("DROP INDEX IF EXISTS uq_planning_jobs_idem")
    op.execute("DROP INDEX IF EXISTS idx_task_state_events_kind")
    op.execute("DROP INDEX IF EXISTS idx_task_state_events_task")
    op.execute("DROP INDEX IF EXISTS uq_task_state_events_seq")
    op.execute("DROP INDEX IF EXISTS idx_task_states_session")
    op.execute("DROP INDEX IF EXISTS uq_task_states_identity")
    for column in (
        "unresolved_effects_json",
        "verification_refs_json",
        "contract_revision",
        "task_state_revision",
        "task_id",
    ):
        op.execute(f"ALTER TABLE handoff_records DROP COLUMN IF EXISTS {column}")
    for column in (
        "mutating",
        "depends_on_json",
        "blocked_reason",
        "attempt_count",
        "plan_contract_revision",
    ):
        op.execute(f"ALTER TABLE autonomy_goals DROP COLUMN IF EXISTS {column}")
    op.execute("DROP TABLE IF EXISTS task_verifications")
    op.execute("DROP TABLE IF EXISTS planning_jobs")
    op.execute("DROP TABLE IF EXISTS task_state_events")
    op.execute("DROP TABLE IF EXISTS task_states")
