"""charter, dispatch records, effect journal, acceptance criteria, verification and sandbox self-tests

Seven new tables and six additive columns.  They exist because the tree can currently answer
"was this actor allowed to act?" but not any of:

* under whose standing authority, with which roots and caps  -> ``authority_charters``
* which runtime, which sandbox, and what did it cost         -> ``dispatch_records``
* did the world change, and do we know                       -> ``effect_journal``
* what would count as done, frozen before the work began     -> ``acceptance_criteria``
* what evidence graded it, and who ran it                    -> ``verification_results``
* is the sandbox we claim actually holding, right now        -> ``sandbox_self_tests``

Two design points a reviewer should check rather than assume:

* ``effect_journal.lease_generation`` and ``claimed_executor`` are EVIDENCE columns — what the
  worker believed when it opened the row.  They are deliberately NOT the fence.  The fence is a JOIN
  against ``directive_executions`` at both open and resolve, because a CAS against a value the same
  worker wrote fences nothing.
* ``effect_journal.enforcement_tier`` and ``action_tracing`` are NOT NULL with no default.  A row
  that cannot say which boundary was in force is not evidence, and defaulting them would let one be
  written silently.

``charter_narrowings.revoked_at`` is deliberately absent: there is no revoke-narrowing route, so the
column would have neither a producer nor a reader.

Every CREATE is IF NOT EXISTS and every ALTER is ADD COLUMN IF NOT EXISTS, so a re-run is a no-op.
"""

from __future__ import annotations

from alembic import op

