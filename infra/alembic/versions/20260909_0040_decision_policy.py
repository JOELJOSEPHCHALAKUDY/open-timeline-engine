"""decision policy: qualification records, threshold registrations, and the columns a decision needs

Two new tables and sixteen additive columns.  They exist because the tree can currently record
that a prediction was made, but not any of:

* did this decision family ever earn the right to use personalization  -> ``policy_qualifications``
* were the thresholds fixed before the data was looked at              -> ``threshold_registrations``
* which loader answered, which model ran, and against which prompt     -> the ``behavior_shadow_predictions`` columns
* was the human's answer already contaminated by advice about this
  very choice                                                          -> ``decision_advice_shown``
* which task episode was this, so a split cannot straddle one          -> ``episode_key``

Three design points a reviewer should check rather than assume:

* ``decision_advice_shown`` defaults to **true**, which is the conservative direction: a row
  written by a writer that has not been upgraded is *excluded* from the promotion gate rather
  than silently admitted to it.  The column it replaces in that role, ``advice_visible``, was
  produced as ``bool(<eight-key dict literal>)`` at six sites in two backends, so it was
  ``True`` unconditionally and never measured the property its name claims.
* ``candidate_option_count`` is stored rather than derived, because the promotion gate must
  exclude cases that offered fewer than two options and the option list is not retained on the
  shadow row.  Live, 32 of 34 observations carry fewer than two.
* ``threshold_registrations.registered_at`` is ``DEFAULT now()`` and has no client-supplied
  value.  This is tamper-*evident*, not tamper-proof: anyone with database access can update
  the row.  What it buys is that backdating becomes an explicit, separate, auditable act
  against a table with its own timestamps, rather than a one-character edit to the constant
  whose digest is being checked.

No calibration columns are added, in either table.  Nothing in this system is calibrated, and
a column with no producer is a column every future reader has to be told to ignore.

``downgrade()`` deliberately does **not** drop ``policy_qualifications`` or
``threshold_registrations``.  Those hold adjudication outcomes and a pre-registration audit
trail; a rolled-back deploy must leave them readable.  Since every ``CREATE`` is
``IF NOT EXISTS``, ``downgrade`` -> ``upgrade`` is still a clean round trip for the CI job that
runs one on every integration build.

Every CREATE is IF NOT EXISTS and every ALTER is ADD COLUMN IF NOT EXISTS, so a re-run is a
no-op.
"""

from __future__ import annotations

from alembic import op

revision = "20260909_0040"
# Parent: P3's charter_effects_verification.
# NOTE: no trailing comment on the assignment below - tests/integration/test_exit_gate_lite.py
# parses this file statically with a regex that captures to end of line.
down_revision = "20260909_0039"
branch_labels = None
depends_on = None


_OBSERVATION_COLUMNS = (
    "project_id TEXT NULL",
    "decision_family TEXT NULL",
    "task_id TEXT NULL",
    "episode_key TEXT NULL",
)

