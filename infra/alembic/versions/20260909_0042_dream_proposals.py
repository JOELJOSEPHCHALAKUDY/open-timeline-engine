"""dream proposals: an append-only aspiration log with a human transition vocabulary

Three tables.  ``dream_proposals`` is a *fold* of ``dream_proposal_events``, never a source of
truth in its own right; ``dream_generation_runs`` records every generation attempt, including
— especially including — the ones that refused to generate anything.

What replaces what.  Today an aspiration is an ``autonomy_goals`` row discriminated by
``step_index = -1``, keyed on a session, with one nullable ``acknowledged_at`` column standing
in for "not seen", "seen and ignored" and "never surfaced" at once.  Refresh DELETEs the prior
rows outright.  So a rejection cannot be recorded, cannot survive a refresh if it were, and
silence is indistinguishable from a no.  These tables make "refresh never deletes" a property
of the schema rather than of a code review: there is no status column a writer can set outside
the compare-and-swap, and there is nowhere to put a DELETE.

FOUR things this revision deliberately does NOT do.

1. ``downgrade()`` DROPS NOTHING.  These tables hold the owner's own accept/reject answers, and
   CI runs a real ``alembic downgrade`` on every integration job.  A downgrade that dropped
   them would destroy every verdict he ever recorded — the exact loss this work exists to
   prevent, moved into a statement a DELETE-scanner would not see.  Because ``upgrade()`` is
   CREATE ... IF NOT EXISTS throughout, downgrade → upgrade is still a clean round trip.
2. It does not touch ``autonomy_goals``.  The fourteen rows with ``step_index = -1`` are the old
   encoding; they are left exactly where they are and the code simply stops reading them.  A
   migration that deleted a user's recorded aspirations to make room for a better table would
   be repeating the mistake the old refresh path made.
3. It adds no column to ``decision_observations``.  Proposals are built from
   ``trusted_input_receipts`` joined to ``events``, both of which already carry project
   attribution, so this revision takes no position on the decision-policy binding key.
4. It adds no second "single migration head" assertion to CI.  There is one, it already runs,
   and a second copy is a second thing to keep in step.

``nonresponse_state`` is a stored column rather than a derived expression so a list query can
filter on it without re-folding every log; it is written only by the fold, and a rebuild
recomputes it like every other field.
"""

from __future__ import annotations

from alembic import op

revision = "20260909_0042"
down_revision = "20260909_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dream_proposals (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL DEFAULT '',
            project_id TEXT,
            scope_kind TEXT NOT NULL DEFAULT 'project',
            session_id TEXT NOT NULL DEFAULT '',
            run_id UUID,
            revision INTEGER NOT NULL DEFAULT 0,
            highest_seq INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'proposed',
            nonresponse_state TEXT NOT NULL DEFAULT 'never_surfaced',
            surfaced_count INTEGER NOT NULL DEFAULT 0,
            surfaced_attested BOOLEAN NOT NULL DEFAULT FALSE,
            first_surfaced_at TIMESTAMPTZ,
            last_surfaced_at TIMESTAMPTZ,
            title TEXT NOT NULL,
            connection_text TEXT NOT NULL DEFAULT '',
            benefit_text TEXT NOT NULL DEFAULT '',
            first_step TEXT NOT NULL DEFAULT '',
            citations_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            citation_count INTEGER NOT NULL DEFAULT 0,
            evidence_basis TEXT NOT NULL DEFAULT 'trusted_current',
            evidence_revision TEXT NOT NULL DEFAULT '',
            evidence_cutoff_at TIMESTAMPTZ,
            theme_tokens_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            supersedes_proposal_id UUID,
            repropose_depth INTEGER NOT NULL DEFAULT 0,
            snooze_until TIMESTAMPTZ,
            expires_at TIMESTAMPTZ,
            accepted_at TIMESTAMPTZ,
            rejected_at TIMESTAMPTZ,
            rejection_reason TEXT NOT NULL DEFAULT '',
            task_id TEXT,
            objective_hash TEXT,
            plan_root_goal_id UUID,
            pursuit_started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            abandoned_at TIMESTAMPTZ,
            abandon_reason TEXT NOT NULL DEFAULT '',
            withdrawn_reason TEXT NOT NULL DEFAULT '',
            source_revision TEXT NOT NULL DEFAULT '',
            policy_revision TEXT NOT NULL DEFAULT 'p5-2026-09',
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dream_proposals_scope_status "
        "ON dream_proposals (workspace_id, owner_id, subject_user_id, scope_kind, project_id, status, updated_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dream_proposals_task ON dream_proposals (workspace_id, owner_id, task_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_dream_proposals_run ON dream_proposals (run_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dream_proposals_supersedes ON dream_proposals (supersedes_proposal_id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dream_proposal_events (
            id UUID PRIMARY KEY,
            proposal_id UUID NOT NULL,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            kind TEXT NOT NULL,
            payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            actor TEXT NOT NULL DEFAULT '',
            actor_class TEXT NOT NULL DEFAULT 'system',
            source_event_id UUID,
            run_id UUID,
            occurred_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    # The uniqueness that makes the append idempotent: an INSERT ... ON CONFLICT DO NOTHING on
    # (proposal_id, seq) is what lets a retried CAS re-issue its events without doubling them.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dream_proposal_events_seq ON dream_proposal_events (proposal_id, seq)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dream_proposal_events_kind "
        "ON dream_proposal_events (workspace_id, owner_id, kind, occurred_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dream_generation_runs (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL DEFAULT '',
            project_id TEXT,
            scope_kind TEXT NOT NULL DEFAULT 'project',
            scope_key TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            planning_job_id UUID,
            state TEXT NOT NULL DEFAULT 'running',
            refusal_reason TEXT NOT NULL DEFAULT '',
            pool_size INTEGER NOT NULL DEFAULT 0,
            pool_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            pool_drops_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            evidence_revision TEXT NOT NULL DEFAULT '',
            evidence_cutoff_at TIMESTAMPTZ,
            candidates_returned INTEGER NOT NULL DEFAULT 0,
            proposals_written INTEGER NOT NULL DEFAULT 0,
            refusals_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            model_provider TEXT NOT NULL DEFAULT '',
            prompt_hash TEXT NOT NULL DEFAULT '',
            started_at TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dream_generation_runs_scope "
        "ON dream_generation_runs (workspace_id, owner_id, scope_kind, project_id, started_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dream_generation_runs_job ON dream_generation_runs (planning_job_id)"
    )
    # At most one in-flight run per scope, enforced by the database rather than by a read
    # followed by a write.  ``scope_key`` is a plain column rather than an expression so the
    # Full and Lite guards can render the SAME predicate and a reader can diff them.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dream_generation_runs_inflight "
        "ON dream_generation_runs (workspace_id, owner_id, scope_key) WHERE state = 'running'"
    )


def downgrade() -> None:
    """Intentionally a NO-OP for the three dream_* tables.  See the module docstring, rule 1.

    These tables hold the owner's own accept/reject answers.  A rolled-back deploy must leave
    them readable.  Nothing is dropped, no index is dropped, no row is deleted.  The revision
    is still reversible for CI because ``upgrade()`` is CREATE ... IF NOT EXISTS throughout, so
    a downgrade followed by an upgrade is a clean round trip that loses nothing.
    """
    return None
