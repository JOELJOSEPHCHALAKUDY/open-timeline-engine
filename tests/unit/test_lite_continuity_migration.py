from __future__ import annotations

import sqlite3

from tce_lite_api.db import _ensure_continuity_v04_schema


def test_continuity_v04_schema_evolves_in_place_and_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """
            CREATE TABLE continuity_resume_attempts (
                id TEXT PRIMARY KEY,
                packet_id TEXT NOT NULL UNIQUE,
                workspace_id TEXT NOT NULL,
                requesting_owner_id TEXT NOT NULL,
                target_owner_id TEXT NOT NULL,
                selected_record_id TEXT NOT NULL,
                query_text TEXT NOT NULL,
                top_file TEXT,
                requested_at TEXT NOT NULL,
                returned_at TEXT NOT NULL,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                time_since_handoff_ms INTEGER NOT NULL DEFAULT 0,
                opened_file TEXT,
                correct_file INTEGER,
                correction_required INTEGER,
                correction_reason TEXT NOT NULL DEFAULT '',
                feedback_at TEXT
            );
            INSERT INTO continuity_resume_attempts(
                id, packet_id, workspace_id, requesting_owner_id, target_owner_id,
                selected_record_id, query_text, requested_at, returned_at
            ) VALUES(
                'attempt-1', 'packet-1', 'shared', 'claude-executor', 'codex-executor',
                'record-1', 'continue work', '2026-07-23T10:00:00+00:00',
                '2026-07-23T10:00:00.010000+00:00'
            );
            """
        )

        _ensure_continuity_v04_schema(conn)
        _ensure_continuity_v04_schema(conn)

        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(continuity_resume_attempts)")
        }
        assert {
            "session_id",
            "recommended_files_json",
            "opened_file_rank",
            "first_file_opened_at",
            "productive_at",
            "completed_at",
            "correct_anchor",
            "archaeology_tool_calls",
            "archaeology_tokens",
            "outcome_status",
            "progress_source",
        } <= columns
        row = conn.execute(
            """
            SELECT packet_id, session_id, recommended_files_json, progress_source
            FROM continuity_resume_attempts WHERE id = 'attempt-1'
            """
        ).fetchone()
        assert dict(row) == {
            "packet_id": "packet-1",
            "session_id": "default",
            "recommended_files_json": "[]",
            "progress_source": "resume_packet",
        }
        indexes = {
            str(row["name"])
            for row in conn.execute("PRAGMA index_list(continuity_resume_attempts)")
        }
        assert "idx_continuity_resume_session" in indexes
    finally:
        conn.close()
