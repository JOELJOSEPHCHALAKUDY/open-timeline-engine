"""pilot enrolment: an arm allocated before the work starts, and an adjudication only a human writes

Five tables, all new, touching nothing P3, P4 or P5 owns.

``pilot_strata`` is the allocator's counter and nothing else.  Its single ``INSERT ... ON
CONFLICT (stratum_id) DO UPDATE SET next_slot = next_slot + 1 RETURNING next_slot - 1`` is what
makes a permuted block *exact* rather than balanced-in-expectation, and it is why P6 does not
hash an identifier into an arm: a hash is balanced only on average, and any caller who can vary a
component of the hashed key can draw again.  Here the only input a caller controls is the work
itself — ``episode_key`` is P4's six-component digest, computed server-side, never accepted on
the wire — so a re-enrolment returns the ORIGINAL arm rather than a fresh one.

``pilot_episodes`` carries the frozen allocation.  The row commits before the response carrying
the arm is built, ``revealed_at`` is stamped on the first read, and there is deliberately no
statement anywhere in this repository of the form ``UPDATE pilot_episodes SET arm_id`` — a unit
test scans every SQL literal under ``shared/``, ``services/`` and this directory for one.

``pilot_episode_closes`` is the adjudication and holds the owner's own answers: what actually
ran, whether he had to rescue it, how long the review took, and whether he accepted the result.
``adjudication_independent`` is computed by the server from ``adjudicator_id`` against the
enrolling ``agent_principal``, so independence is a property of the row rather than a promise in
a runbook.  ``pilot_episode_observations`` is the executor's own account of its run; it is
diagnostic, it lives in its own table precisely so that no clause function can reach it, and
``producer_class`` says so on every row.

FOUR things this revision deliberately does NOT do.

1. ``downgrade()`` DROPS NOTHING.  ``pilot_episode_closes`` and ``dream_relevance_adjudications``
   are nothing but human answers, and CI runs a real ``alembic downgrade`` on every integration
   job.  Because ``upgrade()`` is CREATE ... IF NOT EXISTS throughout, downgrade -> upgrade is
   still a clean round trip.  Same shape, same reason as ``20260909_0042``.
2. It adds no ``dream_proposals`` column, no status value and no nonresponse value.  P5 already
   ships that vocabulary and P6 composes with it.  What P6 adds is the one thing P5 correctly did
   not: an adjudication of *relevance* that is independent of acceptance, in its own table.
3. It does not extend ``behavior_projection_pilot_assignments.variant``.  That column means
   *memory-context format inside one runtime*; P6's arm is a *runtime / workflow* allocation.
   Conflating them would silently merge two different comparisons under one name.
4. It stores no ``success`` column.  The Claim A composite is recomputed at read time from its
   stored components, so a later change to the definition cannot silently disagree with history.
"""

from __future__ import annotations

from alembic import op

