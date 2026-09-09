"""retrieval_eval_runs: the Full twin of a table only Lite ever had

Lite has created ``retrieval_eval_runs`` in its SQLite DDL guard since the retrieval-eval work landed
(services/tce_lite_api/tce_lite_api/db.py), but no alembic revision ever created it for Postgres. Two
things followed from that, both silent:

* ``_search_scale_trigger_met`` (services/tce_api/tce_api/search.py) probes this table on the request
  session. On Postgres it raised UndefinedTable on *every* request, and the bare ``except`` around it
  called ``db.rollback()`` on a session it did not own -- discarding whatever the turn had already
  written. The probe is now savepointed, but the table should exist on both backends regardless.
* ``POST /v1/retrieval/eval/run`` cannot persist a run on Full, which is why
  ``tests/integration/test_retrieval_eval_api.py::test_retrieval_eval_run_and_status_live`` has been
  failing for as long as anyone has looked at it.

Columns mirror the Lite DDL exactly so the two backends agree. Additive and IF NOT EXISTS, so a re-run
is a no-op.
"""

from __future__ import annotations

from alembic import op

revision = "20260909_0038"
down_revision = "20260909_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS retrieval_eval_runs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            style_alignment DOUBLE PRECISION NOT NULL,
            constraint_compliance DOUBLE PRECISION NOT NULL,
            decision_traceability DOUBLE PRECISION NOT NULL,
            followup_reduction DOUBLE PRECISION NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ NOT NULL
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_retrieval_eval_runs_scope_completed
            ON retrieval_eval_runs (workspace_id, user_id, session_id, completed_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_retrieval_eval_runs_scope_completed;")
    op.execute("DROP TABLE IF EXISTS retrieval_eval_runs;")
