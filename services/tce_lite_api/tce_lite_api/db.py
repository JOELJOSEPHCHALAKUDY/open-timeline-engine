from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

from .config import get_settings


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    db_path = Path(settings.lite_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(row[1]) == column for row in rows)


def _ensure_behavior_fidelity_schema(conn: sqlite3.Connection) -> None:
    columns = {
        "subject_user_id": "TEXT NOT NULL DEFAULT ''",
        "objective_text": "TEXT NOT NULL DEFAULT ''",
        "constraints_json": "TEXT NOT NULL DEFAULT '{}'",
        "available_choices_json": "TEXT NOT NULL DEFAULT '[]'",
        "selected_choice": "TEXT NOT NULL DEFAULT ''",
        "action_taken": "TEXT NOT NULL DEFAULT ''",
        "correction_text": "TEXT NOT NULL DEFAULT ''",
        "memory_class": "TEXT NOT NULL DEFAULT 'decision'",
        "evidence_source": "TEXT NOT NULL DEFAULT 'inferred'",
        "lifecycle_status": "TEXT NOT NULL DEFAULT 'active'",
        "valid_from": "TEXT NOT NULL DEFAULT ''",
        "valid_until": "TEXT",
        "contradicts_ids_json": "TEXT NOT NULL DEFAULT '[]'",
        "confirmed_at": "TEXT",
        "behavior_schema_version": "TEXT NOT NULL DEFAULT 'v1'",
        "redaction_applied": "INTEGER NOT NULL DEFAULT 0",
        "learning_eligible": "INTEGER NOT NULL DEFAULT 0",
        "storage_score": "REAL NOT NULL DEFAULT 0.0",
        "storage_decision": "TEXT NOT NULL DEFAULT 'audit_only'",
    }
    for name, ddl in columns.items():
        if not _column_exists(conn, "decision_observations", name):
            conn.execute(f"ALTER TABLE decision_observations ADD COLUMN {name} {ddl}")
    conn.execute(
        """
        UPDATE decision_observations
        SET subject_user_id = CASE WHEN subject_user_id = '' THEN consumer_id ELSE subject_user_id END,
            objective_text = CASE WHEN objective_text = '' THEN situation_summary ELSE objective_text END,
            selected_choice = CASE WHEN selected_choice = '' THEN user_response ELSE selected_choice END,
            valid_from = CASE WHEN valid_from = '' THEN ts ELSE valid_from END,
            lifecycle_status = CASE WHEN superseded_by IS NULL THEN 'active' ELSE 'superseded' END
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS behavior_fidelity_runs (
            id TEXT PRIMARY KEY,
            consumer_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            gate_json TEXT NOT NULL DEFAULT '{}',
            case_results_json TEXT NOT NULL DEFAULT '[]',
            evidence_count INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    if not _column_exists(conn, "behavior_fidelity_runs", "subject_user_id"):
        conn.execute("ALTER TABLE behavior_fidelity_runs ADD COLUMN subject_user_id TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """
        UPDATE behavior_fidelity_runs
        SET subject_user_id = CASE WHEN subject_user_id = '' THEN consumer_id ELSE subject_user_id END
        """
    )
    conn.execute("DROP INDEX IF EXISTS idx_decision_observations_behavior_active")
    conn.execute("DROP INDEX IF EXISTS idx_behavior_fidelity_runs_scope_created")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decision_observations_behavior_active
            ON decision_observations (
                workspace_id, subject_user_id, lifecycle_status, learning_eligible, ts DESC
            )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decision_observations_validity
            ON decision_observations (workspace_id, valid_from, valid_until, ts DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_behavior_fidelity_runs_scope_created
            ON behavior_fidelity_runs (workspace_id, subject_user_id, created_at DESC)
        """
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS capability_grants (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            directive_id TEXT,
            permit_id TEXT,
            capability TEXT NOT NULL,
            action TEXT NOT NULL,
            resource TEXT NOT NULL,
            action_digest TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            risk_tier TEXT NOT NULL,
            mutating INTEGER NOT NULL DEFAULT 0,
            redaction_applied INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_capability_grants_scope_status
            ON capability_grants (workspace_id, owner_id, status, expires_at DESC);

        CREATE TABLE IF NOT EXISTS behavior_process_models (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            process_signature TEXT NOT NULL,
            name TEXT NOT NULL,
            steps_json TEXT NOT NULL DEFAULT '[]',
            transitions_json TEXT NOT NULL DEFAULT '[]',
            support INTEGER NOT NULL DEFAULT 0,
            success_rate REAL NOT NULL DEFAULT 0.0,
            reliability REAL NOT NULL DEFAULT 0.0,
            source_sessions_json TEXT NOT NULL DEFAULT '[]',
            evidence_ids_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'candidate',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1',
            UNIQUE(workspace_id, subject_user_id, process_signature)
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_process_models_scope_status
            ON behavior_process_models (workspace_id, subject_user_id, status, reliability DESC);

        CREATE TABLE IF NOT EXISTS behavior_shadow_predictions (
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
        CREATE INDEX IF NOT EXISTS idx_behavior_shadow_scope_created
            ON behavior_shadow_predictions (workspace_id, subject_user_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS behavior_memory_reviews (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            title TEXT NOT NULL,
            rationale TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            proposed_action TEXT NOT NULL DEFAULT 'promote',
            source TEXT NOT NULL,
            score REAL NOT NULL DEFAULT 0.0,
            reviewer_id TEXT,
            review_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            schema_version TEXT NOT NULL DEFAULT 'v1',
            UNIQUE(workspace_id, subject_user_id, target_type, target_id)
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_memory_reviews_scope_status
            ON behavior_memory_reviews (workspace_id, subject_user_id, status, created_at DESC);

        CREATE TABLE IF NOT EXISTS behavior_counterfactuals (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            observation_id TEXT,
            session_id TEXT NOT NULL,
            directive_id TEXT,
            decision TEXT NOT NULL,
            alternative TEXT NOT NULL,
            expected_outcome TEXT NOT NULL,
            assumptions_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL NOT NULL DEFAULT 0.5,
            status TEXT NOT NULL DEFAULT 'open',
            assessment TEXT,
            observed_outcome TEXT NOT NULL DEFAULT '',
            lesson TEXT NOT NULL DEFAULT '',
            regret_score REAL,
            redaction_applied INTEGER NOT NULL DEFAULT 0,
            review_at TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_counterfactuals_scope_status
            ON behavior_counterfactuals (workspace_id, subject_user_id, status, created_at DESC);

        CREATE TABLE IF NOT EXISTS behavior_projection_pilot_assignments (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            trial_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            variant TEXT NOT NULL,
            situation_type TEXT NOT NULL,
            situation_summary TEXT NOT NULL,
            objective_text TEXT NOT NULL,
            request_json TEXT NOT NULL DEFAULT '{}',
            context_json TEXT NOT NULL DEFAULT '{}',
            context_sha256 TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            citations_json TEXT NOT NULL DEFAULT '[]',
            injected_tokens INTEGER NOT NULL DEFAULT 0,
            retrieval_latency_ms INTEGER NOT NULL DEFAULT 0,
            redaction_applied INTEGER NOT NULL DEFAULT 0,
            assigned_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1',
            UNIQUE(workspace_id, subject_user_id, trial_key)
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_projection_pilot_scope_assigned
            ON behavior_projection_pilot_assignments (
                workspace_id, subject_user_id, assigned_at DESC
            );
        CREATE INDEX IF NOT EXISTS idx_behavior_projection_pilot_variant_assigned
            ON behavior_projection_pilot_assignments (
                workspace_id, subject_user_id, variant, assigned_at DESC
            );

        CREATE TABLE IF NOT EXISTS behavior_projection_pilot_outcomes (
            id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL UNIQUE,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            reporter_id TEXT NOT NULL,
            outcome_digest TEXT NOT NULL,
            agent_choice TEXT,
            top3_choices_json TEXT NOT NULL DEFAULT '[]',
            actual_choice TEXT NOT NULL,
            agent_confidence REAL NOT NULL DEFAULT 0.0,
            abstained INTEGER NOT NULL DEFAULT 0,
            action_similarity REAL NOT NULL DEFAULT 0.0,
            workflow_similarity REAL NOT NULL DEFAULT 0.0,
            correction_required INTEGER NOT NULL DEFAULT 0,
            outcome_regret INTEGER NOT NULL DEFAULT 0,
            irrelevant_personalization INTEGER NOT NULL DEFAULT 0,
            malicious_memory_activated INTEGER NOT NULL DEFAULT 0,
            stale_evidence_used INTEGER NOT NULL DEFAULT 0,
            used_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
            notes TEXT NOT NULL DEFAULT '',
            redaction_applied INTEGER NOT NULL DEFAULT 0,
            reported_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1',
            FOREIGN KEY(assignment_id) REFERENCES behavior_projection_pilot_assignments(id)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_behavior_projection_pilot_outcome_scope_reported
            ON behavior_projection_pilot_outcomes (
                workspace_id, subject_user_id, reported_at DESC
            );
        """
    )
    if not _column_exists(conn, "capability_grants", "completion_required"):
        conn.execute("ALTER TABLE capability_grants ADD COLUMN completion_required INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(conn, "capability_grants", "completion_outbox_id"):
        conn.execute("ALTER TABLE capability_grants ADD COLUMN completion_outbox_id TEXT")
    if not _column_exists(conn, "capability_grants", "completion_recorded_at"):
        conn.execute("ALTER TABLE capability_grants ADD COLUMN completion_recorded_at TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_capability_grants_completion_obligation
        ON capability_grants (
            workspace_id, owner_id, session_id, completion_required, completion_recorded_at
        )
        """
    )


def _ensure_continuity_v04_schema(conn: sqlite3.Connection) -> None:
    columns = {
        "session_id": "TEXT NOT NULL DEFAULT 'default'",
        "recommended_files_json": "TEXT NOT NULL DEFAULT '[]'",
        "opened_file_rank": "INTEGER",
        "first_file_opened_at": "TEXT",
        "productive_at": "TEXT",
        "completed_at": "TEXT",
        "correct_anchor": "INTEGER",
        "archaeology_tool_calls": "INTEGER",
        "archaeology_tokens": "INTEGER",
        "outcome_status": "TEXT",
        "progress_source": "TEXT NOT NULL DEFAULT 'resume_packet'",
    }
    for name, ddl in columns.items():
        if not _column_exists(conn, "continuity_resume_attempts", name):
            conn.execute(f"ALTER TABLE continuity_resume_attempts ADD COLUMN {name} {ddl}")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_continuity_resume_session
            ON continuity_resume_attempts (
                workspace_id, requesting_owner_id, session_id, requested_at DESC
            )
        """
    )


def _ensure_takeover_v3_schema(conn: sqlite3.Connection) -> None:
    if not _column_exists(conn, "takeover_sessions", "objective_hash"):
        conn.execute("ALTER TABLE takeover_sessions ADD COLUMN objective_hash TEXT")
    if not _column_exists(conn, "takeover_sessions", "working_set_json"):
        conn.execute("ALTER TABLE takeover_sessions ADD COLUMN working_set_json TEXT NOT NULL DEFAULT '{}'")
    if not _column_exists(conn, "takeover_sessions", "last_deliberation_at"):
        conn.execute("ALTER TABLE takeover_sessions ADD COLUMN last_deliberation_at TEXT")
    if not _column_exists(conn, "takeover_sessions", "recent_outcomes_json"):
        conn.execute("ALTER TABLE takeover_sessions ADD COLUMN recent_outcomes_json TEXT NOT NULL DEFAULT '[]'")
    if not _column_exists(conn, "takeover_sessions", "autonomy_score"):
        conn.execute("ALTER TABLE takeover_sessions ADD COLUMN autonomy_score REAL NOT NULL DEFAULT 0.5")
    if not _column_exists(conn, "takeover_sessions", "autonomy_policy_profile"):
        conn.execute("ALTER TABLE takeover_sessions ADD COLUMN autonomy_policy_profile TEXT NOT NULL DEFAULT 'human_consultative'")
    if not _column_exists(conn, "takeover_sessions", "active_goal_id"):
        conn.execute("ALTER TABLE takeover_sessions ADD COLUMN active_goal_id TEXT")
    if not _column_exists(conn, "takeover_sessions", "goal_queue_size"):
        conn.execute("ALTER TABLE takeover_sessions ADD COLUMN goal_queue_size INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(conn, "takeover_sessions", "last_discovery_at"):
        conn.execute("ALTER TABLE takeover_sessions ADD COLUMN last_discovery_at TEXT")
    if not _column_exists(conn, "takeover_sessions", "continuity_violation_count"):
        conn.execute("ALTER TABLE takeover_sessions ADD COLUMN continuity_violation_count INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(conn, "takeover_sessions", "enforcement_mode"):
        conn.execute("ALTER TABLE takeover_sessions ADD COLUMN enforcement_mode TEXT NOT NULL DEFAULT 'strict_takeover'")
    if not _column_exists(conn, "takeover_sessions", "last_tick_at"):
        conn.execute("ALTER TABLE takeover_sessions ADD COLUMN last_tick_at TEXT")
    if not _column_exists(conn, "takeover_sessions", "pending_directive_count"):
        conn.execute("ALTER TABLE takeover_sessions ADD COLUMN pending_directive_count INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(conn, "takeover_sessions", "retry_backlog_count"):
        conn.execute("ALTER TABLE takeover_sessions ADD COLUMN retry_backlog_count INTEGER NOT NULL DEFAULT 0")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS takeover_action_log (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            turn INTEGER NOT NULL DEFAULT 0,
            objective_hash TEXT,
            action_kind TEXT NOT NULL,
            result TEXT NOT NULL,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            meta TEXT NOT NULL DEFAULT '{}',
            ts TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_takeover_action_log_scope
            ON takeover_action_log (workspace_id, user_id, ts DESC);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_takeover_action_log_session
            ON takeover_action_log (session_id, ts DESC);
        """
    )
    if not _column_exists(conn, "decision_observations", "embedding"):
        conn.execute("ALTER TABLE decision_observations ADD COLUMN embedding TEXT")
    if not _column_exists(conn, "decision_observations", "superseded_by"):
        conn.execute("ALTER TABLE decision_observations ADD COLUMN superseded_by TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_observations_superseded
            ON decision_observations (workspace_id, superseded_by, ts DESC)
        """
    )
    if not _column_exists(conn, "autonomy_goals", "goal_kind"):
        conn.execute("ALTER TABLE autonomy_goals ADD COLUMN goal_kind TEXT NOT NULL DEFAULT 'normal'")
    if not _column_exists(conn, "autonomy_goals", "affective_scores"):
        conn.execute("ALTER TABLE autonomy_goals ADD COLUMN affective_scores TEXT NOT NULL DEFAULT '{}'")
    if not _column_exists(conn, "autonomy_goals", "selection_score"):
        conn.execute("ALTER TABLE autonomy_goals ADD COLUMN selection_score REAL NOT NULL DEFAULT 0.0")
    if not _column_exists(conn, "autonomy_goals", "goal_embedding"):
        conn.execute("ALTER TABLE autonomy_goals ADD COLUMN goal_embedding TEXT")
    if not _column_exists(conn, "autonomy_goals", "goal_signature"):
        conn.execute("ALTER TABLE autonomy_goals ADD COLUMN goal_signature TEXT")
    if not _column_exists(conn, "autonomy_goals", "parent_goal_id"):
        conn.execute("ALTER TABLE autonomy_goals ADD COLUMN parent_goal_id TEXT")
    if not _column_exists(conn, "autonomy_goals", "cache_hit"):
        conn.execute("ALTER TABLE autonomy_goals ADD COLUMN cache_hit INTEGER NOT NULL DEFAULT 0")
    if not _column_exists(conn, "autonomy_goals", "cache_source"):
        conn.execute("ALTER TABLE autonomy_goals ADD COLUMN cache_source TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_goals (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'open_discovery',
            priority_score REAL NOT NULL DEFAULT 0.0,
            risk_tier TEXT NOT NULL DEFAULT 'medium',
            confidence REAL NOT NULL DEFAULT 0.0,
            reasoning TEXT NOT NULL DEFAULT '',
            evidence_event_ids TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'candidate',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_autonomy_goals_session_status_priority
            ON autonomy_goals (session_id, status, priority_score DESC, updated_at DESC);
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_permits (
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
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_execution_permits_session_decision_expires
            ON execution_permits (session_id, decision, expires_at DESC);
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_goal_cache (
            cache_key TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            cache_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_accessed_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomy_notices (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            goal_id TEXT,
            title TEXT NOT NULL,
            reason TEXT NOT NULL,
            priority REAL NOT NULL DEFAULT 0.0,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            acknowledged_at TEXT
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS directive_executions (
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
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_human_score_snapshots (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            score INTEGER NOT NULL,
            band TEXT NOT NULL,
            subscores_json TEXT NOT NULL DEFAULT '{}',
            inputs_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_autonomy_goal_cache_scope_expires
            ON autonomy_goal_cache (session_id, workspace_id, user_id, expires_at DESC);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_autonomy_notices_session_priority_created
            ON autonomy_notices (session_id, acknowledged_at, priority DESC, created_at DESC);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_directive_executions_session_state_started
            ON directive_executions (session_id, state, started_at DESC);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dashboard_human_score_scope_created
            ON dashboard_human_score_snapshots (workspace_id, user_id, session_id, created_at DESC);
        """
    )
    if not _column_exists(conn, "events", "source_id"):
        conn.execute("ALTER TABLE events ADD COLUMN source_id TEXT")
    if not _column_exists(conn, "events", "source_seq"):
        conn.execute("ALTER TABLE events ADD COLUMN source_seq INTEGER")
    if not _column_exists(conn, "events", "vector_clock"):
        conn.execute("ALTER TABLE events ADD COLUMN vector_clock TEXT NOT NULL DEFAULT '{}'")
    if not _column_exists(conn, "events", "idempotency_key"):
        conn.execute("ALTER TABLE events ADD COLUMN idempotency_key TEXT")
    if not _column_exists(conn, "events", "authority_level"):
        conn.execute("ALTER TABLE events ADD COLUMN authority_level TEXT NOT NULL DEFAULT 'incidental'")

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_source_seq_scope
            ON events (source_id, source_seq, json_extract(context, '$._tce_workspace'))
            WHERE source_id IS NOT NULL AND source_seq IS NOT NULL;
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency_scope
            ON events (idempotency_key, json_extract(context, '$._tce_workspace'), json_extract(context, '$._tce_owner'))
            WHERE idempotency_key IS NOT NULL;
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS episodes (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT 'default',
            goal TEXT NOT NULL,
            context TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'open',
            authority_score REAL NOT NULL DEFAULT 0.0,
            stability_score REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS episode_decisions (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            decision TEXT NOT NULL,
            why TEXT NOT NULL DEFAULT '',
            alternatives_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS episode_lessons (
            episode_id TEXT PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
            do_more_json TEXT NOT NULL DEFAULT '[]',
            do_less_json TEXT NOT NULL DEFAULT '[]',
            avoid_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS episode_event_links (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_episodes_scope_updated
            ON episodes (workspace_id, user_id, session_id, updated_at DESC);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_episode_event_links_scope
            ON episode_event_links (episode_id, event_id);
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_rules (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT '{}',
            rule_type TEXT NOT NULL,
            statement TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 2,
            active INTEGER NOT NULL DEFAULT 1,
            evergreen INTEGER NOT NULL DEFAULT 1,
            expires_at TEXT,
            source_episode_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    if not _column_exists(conn, "memory_rules", "evergreen"):
        conn.execute("ALTER TABLE memory_rules ADD COLUMN evergreen INTEGER NOT NULL DEFAULT 1")
    if not _column_exists(conn, "memory_rules", "expires_at"):
        conn.execute("ALTER TABLE memory_rules ADD COLUMN expires_at TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_tombstones (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_ids TEXT NOT NULL DEFAULT '[]',
            reason TEXT NOT NULL DEFAULT 'user_requested',
            requested_by TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            meta TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_rules_scope_priority
            ON memory_rules (workspace_id, user_id, active, priority, updated_at DESC);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_rules_expiry
            ON memory_rules (workspace_id, user_id, active, evergreen, expires_at, updated_at DESC);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_tombstones_scope_deleted
            ON memory_tombstones (workspace_id, user_id, deleted_at DESC);
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_aliases (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            canonical_entity_id TEXT NOT NULL,
            alias_key TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS event_fingerprints (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            fingerprint_hash TEXT NOT NULL,
            fingerprint_payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_entity_aliases_scope_key
            ON entity_aliases (workspace_id, owner_id, alias_key);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_fingerprints_scope_hash
            ON event_fingerprints (workspace_id, owner_id, fingerprint_hash, created_at DESC);
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS retrieval_eval_runs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            style_alignment REAL NOT NULL,
            constraint_compliance REAL NOT NULL,
            decision_traceability REAL NOT NULL,
            followup_reduction REAL NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_retrieval_eval_runs_scope_completed
            ON retrieval_eval_runs (workspace_id, user_id, session_id, completed_at DESC);
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS advisor_profiles (
            profile_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'workspace',
            routing_mode TEXT NOT NULL DEFAULT 'adaptive',
            failure_policy TEXT NOT NULL DEFAULT 'risk_aware_fail_safe',
            active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS advisor_profile_routes (
            id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL REFERENCES advisor_profiles(profile_id) ON DELETE CASCADE,
            provider_id TEXT NOT NULL,
            model TEXT,
            api_key_ref TEXT,
            base_url TEXT,
            api_version TEXT,
            region_hint TEXT,
            priority INTEGER NOT NULL DEFAULT 0,
            provider_category TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS advisor_runtime_health (
            route_key TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            model TEXT,
            success_ewma REAL NOT NULL DEFAULT 0.8,
            latency_ewma_ms REAL NOT NULL DEFAULT 350.0,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            circuit_state TEXT NOT NULL DEFAULT 'closed',
            half_open_successes INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            open_until TEXT,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS advisor_switch_audit (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            from_profile_id TEXT,
            to_profile_id TEXT NOT NULL,
            switched_by TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_advisor_profiles_scope_active
            ON advisor_profiles (workspace_id, user_id, active, updated_at DESC);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_advisor_profile_routes_profile_priority
            ON advisor_profile_routes (profile_id, priority ASC, updated_at DESC);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_advisor_runtime_health_scope_updated
            ON advisor_runtime_health (workspace_id, user_id, updated_at DESC);
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_memory_snapshots (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            objective_hash TEXT,
            snapshot_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_session_memory_snapshots_lookup
            ON session_memory_snapshots (workspace_id, user_id, session_id, objective_hash, created_at DESC);
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS handoff_records (
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
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_handoff_records_workspace_owner_ts
            ON handoff_records (workspace_id, owner_id, ts DESC);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_handoff_records_workspace_ts
            ON handoff_records (workspace_id, ts DESC);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_handoff_records_workspace_session_ts
            ON handoff_records (workspace_id, session_id, ts DESC);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_handoff_records_expires
            ON handoff_records (expires_at DESC);
        """
    )
    if not _column_exists(conn, "handoff_records", "change_summary_json"):
        conn.execute("ALTER TABLE handoff_records ADD COLUMN change_summary_json TEXT NOT NULL DEFAULT '{}'")
    if not _column_exists(conn, "handoff_records", "objective_text"):
        conn.execute("ALTER TABLE handoff_records ADD COLUMN objective_text TEXT NOT NULL DEFAULT ''")
    if not _column_exists(conn, "handoff_records", "source"):
        conn.execute("ALTER TABLE handoff_records ADD COLUMN source TEXT NOT NULL DEFAULT 'native'")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_handoff_records_workspace_owner_objective
            ON handoff_records (workspace_id, owner_id, objective_text);
        """
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS handoff_outbox (
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
        CREATE INDEX IF NOT EXISTS idx_handoff_outbox_delivery
            ON handoff_outbox (status, next_attempt_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_handoff_outbox_scope
            ON handoff_outbox (workspace_id, owner_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS continuity_resume_attempts (
            id TEXT PRIMARY KEY,
            packet_id TEXT NOT NULL UNIQUE,
            workspace_id TEXT NOT NULL,
            requesting_owner_id TEXT NOT NULL,
            target_owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT 'default',
            selected_record_id TEXT NOT NULL,
            query_text TEXT NOT NULL,
            top_file TEXT,
            requested_at TEXT NOT NULL,
            returned_at TEXT NOT NULL,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            time_since_handoff_ms INTEGER NOT NULL DEFAULT 0,
            recommended_files_json TEXT NOT NULL DEFAULT '[]',
            opened_file TEXT,
            opened_file_rank INTEGER,
            first_file_opened_at TEXT,
            productive_at TEXT,
            completed_at TEXT,
            correct_file INTEGER,
            correct_anchor INTEGER,
            correction_required INTEGER,
            correction_reason TEXT NOT NULL DEFAULT '',
            archaeology_tool_calls INTEGER,
            archaeology_tokens INTEGER,
            outcome_status TEXT,
            progress_source TEXT NOT NULL DEFAULT 'resume_packet',
            feedback_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_continuity_resume_scope
            ON continuity_resume_attempts (workspace_id, requesting_owner_id, requested_at DESC);
        """
    )


def _seed_lifecycle_defaults(conn: sqlite3.Connection) -> None:
    settings = get_settings()
    now = datetime.now(tz=UTC).isoformat()
    retention_payload = json.dumps(
        {
            "retention_days": int(settings.event_retention_days),
            "handoff_retention_days": int(getattr(settings, "handoff_retention_days", 90)),
            "behavior_control_retention_days": int(
                getattr(settings, "behavior_control_retention_days", 365)
            ),
            "archive_enabled": bool(settings.archive_enabled),
            "archive_path": settings.archive_path,
        }
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO runtime_settings(key, value, updated_at)
        VALUES(?, ?, ?)
        """,
        ("event_retention", retention_payload, now),
    )


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(
            """
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            actor TEXT NOT NULL,
            source TEXT NOT NULL,
            domain TEXT NOT NULL,
            task_type TEXT NOT NULL,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            summary_l0 TEXT NOT NULL DEFAULT '',
            summary_l1_json TEXT NOT NULL DEFAULT '{}',
            summary_version TEXT NOT NULL DEFAULT 'v1',
            summary_updated_at TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL,
            context TEXT NOT NULL,
            inputs TEXT NOT NULL,
            steps TEXT NOT NULL,
            decision TEXT,
            outcome TEXT,
            style TEXT,
            links TEXT,
            tags TEXT NOT NULL,
            sensitivity INTEGER NOT NULL,
            redaction_hints TEXT NOT NULL,
            hash TEXT NOT NULL,
            schema_version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS patterns (
            id TEXT PRIMARY KEY,
            domain TEXT NOT NULL,
            pattern_type TEXT NOT NULL,
            statement TEXT NOT NULL,
            evidence_event_ids TEXT NOT NULL,
            confidence REAL NOT NULL,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            UNIQUE(domain, pattern_type, statement)
        );

        CREATE TABLE IF NOT EXISTS workflow_templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            domain TEXT NOT NULL,
            graph TEXT NOT NULL DEFAULT '{}',
            triggers TEXT NOT NULL DEFAULT '{}',
            version INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pattern_feedback (
            id TEXT PRIMARY KEY,
            pattern_id TEXT NOT NULL,
            approved INTEGER NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runtime_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS context_bundles (
            query_hash TEXT PRIMARY KEY,
            bundle TEXT NOT NULL,
            created_at TEXT NOT NULL,
            ttl_seconds INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            consumer TEXT NOT NULL,
            action TEXT NOT NULL,
            query TEXT NOT NULL,
            result_event_ids TEXT NOT NULL,
            policy_decisions TEXT NOT NULL,
            latency_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_interactions (
            id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            interaction_id TEXT NOT NULL,
            source_consumer TEXT NOT NULL,
            source_role TEXT NOT NULL,
            target_role TEXT NOT NULL,
            action TEXT NOT NULL,
            citations TEXT NOT NULL,
            payload TEXT NOT NULL,
            allowed INTEGER NOT NULL,
            reason TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS takeover_sessions (
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'takeover',
            persona_mode TEXT NOT NULL DEFAULT 'normal',
            activation_keywords TEXT NOT NULL DEFAULT '',
            stop_keywords TEXT NOT NULL DEFAULT '',
            expires_at TEXT,
            activated_at TEXT,
            last_message_at TEXT,
            takeover_context TEXT NOT NULL DEFAULT '{}',
            objective_hash TEXT,
            working_set_json TEXT NOT NULL DEFAULT '{}',
            last_deliberation_at TEXT,
            recent_outcomes_json TEXT NOT NULL DEFAULT '[]',
            autonomy_score REAL NOT NULL DEFAULT 0.5,
            autonomy_policy_profile TEXT NOT NULL DEFAULT 'human_consultative',
            active_goal_id TEXT,
            goal_queue_size INTEGER NOT NULL DEFAULT 0,
            last_discovery_at TEXT,
            continuity_violation_count INTEGER NOT NULL DEFAULT 0,
            enforcement_mode TEXT NOT NULL DEFAULT 'strict_takeover',
            last_tick_at TEXT,
            pending_directive_count INTEGER NOT NULL DEFAULT 0,
            retry_backlog_count INTEGER NOT NULL DEFAULT 0,
            last_classification TEXT,
            last_safety_decision TEXT NOT NULL DEFAULT 'allow',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(session_id, workspace_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS autonomy_goals (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'open_discovery',
            priority_score REAL NOT NULL DEFAULT 0.0,
            risk_tier TEXT NOT NULL DEFAULT 'medium',
            confidence REAL NOT NULL DEFAULT 0.0,
            reasoning TEXT NOT NULL DEFAULT '',
            evidence_event_ids TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'candidate',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS execution_permits (
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

        CREATE TABLE IF NOT EXISTS autonomy_notices (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            goal_id TEXT,
            title TEXT NOT NULL,
            reason TEXT NOT NULL,
            priority REAL NOT NULL DEFAULT 0.0,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            acknowledged_at TEXT
        );

        CREATE TABLE IF NOT EXISTS directive_executions (
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

        CREATE TABLE IF NOT EXISTS entity_nodes (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(workspace_id, owner_id, entity_type, entity_key)
        );

        CREATE TABLE IF NOT EXISTS event_entity_links (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            role TEXT NOT NULL,
            confidence REAL NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(workspace_id, event_id, entity_id, role),
            FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE,
            FOREIGN KEY(entity_id) REFERENCES entity_nodes(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS event_relationships (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            target_event_id TEXT NOT NULL,
            relationship_type TEXT NOT NULL,
            confidence REAL NOT NULL,
            relation_meta TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(source_event_id) REFERENCES events(id) ON DELETE CASCADE,
            FOREIGN KEY(target_event_id) REFERENCES events(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS fact_assertions (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            event_id TEXT NOT NULL,
            supersedes_event_id TEXT,
            active INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS team_memberships (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            added_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL,
            UNIQUE(workspace_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS decision_observations (
            id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            consumer_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL DEFAULT '',
            situation_type TEXT NOT NULL,
            situation_summary TEXT NOT NULL,
            user_response TEXT NOT NULL,
            response_reasoning TEXT,
            outcome TEXT,
            outcome_sentiment TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            source_event_ids TEXT NOT NULL DEFAULT '[]',
            context_snapshot TEXT NOT NULL DEFAULT '{}',
            embedding TEXT,
            superseded_by TEXT,
            objective_text TEXT NOT NULL DEFAULT '',
            constraints_json TEXT NOT NULL DEFAULT '{}',
            available_choices_json TEXT NOT NULL DEFAULT '[]',
            selected_choice TEXT NOT NULL DEFAULT '',
            action_taken TEXT NOT NULL DEFAULT '',
            correction_text TEXT NOT NULL DEFAULT '',
            memory_class TEXT NOT NULL DEFAULT 'decision',
            evidence_source TEXT NOT NULL DEFAULT 'inferred',
            lifecycle_status TEXT NOT NULL DEFAULT 'active',
            valid_from TEXT NOT NULL DEFAULT '',
            valid_until TEXT,
            contradicts_ids_json TEXT NOT NULL DEFAULT '[]',
            confirmed_at TEXT,
            behavior_schema_version TEXT NOT NULL DEFAULT 'v1',
            redaction_applied INTEGER NOT NULL DEFAULT 0,
            learning_eligible INTEGER NOT NULL DEFAULT 0,
            storage_score REAL NOT NULL DEFAULT 0.0,
            storage_decision TEXT NOT NULL DEFAULT 'audit_only'
        );

        CREATE TABLE IF NOT EXISTS behavior_fidelity_runs (
            id TEXT PRIMARY KEY,
            consumer_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            gate_json TEXT NOT NULL DEFAULT '{}',
            case_results_json TEXT NOT NULL DEFAULT '[]',
            evidence_count INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        );

        CREATE TABLE IF NOT EXISTS clone_feedback (
            id TEXT PRIMARY KEY,
            observation_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            feedback_type TEXT NOT NULL,
            correction_text TEXT,
            ts TEXT NOT NULL,
            FOREIGN KEY(observation_id) REFERENCES decision_observations(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS behavioral_fingerprints (
            id TEXT PRIMARY KEY,
            consumer_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            observation_count INTEGER NOT NULL DEFAULT 0,
            last_updated_at TEXT NOT NULL,
            UNIQUE(consumer_id, workspace_id)
        );

        CREATE TABLE IF NOT EXISTS dashboard_human_score_snapshots (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            score INTEGER NOT NULL,
            band TEXT NOT NULL,
            subscores_json TEXT NOT NULL DEFAULT '{}',
            inputs_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
        CREATE INDEX IF NOT EXISTS idx_events_domain_task_type_ts
            ON events (domain, task_type, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_patterns_domain_conf ON patterns (domain, confidence DESC);
        CREATE INDEX IF NOT EXISTS idx_workflow_templates_domain_updated
            ON workflow_templates (domain, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_interactions_key ON agent_interactions (interaction_id, action);
        CREATE INDEX IF NOT EXISTS idx_takeover_sessions_scope
            ON takeover_sessions (workspace_id, user_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_autonomy_goals_session_status_priority
            ON autonomy_goals (session_id, status, priority_score DESC, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_execution_permits_session_decision_expires
            ON execution_permits (session_id, decision, expires_at DESC);
        CREATE INDEX IF NOT EXISTS idx_autonomy_notices_session_priority_created
            ON autonomy_notices (session_id, acknowledged_at, priority DESC, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_directive_executions_session_state_started
            ON directive_executions (session_id, state, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_dashboard_human_score_scope_created
            ON dashboard_human_score_snapshots (workspace_id, user_id, session_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_entity_nodes_scope
            ON entity_nodes (workspace_id, owner_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_event_entity_links_event
            ON event_entity_links (workspace_id, event_id);
        CREATE INDEX IF NOT EXISTS idx_event_relationships_source
            ON event_relationships (workspace_id, source_event_id);
        CREATE INDEX IF NOT EXISTS idx_fact_assertions_active
            ON fact_assertions (workspace_id, owner_id, fact_key, active);
        CREATE INDEX IF NOT EXISTS idx_team_memberships_workspace
            ON team_memberships (workspace_id, created_at ASC);
        CREATE INDEX IF NOT EXISTS idx_observations_workspace
            ON decision_observations (workspace_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_observations_type
            ON decision_observations (workspace_id, situation_type);
        CREATE INDEX IF NOT EXISTS idx_clone_feedback_obs
            ON clone_feedback (observation_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_clone_feedback_session
            ON clone_feedback (session_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_log_ts
            ON audit_log (ts DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_interactions_ts
            ON agent_interactions (ts DESC);
        CREATE INDEX IF NOT EXISTS idx_fingerprints_scope
            ON behavioral_fingerprints (consumer_id, workspace_id);
        """
        )
        if not _column_exists(conn, "events", "summary_l0"):
            conn.execute("ALTER TABLE events ADD COLUMN summary_l0 TEXT NOT NULL DEFAULT ''")
        if not _column_exists(conn, "events", "summary_l1_json"):
            conn.execute("ALTER TABLE events ADD COLUMN summary_l1_json TEXT NOT NULL DEFAULT '{}'")
        if not _column_exists(conn, "events", "summary_version"):
            conn.execute("ALTER TABLE events ADD COLUMN summary_version TEXT NOT NULL DEFAULT 'v1'")
        if not _column_exists(conn, "events", "summary_updated_at"):
            conn.execute("ALTER TABLE events ADD COLUMN summary_updated_at TEXT NOT NULL DEFAULT ''")
        _ensure_takeover_v3_schema(conn)
        _ensure_behavior_fidelity_schema(conn)
        _ensure_continuity_v04_schema(conn)
        _seed_lifecycle_defaults(conn)
        conn.commit()
    finally:
        conn.close()


def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()
