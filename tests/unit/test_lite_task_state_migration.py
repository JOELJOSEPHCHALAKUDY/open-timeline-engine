from __future__ import annotations

import sqlite3

from tce_lite_api.db import _ensure_task_state_schema

_LEGACY_TABLES = """
CREATE TABLE autonomy_goals (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate',
    step_index INTEGER,
    parent_goal_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE directive_executions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    directive_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT
);
CREATE TABLE execution_permits (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    objective_hash TEXT,
    scope_digest TEXT,
    policy_revision TEXT,
    expires_at TEXT
);
CREATE TABLE handoff_records (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL
);
INSERT INTO autonomy_goals(id, session_id, workspace_id, user_id, title, created_at, updated_at)
VALUES('g-1', 'sess', 'personal', 'human-1', 'legacy goal', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00');
INSERT INTO directive_executions(id, workspace_id, user_id, session_id, directive_id)
VALUES('d-1', 'personal', 'human-1', 'sess', 'dir-1');
INSERT INTO execution_permits(id, workspace_id, session_id) VALUES('p-1', 'personal', 'sess');
INSERT INTO handoff_records(id, workspace_id, session_id, ts)
VALUES('h-1', 'personal', 'sess', '2026-09-01T00:00:00+00:00');
"""

_DIRECTIVE_COLUMNS_BEFORE = {
    "id",
    "workspace_id",
    "user_id",
    "session_id",
    "directive_id",
    "state",
    "started_at",
}
_PERMIT_COLUMNS_BEFORE = {
    "id",
    "workspace_id",
    "session_id",
    "objective_hash",
    "scope_digest",
    "policy_revision",
    "expires_at",
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA index_list({table})")}


def test_task_state_schema_is_additive_and_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_LEGACY_TABLES)

        _ensure_task_state_schema(conn)
        _ensure_task_state_schema(conn)

        for table in ("task_states", "task_state_events", "planning_jobs", "task_verifications"):
            assert (
                conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                is not None
            )

        assert "scope_json" in _columns(conn, "planning_jobs")
        assert "charter_json" in _columns(conn, "planning_jobs")
        assert "last_cancel_seq" in _columns(conn, "task_states")
        assert "cancelled_seq" in _columns(conn, "task_states")
        assert "objective_set_seq" in _columns(conn, "task_states")
        assert {"contract_revision", "plan_id"} <= _columns(conn, "task_verifications")

        assert {
            "plan_contract_revision",
            "attempt_count",
            "blocked_reason",
            "depends_on_json",
            "mutating",
        } <= _columns(conn, "autonomy_goals")
        assert {
            "task_id",
            "task_state_revision",
            "contract_revision",
            "verification_refs_json",
            "unresolved_effects_json",
        } <= _columns(conn, "handoff_records")

        # S1: neither directive_executions nor execution_permits is altered by this revision.
        assert _columns(conn, "directive_executions") == _DIRECTIVE_COLUMNS_BEFORE
        assert _columns(conn, "execution_permits") == _PERMIT_COLUMNS_BEFORE
        assert "idx_directive_executions_task" not in _indexes(conn, "directive_executions")

        assert "uq_task_states_identity" in _indexes(conn, "task_states")
        assert "idx_task_states_session" in _indexes(conn, "task_states")
        assert "uq_task_state_events_seq" in _indexes(conn, "task_state_events")
        assert "idx_task_state_events_task" in _indexes(conn, "task_state_events")
        assert "idx_task_state_events_kind" in _indexes(conn, "task_state_events")
        assert "uq_planning_jobs_idem" in _indexes(conn, "planning_jobs")
        assert "idx_planning_jobs_dispatch" in _indexes(conn, "planning_jobs")
        assert "idx_planning_jobs_task" in _indexes(conn, "planning_jobs")
        assert "idx_task_verifications_task" in _indexes(conn, "task_verifications")

        # Seed rows survive, and the new goal columns take their declared defaults.
        goal = conn.execute(
            "SELECT title, plan_contract_revision, attempt_count, depends_on_json, mutating FROM autonomy_goals WHERE id = 'g-1'"
        ).fetchone()
        assert dict(goal) == {
            "title": "legacy goal",
            "plan_contract_revision": None,
            "attempt_count": 0,
            "depends_on_json": "[]",
            "mutating": 0,
        }
        assert conn.execute("SELECT COUNT(*) FROM directive_executions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM execution_permits").fetchone()[0] == 1
        handoff = conn.execute(
            "SELECT task_id, verification_refs_json, unresolved_effects_json FROM handoff_records WHERE id = 'h-1'"
        ).fetchone()
        assert dict(handoff) == {"task_id": None, "verification_refs_json": "[]", "unresolved_effects_json": "[]"}

        # The task identity and the event sequence are both unique.
        now = "2026-09-09T00:00:00+00:00"
        conn.execute(
            "INSERT INTO task_states(id, workspace_id, owner_id, task_id, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("ts-1", "personal", "human-1", "task-1", now, now),
        )
        try:
            conn.execute(
                "INSERT INTO task_states(id, workspace_id, owner_id, task_id, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                ("ts-2", "personal", "human-1", "task-1", now, now),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("duplicate (workspace_id, owner_id, task_id) must be rejected")

        conn.execute(
            "INSERT INTO task_state_events(id, task_state_id, workspace_id, owner_id, task_id, seq, kind, occurred_at) VALUES(?,?,?,?,?,?,?,?)",
            ("e-1", "ts-1", "personal", "human-1", "task-1", 1, "objective_set", now),
        )
        try:
            conn.execute(
                "INSERT INTO task_state_events(id, task_state_id, workspace_id, owner_id, task_id, seq, kind, occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                ("e-2", "ts-1", "personal", "human-1", "task-1", 1, "plan_approved", now),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("duplicate (task_state_id, seq) must be rejected")
    finally:
        conn.close()