revision = "20260909_0043"
down_revision = "20260909_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- the allocator's counter -------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pilot_strata (
            stratum_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL DEFAULT '',
            project_id TEXT,
            decision_family TEXT NOT NULL,
            arm_set_sha TEXT NOT NULL DEFAULT '',
            allocation_salt_sha256 TEXT NOT NULL DEFAULT '',
            next_slot INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pilot_strata_scope "
        "ON pilot_strata (workspace_id, subject_user_id, project_id, decision_family)"
    )

    # --- the enrolment record: written once, never updated except revealed_at -----------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pilot_episodes (
            id UUID PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL DEFAULT '',
            project_id TEXT,
            decision_family TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            objective_hash TEXT NOT NULL DEFAULT '',
            cancel_epoch INTEGER NOT NULL DEFAULT 0,
            episode_key TEXT NOT NULL,
            task_id TEXT,
            stratum_id TEXT NOT NULL,
            slot INTEGER NOT NULL DEFAULT -1,
            block_ordinal INTEGER NOT NULL DEFAULT -1,
            block_position INTEGER NOT NULL DEFAULT -1,
            arm_id TEXT NOT NULL,
            arm_class TEXT NOT NULL DEFAULT 'runtime',
            allocation_kind TEXT NOT NULL DEFAULT 'randomized',
            arm_set_sha TEXT NOT NULL DEFAULT '',
            allocation_salt_sha256 TEXT NOT NULL DEFAULT '',
            agent_principal TEXT NOT NULL DEFAULT '',
            allocated_at TIMESTAMPTZ NOT NULL,
            revealed_at TIMESTAMPTZ,
            enrolled_before_execution BOOLEAN NOT NULL DEFAULT TRUE,
            thresholds_sha TEXT NOT NULL DEFAULT '',
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    # The uniqueness that makes the arm un-shoppable: the same work re-enrolled collides here
    # and the store returns the ORIGINAL row with reused=true instead of drawing again.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pilot_episodes_episode "
        "ON pilot_episodes (workspace_id, subject_user_id, episode_key)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pilot_episodes_cell "
        "ON pilot_episodes (workspace_id, subject_user_id, project_id, decision_family, arm_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_pilot_episodes_block ON pilot_episodes (stratum_id, block_ordinal)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_pilot_episodes_allocated_at ON pilot_episodes (allocated_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_pilot_episodes_session ON pilot_episodes (workspace_id, session_id)")

    # --- the adjudication record: one per episode, written by a verified human -----------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pilot_episode_closes (
            id UUID PRIMARY KEY,
            episode_id UUID NOT NULL,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            adjudicator_id TEXT NOT NULL DEFAULT '',
            adjudicator_verified BOOLEAN NOT NULL DEFAULT FALSE,
            adjudication_independent BOOLEAN NOT NULL DEFAULT FALSE,
            executed_arm TEXT NOT NULL,
            deviated BOOLEAN NOT NULL DEFAULT FALSE,
            deviation_reason TEXT NOT NULL DEFAULT '',
            rescue_level TEXT NOT NULL DEFAULT 'none',
            finished BOOLEAN NOT NULL DEFAULT FALSE,
            completion_basis TEXT NOT NULL DEFAULT 'unfinished',
            review_minutes INTEGER NOT NULL DEFAULT 0,
            review_verdict TEXT NOT NULL DEFAULT 'accepted_as_is',
            unfinished_reason TEXT NOT NULL DEFAULT '',
            late_close BOOLEAN NOT NULL DEFAULT FALSE,
            closed_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_pilot_episode_closes_episode ON pilot_episode_closes (episode_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pilot_episode_closes_closed_at "
        "ON pilot_episode_closes (workspace_id, closed_at)"
    )

    # --- the executor's own account: diagnostics, and no gate clause reads it ------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pilot_episode_observations (
            id UUID PRIMARY KEY,
            episode_id UUID NOT NULL,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            principal TEXT NOT NULL DEFAULT '',
            producer_class TEXT NOT NULL DEFAULT 'agent_asserted',
            agent_notes TEXT NOT NULL DEFAULT '',
            agent_declared_steps_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            agent_self_rated_difficulty INTEGER,
            observed_at TIMESTAMPTZ NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pilot_episode_observations_episode "
        "ON pilot_episode_observations (episode_id, observed_at)"
    )

    # --- relevance, adjudicated independently of acceptance -----------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dream_relevance_adjudications (
            id UUID PRIMARY KEY,
            proposal_id UUID NOT NULL,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL DEFAULT '',
            project_id TEXT,
            adjudicator_id TEXT NOT NULL DEFAULT '',
            adjudicator_verified BOOLEAN NOT NULL DEFAULT FALSE,
            relevance TEXT NOT NULL,
            rationale TEXT NOT NULL DEFAULT '',
            blind_claimed BOOLEAN NOT NULL DEFAULT FALSE,
            blind_verified BOOLEAN NOT NULL DEFAULT FALSE,
            delivery_useful TEXT NOT NULL DEFAULT '',
            counted_for_delivery BOOLEAN NOT NULL DEFAULT FALSE,
            adjudicated_at TIMESTAMPTZ NOT NULL,
            supersedes_adjudication_id UUID,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dream_relevance_adjudications_proposal "
        "ON dream_relevance_adjudications (proposal_id, adjudicated_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_dream_relevance_adjudications_scope "
        "ON dream_relevance_adjudications (workspace_id, owner_id, project_id, adjudicated_at)"
    )


def downgrade() -> None:
    """Intentionally a NO-OP for all five tables.  See the module docstring, rule 1.

    ``pilot_episode_closes`` and ``dream_relevance_adjudications`` hold the owner's own
    adjudications, and ``pilot_episodes`` holds allocations that were frozen before the work
    started — an allocation that can be re-drawn after the fact is not an allocation.  A rolled
    back deploy must leave all of it readable.  Nothing is dropped, no index is dropped, and no
    row is deleted.
    """
    return None
