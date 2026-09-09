from __future__ import annotations

import sqlite3

from tce_lite_api.db import _ensure_continuity_v05_schema, _ensure_directive_lease_schema

_LEGACY_DIRECTIVE_TABLES = """
CREATE TABLE directive_executions (
    directive_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    goal_id TEXT,
    objective_hash TEXT,
    action_kind TEXT NOT NULL DEFAULT 'execute',
    attempt INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'pending',
    requires_permit INTEGER NOT NULL DEFAULT 0,
    permit_id TEXT,
    claimed_by TEXT,
    started_at TEXT,
    finished_at TEXT,
    expires_at TEXT,
    failure_class TEXT,
    failure_reason TEXT,
    retry_strategy TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE execution_permits (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    action_kind TEXT NOT NULL,
    target_paths TEXT NOT NULL DEFAULT '[]',
    command_preview TEXT,
    estimated_change_size INTEGER NOT NULL DEFAULT 0,
    decision TEXT NOT NULL DEFAULT 'allow',
    reason TEXT NOT NULL DEFAULT '',
    confirmed_by TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
INSERT INTO directive_executions(
    directive_id, session_id, workspace_id, user_id, action_kind, attempt, state, requires_permit,
    claimed_by, meta, created_at, updated_at
) VALUES(
    'd-1', 'legacy', 'personal', 'user', 'edit', 1, 'in_progress', 1,
    'legacy-claimer', '{}', '2026-07-20T00:00:00+00:00', '2026-07-20T00:00:00+00:00'
);
INSERT INTO execution_permits(id, session_id, workspace_id, action_kind, decision, created_at)
VALUES('p-1', 'legacy', 'personal', 'edit', 'allow', '2026-07-20T00:00:00+00:00');
"""

_LEGACY_CONTINUITY_TABLES = """
CREATE TABLE handoff_records (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    directive_id TEXT,
    ts TEXT NOT NULL,
    title TEXT NOT NULL,
    decision TEXT NOT NULL,
    next_step TEXT NOT NULL,
    status TEXT NOT NULL,
    files_json TEXT NOT NULL DEFAULT '[]',
    anchors_json TEXT NOT NULL DEFAULT '[]',
    git_json TEXT NOT NULL DEFAULT '{}',
    change_summary_json TEXT NOT NULL DEFAULT '{}',
    objective_text TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'native',
    event_id TEXT,
    schema_version TEXT NOT NULL DEFAULT 'v1',
    redaction_applied INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL
);
CREATE TABLE handoff_outbox (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    behavior_subject_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    directive_id TEXT,
    completion_key TEXT NOT NULL,
    terminal_state TEXT NOT NULL,
    milestone_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'native',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    event_id TEXT NOT NULL,
    handoff_record_id TEXT NOT NULL,
    redaction_applied INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(workspace_id, owner_id, completion_key)
);
CREATE TABLE continuity_resume_attempts (
    id TEXT PRIMARY KEY,
    packet_id TEXT NOT NULL UNIQUE,
    workspace_id TEXT NOT NULL,
    requesting_owner_id TEXT NOT NULL,
    target_owner_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT 'default',
    selected_record_id TEXT NOT NULL,
    query_text TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    returned_at TEXT NOT NULL
);
INSERT INTO handoff_records(id, workspace_id, owner_id, session_id, ts, title, decision, next_step, status, expires_at)
VALUES('h-1', 'shared', 'codex-executor', 'codex-a', '2026-07-23T10:00:00+00:00', 'legacy', 'legacy', 'legacy', 'succeeded', '2099-01-01T00:00:00+00:00');
INSERT INTO handoff_outbox(id, workspace_id, owner_id, behavior_subject_id, session_id, completion_key, terminal_state, next_attempt_at, event_id, handoff_record_id, created_at, updated_at)
VALUES('o-1', 'shared', 'codex-executor', 'human', 'codex-a', 'k-1', 'succeeded', '2026-07-23T10:00:00+00:00', 'e-1', 'h-1', '2026-07-23T10:00:00+00:00', '2026-07-23T10:00:00+00:00');
INSERT INTO continuity_resume_attempts(id, packet_id, workspace_id, requesting_owner_id, target_owner_id, selected_record_id, query_text, requested_at, returned_at)
VALUES('a-1', 'packet-1', 'shared', 'claude-executor', 'codex-executor', 'h-1', 'continue', '2026-07-23T10:00:00+00:00', '2026-07-23T10:00:00.010000+00:00');
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA index_list({table})")}


def test_directive_lease_schema_evolves_in_place_and_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_LEGACY_DIRECTIVE_TABLES)

        _ensure_directive_lease_schema(conn)
        _ensure_directive_lease_schema(conn)

        assert {
            "lease_generation",
            "claimed_executor",
            "lease_expires_at",
            "verification_state",
            "report_idempotency_key",
            "report_payload_hash",
            "cancelled_at",
            "cancel_reason",
        } <= _columns(conn, "directive_executions")
        assert {
            "user_id",
            "requested_by",
            "directive_id",
            "attempt",
            "objective_hash",
            "policy_revision",
            "scope_digest",
            "resolved_by",
        } <= _columns(conn, "execution_permits")

        row = conn.execute(
            "SELECT lease_generation, claimed_executor, verification_state, report_idempotency_key, cancelled_at, claimed_by FROM directive_executions WHERE directive_id = 'd-1'"
        ).fetchone()
        assert dict(row) == {
            "lease_generation": 0,
            "claimed_executor": None,  # legacy claims are labels only; never promoted to an auth-bound executor
            "verification_state": "unverified",
            "report_idempotency_key": None,
            "cancelled_at": None,
            "claimed_by": "legacy-claimer",
        }
        permit = conn.execute("SELECT user_id, directive_id, objective_hash, scope_digest FROM execution_permits WHERE id = 'p-1'").fetchone()
        assert dict(permit) == {"user_id": None, "directive_id": None, "objective_hash": None, "scope_digest": None}
        assert "idx_directive_executions_report_idem" in _indexes(conn, "directive_executions")
    finally:
        conn.close()


def test_continuity_v05_schema_evolves_in_place_and_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_LEGACY_CONTINUITY_TABLES)

        _ensure_continuity_v05_schema(conn)
        _ensure_continuity_v05_schema(conn)

        assert {"project_id", "git_remote", "executor_id"} <= _columns(conn, "handoff_records")
        assert {"executor_id", "payload_hash"} <= _columns(conn, "handoff_outbox")
        assert "source_session_id" in _columns(conn, "continuity_resume_attempts")

        record = conn.execute("SELECT project_id, git_remote, executor_id FROM handoff_records WHERE id = 'h-1'").fetchone()
        assert dict(record) == {"project_id": None, "git_remote": None, "executor_id": None}
        outbox = conn.execute("SELECT executor_id, payload_hash FROM handoff_outbox WHERE id = 'o-1'").fetchone()
        assert dict(outbox) == {"executor_id": None, "payload_hash": None}
        attempt = conn.execute("SELECT session_id, source_session_id FROM continuity_resume_attempts WHERE id = 'a-1'").fetchone()
        assert dict(attempt) == {"session_id": "default", "source_session_id": None}
        assert "idx_handoff_records_workspace_project_ts" in _indexes(conn, "handoff_records")
    finally:
        conn.close()