revision = "20260909_0039"
down_revision = "20260909_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS authority_charters (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            project_id TEXT NULL,
            charter_version TEXT NOT NULL,
            policy_revision TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            enforcement_tier TEXT NOT NULL,
            credential_risk_acknowledged BOOLEAN NOT NULL DEFAULT false,
            source_receipt_id UUID NOT NULL,
            permitted_roots_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            protected_write_prefixes_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            denied_read_paths_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            permitted_capabilities_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            confirm_required_capabilities_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            egress_mode TEXT NOT NULL DEFAULT 'deny_all',
            runtime_allowlist_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            task_families_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            max_concurrent_dispatches INTEGER NOT NULL DEFAULT 1,
            max_wall_seconds INTEGER NOT NULL DEFAULT 1800,
            budget_minor_units INTEGER NOT NULL DEFAULT 0,
            budget_currency TEXT NOT NULL DEFAULT 'USD',
            spend_enforcement TEXT NOT NULL DEFAULT 'unsupported',
            charter_digest TEXT NOT NULL,
            approved_by TEXT NULL,
            approved_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ NULL,
            revoke_reason TEXT NULL,
            superseded_by UUID NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_authority_charters_scope_status
            ON authority_charters (workspace_id, owner_id, status, expires_at);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS charter_narrowings (
            id UUID PRIMARY KEY,
            charter_id UUID NOT NULL,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            source_receipt_id UUID NOT NULL,
            narrowing_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_charter_narrowings_session
            ON charter_narrowings (workspace_id, session_id, created_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_charter_narrowings_charter
            ON charter_narrowings (charter_id, created_at);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dispatch_records (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            task_id TEXT NULL,
            directive_id UUID NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            charter_id UUID NOT NULL,
            charter_digest TEXT NOT NULL,
            claimed_by TEXT NOT NULL DEFAULT '',
            enforcement_tier TEXT NOT NULL,
            sandbox_provider TEXT NOT NULL,
            sandbox_profile_digest TEXT NULL,
            sandbox_self_test_id UUID NULL,
            runtime_id TEXT NOT NULL,
            runtime_version TEXT NOT NULL,
            surface TEXT NOT NULL,
            model_id TEXT NOT NULL DEFAULT '',
            contract_digest TEXT NOT NULL DEFAULT '',
            capability_matrix_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            provider_run_id TEXT NULL,
            provider_turn_id TEXT NULL,
            task_family TEXT NOT NULL DEFAULT 'unspecified',
            budget_reserved_minor_units INTEGER NOT NULL DEFAULT 0,
            budget_currency TEXT NOT NULL DEFAULT 'USD',
            spend_enforcement TEXT NOT NULL,
            cap_applied_json JSONB NULL,
            cost_minor_units INTEGER NULL,
            cost_source TEXT NULL,
            tokens_input INTEGER NOT NULL DEFAULT 0,
            tokens_output INTEGER NOT NULL DEFAULT 0,
            tokens_cached_input INTEGER NOT NULL DEFAULT 0,
            tokens_reasoning INTEGER NOT NULL DEFAULT 0,
            human_intervention_count INTEGER NOT NULL DEFAULT 0,
            outcome TEXT NULL,
            terminal_reason TEXT NULL,
            wall_ms INTEGER NULL,
            started_at TIMESTAMPTZ NULL,
            finished_at TIMESTAMPTZ NULL,
            reconciled_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            schema_version TEXT NOT NULL DEFAULT 'v1'
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_dispatch_records_directive_attempt
            ON dispatch_records (directive_id, attempt);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dispatch_records_open
            ON dispatch_records (workspace_id, outcome, started_at);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS effect_journal (
            effect_id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            task_id TEXT NULL,
            directive_id UUID NOT NULL,
            dispatch_id UUID NULL,
            seq INTEGER NOT NULL,
            state TEXT NOT NULL,
            kind TEXT NOT NULL,
            reversibility TEXT NOT NULL,
            capability TEXT NOT NULL,
            resource TEXT NOT NULL,
            argv_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            description TEXT NOT NULL DEFAULT '',
            intent_digest TEXT NOT NULL,
            enforcement_tier TEXT NOT NULL,
            action_tracing TEXT NOT NULL,
            lease_generation INTEGER NOT NULL DEFAULT 0,
            claimed_executor TEXT NULL,
            provider_run_id TEXT NULL,
            provider_turn_id TEXT NULL,
            runtime_id TEXT NULL,
            runtime_version TEXT NULL,
            model_id TEXT NULL,
            opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at TIMESTAMPTZ NULL,
            resolution_source TEXT NULL,
            resolved_by_actor TEXT NULL,
            evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            schema_version TEXT NOT NULL DEFAULT 'v1'
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_effect_journal_intent
            ON effect_journal (directive_id, intent_digest);
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_effect_journal_seq
            ON effect_journal (directive_id, seq);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_effect_journal_open
            ON effect_journal (workspace_id, state, opened_at);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_effect_journal_session_open
            ON effect_journal (workspace_id, owner_id, session_id, state);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS acceptance_criteria (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            task_id TEXT NULL,
            directive_id UUID NOT NULL,
            charter_id UUID NULL,
            checks_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            criteria_digest TEXT NOT NULL,
            corpus_digest TEXT NOT NULL,
            corpus_manifest_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            frozen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            frozen_by TEXT NOT NULL,
            policy_revision TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_acceptance_criteria_directive
            ON acceptance_criteria (directive_id);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS verification_results (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            task_id TEXT NULL,
            directive_id UUID NOT NULL,
            criteria_id UUID NULL,
            criteria_digest_at_run TEXT NOT NULL,
            corpus_digest_at_run TEXT NOT NULL,
            criteria_digest_match BOOLEAN NOT NULL DEFAULT false,
            corpus_digest_match BOOLEAN NOT NULL DEFAULT false,
            verdict TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            runner_principal TEXT NOT NULL,
            executing_identity TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            commit_sha TEXT NULL,
            tree_sha TEXT NULL,
            checks_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            reviewer_model_json JSONB NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            schema_version TEXT NOT NULL DEFAULT 'v1'
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_verification_results_directive
            ON verification_results (directive_id, recorded_at DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sandbox_self_tests (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            host_id TEXT NOT NULL,
            sandbox_provider TEXT NOT NULL,
            provider_version TEXT NOT NULL DEFAULT '',
            profile_digest TEXT NOT NULL DEFAULT '',
            assertions_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            passed BOOLEAN NOT NULL DEFAULT false,
            uid_separation BOOLEAN NOT NULL DEFAULT false,
            ran_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            schema_version TEXT NOT NULL DEFAULT 'v1'
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sandbox_self_tests_recent
            ON sandbox_self_tests (workspace_id, sandbox_provider, ran_at DESC);
        """
    )

    op.execute("ALTER TABLE directive_executions ADD COLUMN IF NOT EXISTS dispatch_id UUID NULL;")
    op.execute("ALTER TABLE directive_executions ADD COLUMN IF NOT EXISTS charter_id UUID NULL;")
    op.execute("ALTER TABLE execution_permits    ADD COLUMN IF NOT EXISTS charter_id UUID NULL;")
    op.execute("ALTER TABLE execution_permits    ADD COLUMN IF NOT EXISTS charter_version TEXT NULL;")
    op.execute("ALTER TABLE capability_grants    ADD COLUMN IF NOT EXISTS charter_id UUID NULL;")
    op.execute("ALTER TABLE handoff_records      ADD COLUMN IF NOT EXISTS verification_state TEXT NULL;")


def downgrade() -> None:
    op.execute("ALTER TABLE handoff_records      DROP COLUMN IF EXISTS verification_state;")
    op.execute("ALTER TABLE capability_grants    DROP COLUMN IF EXISTS charter_id;")
    op.execute("ALTER TABLE execution_permits    DROP COLUMN IF EXISTS charter_version;")
    op.execute("ALTER TABLE execution_permits    DROP COLUMN IF EXISTS charter_id;")
    op.execute("ALTER TABLE directive_executions DROP COLUMN IF EXISTS charter_id;")
    op.execute("ALTER TABLE directive_executions DROP COLUMN IF EXISTS dispatch_id;")

    op.execute("DROP INDEX IF EXISTS idx_sandbox_self_tests_recent;")
    op.execute("DROP INDEX IF EXISTS idx_verification_results_directive;")
    op.execute("DROP INDEX IF EXISTS uq_acceptance_criteria_directive;")
    op.execute("DROP INDEX IF EXISTS idx_effect_journal_session_open;")
    op.execute("DROP INDEX IF EXISTS idx_effect_journal_open;")
    op.execute("DROP INDEX IF EXISTS uq_effect_journal_seq;")
    op.execute("DROP INDEX IF EXISTS uq_effect_journal_intent;")
    op.execute("DROP INDEX IF EXISTS idx_dispatch_records_open;")
    op.execute("DROP INDEX IF EXISTS uq_dispatch_records_directive_attempt;")
    op.execute("DROP INDEX IF EXISTS idx_charter_narrowings_charter;")
    op.execute("DROP INDEX IF EXISTS idx_charter_narrowings_session;")
    op.execute("DROP INDEX IF EXISTS idx_authority_charters_scope_status;")

    op.execute("DROP TABLE IF EXISTS sandbox_self_tests;")
    op.execute("DROP TABLE IF EXISTS verification_results;")
    op.execute("DROP TABLE IF EXISTS acceptance_criteria;")
    op.execute("DROP TABLE IF EXISTS effect_journal;")
    op.execute("DROP TABLE IF EXISTS dispatch_records;")
    op.execute("DROP TABLE IF EXISTS charter_narrowings;")
    op.execute("DROP TABLE IF EXISTS authority_charters;")
