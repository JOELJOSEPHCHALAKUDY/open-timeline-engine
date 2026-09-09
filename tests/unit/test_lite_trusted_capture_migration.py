from __future__ import annotations

import sqlite3

from tce_lite_api.db import _ensure_trusted_capture_schema

_LEGACY_TABLES = """
CREATE TABLE behavior_shadow_predictions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    subject_user_id TEXT NOT NULL,
    observation_id TEXT,
    predicted_choice TEXT,
    actual_choice TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.0,
    abstained INTEGER NOT NULL DEFAULT 1,
    correct INTEGER,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    query_json TEXT NOT NULL DEFAULT '{}',
    citations_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    schema_version TEXT NOT NULL DEFAULT 'v1'
);
CREATE TABLE decision_observations (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    subject_user_id TEXT NOT NULL DEFAULT '',
    situation_type TEXT NOT NULL,
    situation_summary TEXT NOT NULL,
    user_response TEXT NOT NULL
);
INSERT INTO behavior_shadow_predictions(id, workspace_id, subject_user_id, actual_choice, created_at)
VALUES('s-1', 'personal', 'human-1', 'confirm', '2026-09-01T00:00:00+00:00');
INSERT INTO decision_observations(id, ts, consumer_id, workspace_id, situation_type, situation_summary, user_response)
VALUES('o-1', '2026-09-01T00:00:00+00:00', 'codex', 'personal', 'approval_requested', 'legacy', 'confirm');
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA index_list({table})")}


def test_trusted_capture_schema_is_additive_and_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_LEGACY_TABLES)

        _ensure_trusted_capture_schema(conn)
        _ensure_trusted_capture_schema(conn)

        for table in ("trusted_input_receipts", "decision_opportunities", "decision_candidates", "human_resolutions"):
            assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
        assert {
            "opportunity_id",
            "session_id",
            "turn",
            "decision_family",
            "prediction_stage",
            "frozen_at",
            "evidence_cutoff_at",
            "evidence_revision",
            "prediction_shown_at",
            "advice_visible",
            "resolution_state",
            "resolved_at",
            "resolution_source",
            "human_source_ref",
            "resolution_source_event_id",
            "corrections_json",
        } <= _columns(conn, "behavior_shadow_predictions")
        assert {"opportunity_id", "origin_kind", "capture_receipt_id", "extraction_version"} <= _columns(conn, "decision_observations")

        # Legacy rows keep their meaning: an existing shadow row is a resolved, retrospective prediction.
        row = conn.execute(
            "SELECT prediction_stage, resolution_state, advice_visible, corrections_json, opportunity_id FROM behavior_shadow_predictions WHERE id = 's-1'"
        ).fetchone()
        assert dict(row) == {
            "prediction_stage": "retrospective",
            "resolution_state": "resolved",
            "advice_visible": 0,
            "corrections_json": "[]",
            "opportunity_id": None,
        }
        obs = conn.execute("SELECT opportunity_id, origin_kind, capture_receipt_id FROM decision_observations WHERE id = 'o-1'").fetchone()
        assert dict(obs) == {"opportunity_id": None, "origin_kind": None, "capture_receipt_id": None}

        assert "uq_trusted_input_receipts_delivery" in _indexes(conn, "trusted_input_receipts")
        assert "idx_trusted_input_receipts_extraction" in _indexes(conn, "trusted_input_receipts")
        assert "idx_decision_opportunities_open" in _indexes(conn, "decision_opportunities")
        assert "uq_decision_candidates_span" in _indexes(conn, "decision_candidates")
        assert "idx_human_resolutions_opportunity" in _indexes(conn, "human_resolutions")
        assert "idx_behavior_shadow_open" in _indexes(conn, "behavior_shadow_predictions")
        assert "idx_decision_obs_opportunity" in _indexes(conn, "decision_observations")

        # Delivery key is unique per (workspace, owner).
        conn.execute(
            """
            INSERT INTO trusted_input_receipts(id, workspace_id, owner_id, subject_user_id, host_session_id, delivery_key,
                content_sha256, origin_kind, capture_principal, observed_at, ingested_at)
            VALUES('r-1', 'personal', 'human-1', 'human-1', 'sess', 'k', 'h', 'human_input', 'host:x', 'now', 'now')
            """
        )
        try:
            conn.execute(
                """
                INSERT INTO trusted_input_receipts(id, workspace_id, owner_id, subject_user_id, host_session_id, delivery_key,
                    content_sha256, origin_kind, capture_principal, observed_at, ingested_at)
                VALUES('r-2', 'personal', 'human-1', 'human-1', 'sess', 'k', 'h', 'human_input', 'host:x', 'now', 'now')
                """
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("duplicate delivery_key must be rejected")
    finally:
        conn.close()
