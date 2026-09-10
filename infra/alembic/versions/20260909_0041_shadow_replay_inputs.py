"""shadow replay inputs: store what the decision was actually offered, so replay can rebuild it

Four additive columns on ``behavior_shadow_predictions``.  They exist because P4 promised that
"the evaluated route is the deployed route", and today that promise cannot be checked: the
qualification harness replays a frozen row by *re-retrieving* evidence with a fresh, unscoped
``SELECT ... LIMIT 40`` and stamping literals over the identity fields.  A re-retrieval is not a
replay.  It answers "what would the policy decide about this situation now", which is a
different and much weaker question than "did the policy decide what the record says it decided".

The instrument that settles it already exists: ``DecisionRequest.fingerprint()``, persisted per
row as ``request_fingerprint``.  It hashes eleven terms.  Seven of them are already recoverable
from the stored row -- ``decision_family`` and ``situation_type`` from the row and its
``query_json``, ``candidate_options`` from the joined ``decision_opportunities.alternatives_json``
(see ``tce_api.policy_store.REPLAY_CANDIDATE_COLUMN``), ``evidence_revision`` and
``retrieval_version`` from the row, and ``decision_policy_revision`` / ``thresholds_sha`` from the
frozen constants.  The four added here are exactly the four that were **not**:

``offered_evidence_ids_json``
    The evidence set the decision was offered -- not the subset it cited.  ``policy_json``
    carries only ``evidence_observation_ids``, the CITED ids, and the fingerprint hashes the
    OFFERED ids.  So for any row with non-empty evidence the decision could not be reconstructed
    at all.  The list is bounded by the retrieval limit (``MAX_NEIGHBOURS`` is 12; the loader
    caps at ``MAX_NEIGHBOURS * 8``), so this is a few hundred bytes against the one property that
    makes the whole promotion gate mean something.

``decision_at``
    The write path calls ``datetime.now(tz=UTC)`` **twice**: once for
    ``build_decision_request(decision_at=...)`` and again, later in the same block, for
    ``frozen_at``/``created_at`` (main.py, ``_freeze_decision_opportunity``).  The two differ by
    the evidence load, so replaying with ``created_at`` produces a different fingerprint by
    construction.  And when the turn's request is reused rather than rebuilt, ``decision_at`` is
    older still.  Reading it back is the only way to reproduce the term.

``advisor_present`` / ``advisor_recommended_option``
    Both are fingerprint terms.  ``prompt_sha256`` is a near-proxy for the first and nothing
    stores the second.  No call site passes an advisor today, so both are constant on every live
    row -- which is precisely why they must be stored *now*, before the first row that has one
    makes every earlier replay silently unverifiable.

All four are additive and nullable-or-defaulted, so a service running ahead of its database
records less rather than failing a turn; ``tce_api.policy_store.replay_columns_available``
probes for them the same way ``policy_columns_available`` probes for 20260909_0040.

``downgrade()`` drops all four.  Unlike ``policy_qualifications``, these hold no human answer and
no adjudication outcome -- they are machine-recorded inputs that the next turn regenerates -- so
Y5 does not bite and the CI ``downgrade -> upgrade`` round trip stays exact.
"""

from __future__ import annotations

from alembic import op

revision = "20260909_0041"
# Parent: P4's decision_policy.
# NOTE: no trailing comment on the assignment below - tests/integration/test_exit_gate_lite.py
# parses this file statically with a regex that captures to end of line.
down_revision = "20260909_0040"
branch_labels = None
depends_on = None


# The Lite twin in `tce_lite_api/db.py` uses these same names in this same order.  Any change
# here is a change there; the parity test asserts the two lists agree.
_REPLAY_COLUMNS = (
    "offered_evidence_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb",
    "decision_at TIMESTAMPTZ NULL",
    "advisor_present BOOLEAN NOT NULL DEFAULT false",
    "advisor_recommended_option TEXT NULL",
)


def upgrade() -> None:
    for ddl in _REPLAY_COLUMNS:
        op.execute(f"ALTER TABLE behavior_shadow_predictions ADD COLUMN IF NOT EXISTS {ddl};")


def downgrade() -> None:
    for ddl in reversed(_REPLAY_COLUMNS):
        column = ddl.split(" ", 1)[0]
        op.execute(f"ALTER TABLE behavior_shadow_predictions DROP COLUMN IF EXISTS {column};")