_SHADOW_COLUMNS = (
    "decision_policy_revision TEXT NULL",
    "model_id TEXT NULL",
    "runtime_version TEXT NULL",
    "prompt_sha256 TEXT NULL",
    "retrieval_version TEXT NULL",
    "episode_key TEXT NULL",
    "project_id TEXT NULL",
    "abstain_reason TEXT NULL",
    "ood_status TEXT NULL",
    "conflict_status TEXT NULL",
    "policy_score REAL NOT NULL DEFAULT 0",
    "exposed BOOLEAN NOT NULL DEFAULT false",
    "decision_advice_shown BOOLEAN NOT NULL DEFAULT true",
    "candidate_option_count INTEGER NOT NULL DEFAULT 0",
    "request_fingerprint TEXT NULL",
    "policy_json JSONB NOT NULL DEFAULT '{}'::jsonb",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_qualifications (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            project_id TEXT NULL,
            decision_family TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'not_qualified',
            decision_policy_revision TEXT NOT NULL,
            model_id TEXT NOT NULL DEFAULT '',
            runtime_version TEXT NOT NULL DEFAULT '',
            prompt_sha256 TEXT NOT NULL DEFAULT '',
            retrieval_version TEXT NOT NULL DEFAULT '',
            evidence_revision TEXT NULL,
            evidence_cutoff_at TIMESTAMPTZ NULL,
            learning_eligible_at_qualification INTEGER NOT NULL DEFAULT 0,
            thresholds_sha TEXT NOT NULL,
            thresholds_version TEXT NOT NULL DEFAULT '',
            tuning_sha TEXT NOT NULL,
            split_sha256 TEXT NULL,
            split_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            gate_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            shortfalls_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            adjudicated_count INTEGER NOT NULL DEFAULT 0,
            non_abstained_count INTEGER NOT NULL DEFAULT 0,
            coverage REAL NOT NULL DEFAULT 0,
            precision_lower_bound REAL NOT NULL DEFAULT 0,
            distinct_episodes INTEGER NOT NULL DEFAULT 0,
            duplicate_context_ratio REAL NOT NULL DEFAULT 0,
            baselines_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            exclusions_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_policy_qualifications_attempt
            ON policy_qualifications (workspace_id, subject_user_id, project_id, decision_family, created_at);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_policy_qualifications_live
            ON policy_qualifications (workspace_id, subject_user_id, decision_family, state, created_at DESC);
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS threshold_registrations (
            id UUID PRIMARY KEY,
            thresholds_sha TEXT NOT NULL,
            tuning_sha TEXT NOT NULL,
            thresholds_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            schema_version TEXT NOT NULL DEFAULT 'v1'
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_threshold_registrations_sha
            ON threshold_registrations (thresholds_sha, tuning_sha);
        """
    )

    for ddl in _OBSERVATION_COLUMNS:
        op.execute(f"ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS {ddl};")
    for ddl in _SHADOW_COLUMNS:
        op.execute(f"ALTER TABLE behavior_shadow_predictions ADD COLUMN IF NOT EXISTS {ddl};")
    op.execute("ALTER TABLE decision_opportunities ADD COLUMN IF NOT EXISTS episode_key TEXT NULL;")

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decision_obs_policy_scope
            ON decision_observations (workspace_id, subject_user_id, decision_family, situation_type);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_behavior_shadow_episode
            ON behavior_shadow_predictions (workspace_id, subject_user_id, decision_family, episode_key);
        """
    )

    # Backfill from the opportunity that promoted the observation.  This reaches P1-era
    # promoted rows only; everything older keeps NULL and is handled by the NULL-project rule
    # in the scope predicate (admissible evidence, never sufficient on its own).
    op.execute(
        """
        UPDATE decision_observations o
           SET project_id = d.project_id,
               decision_family = d.decision_family,
               task_id = d.task_id
          FROM decision_opportunities d
         WHERE o.opportunity_id = d.id
           AND o.project_id IS NULL;
        """
    )

    # Every stored fidelity run was computed under corpus-relative recency and a row-index
    # split, so none of them is comparable with anything measured after this revision.  They
    # are invalidated rather than migrated, and the qualification reporter refuses to read
    # them.  There are zero rows live, so this costs nothing today and prevents a silent
    # comparison later.
    op.execute("UPDATE behavior_fidelity_runs SET status = 'invalidated_by_p4';")


def downgrade() -> None:
    # policy_qualifications and threshold_registrations are NOT dropped.  They hold
    # adjudication outcomes and a pre-registration audit trail, and a rolled-back deploy must
    # leave them readable.  upgrade() is CREATE TABLE IF NOT EXISTS throughout, so the
    # downgrade/upgrade round trip stays clean without dropping them.
    op.execute("DROP INDEX IF EXISTS idx_behavior_shadow_episode;")
    op.execute("DROP INDEX IF EXISTS idx_decision_obs_policy_scope;")

    op.execute("ALTER TABLE decision_opportunities DROP COLUMN IF EXISTS episode_key;")
    for ddl in reversed(_SHADOW_COLUMNS):
        column = ddl.split(" ", 1)[0]
        op.execute(f"ALTER TABLE behavior_shadow_predictions DROP COLUMN IF EXISTS {column};")
    for ddl in reversed(_OBSERVATION_COLUMNS):
        column = ddl.split(" ", 1)[0]
        op.execute(f"ALTER TABLE decision_observations DROP COLUMN IF EXISTS {column};")
