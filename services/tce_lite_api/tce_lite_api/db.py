from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

from .config import get_settings
from .dream_adjudication_store import ensure_dream_adjudication_tables


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


def _ensure_directive_lease_schema(conn: sqlite3.Connection) -> None:
    """P0 trust boundary: lease fencing / verification / report idempotency on directives, permit binding."""
    directive_columns = {
        "lease_generation": "INTEGER NOT NULL DEFAULT 0",
        "claimed_executor": "TEXT",
        "lease_expires_at": "TEXT",
        "verification_state": "TEXT NOT NULL DEFAULT 'unverified'",
        "report_idempotency_key": "TEXT",
        "report_payload_hash": "TEXT",
        "cancelled_at": "TEXT",
        "cancel_reason": "TEXT",
    }
    for name, ddl in directive_columns.items():
        if not _column_exists(conn, "directive_executions", name):
            conn.execute(f"ALTER TABLE directive_executions ADD COLUMN {name} {ddl}")
    permit_columns = {
        "user_id": "TEXT",
        "requested_by": "TEXT",
        "directive_id": "TEXT",
        "attempt": "INTEGER",
        "objective_hash": "TEXT",
        "policy_revision": "TEXT",
        "scope_digest": "TEXT",
        "resolved_by": "TEXT",
    }
    for name, ddl in permit_columns.items():
        if not _column_exists(conn, "execution_permits", name):
            conn.execute(f"ALTER TABLE execution_permits ADD COLUMN {name} {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_directive_executions_report_idem ON directive_executions (workspace_id, report_idempotency_key)"
    )


def _ensure_continuity_v05_schema(conn: sqlite3.Connection) -> None:
    """P0 trust boundary: project/executor binding on handoffs and the reader-vs-source session split."""
    for name in ("project_id", "git_remote", "executor_id"):
        if not _column_exists(conn, "handoff_records", name):
            conn.execute(f"ALTER TABLE handoff_records ADD COLUMN {name} TEXT")
    for name in ("executor_id", "payload_hash"):
        if not _column_exists(conn, "handoff_outbox", name):
            conn.execute(f"ALTER TABLE handoff_outbox ADD COLUMN {name} TEXT")
    if not _column_exists(conn, "continuity_resume_attempts", "source_session_id"):
        conn.execute("ALTER TABLE continuity_resume_attempts ADD COLUMN source_session_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_handoff_records_workspace_project_ts ON handoff_records (workspace_id, project_id, ts DESC)"
    )


def _ensure_trusted_capture_schema(conn: sqlite3.Connection) -> None:
    """P1 trusted capture: input receipts, decision opportunities/candidates, human resolutions,
    and the prospective-vs-retrospective labelling on shadow predictions / observations.

    Mirrors alembic revision 20260909_0036 (Full). Additive and idempotent only."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trusted_input_receipts (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            host_session_id TEXT NOT NULL,
            sequence INTEGER,
            prompt_id TEXT,
            delivery_key TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            origin_kind TEXT NOT NULL,
            capture_principal TEXT NOT NULL,
            host_client TEXT NOT NULL DEFAULT 'claude',
            event_id TEXT,
            project_id TEXT,
            observed_at TEXT NOT NULL,
            ingested_at TEXT NOT NULL,
            original_char_count INTEGER NOT NULL DEFAULT 0,
            content_truncated INTEGER NOT NULL DEFAULT 0,
            redaction_applied_json TEXT NOT NULL DEFAULT '[]',
            spool_depth INTEGER NOT NULL DEFAULT 0,
            spool_failures INTEGER NOT NULL DEFAULT 0,
            gap_since TEXT,
            queue_state TEXT NOT NULL DEFAULT 'inline',
            extraction_state TEXT NOT NULL DEFAULT 'pending',
            extraction_lease_until TEXT,
            extraction_attempts INTEGER NOT NULL DEFAULT 0,
            extraction_last_error TEXT,
            extraction_version_done TEXT,
            next_extraction_at TEXT,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_trusted_input_receipts_delivery ON trusted_input_receipts (workspace_id, owner_id, delivery_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trusted_input_receipts_subject_ingested ON trusted_input_receipts (workspace_id, subject_user_id, ingested_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trusted_input_receipts_extraction ON trusted_input_receipts (extraction_state, next_extraction_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_opportunities (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            owner_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL,
            turn INTEGER,
            objective_hash TEXT,
            task_id TEXT,
            project_id TEXT,
            decision_family TEXT NOT NULL,
            situation_type TEXT NOT NULL DEFAULT 'choice_required',
            question_text TEXT NOT NULL DEFAULT '',
            alternatives_json TEXT NOT NULL DEFAULT '[]',
            pre_answer_snapshot_json TEXT NOT NULL DEFAULT '{}',
            evidence_cutoff_at TEXT,
            evidence_revision TEXT,
            advice_exposure_json TEXT NOT NULL DEFAULT '{}',
            shadow_prediction_id TEXT,
            source_event_id TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            relayed_answer TEXT,
            relayed_at TEXT,
            resolved_at TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            frozen_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_opportunities_open ON decision_opportunities (workspace_id, subject_user_id, status, created_at DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_opportunities_session ON decision_opportunities (session_id, status)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_candidates (
            id TEXT PRIMARY KEY,
            receipt_id TEXT NOT NULL,
            source_event_id TEXT,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            opportunity_id TEXT,
            candidate_kind TEXT NOT NULL,
            supporting_span TEXT NOT NULL,
            span_sha256 TEXT NOT NULL,
            observed_alternatives_json TEXT NOT NULL DEFAULT '[]',
            selected_option TEXT,
            stated_rationale TEXT,
            is_negated INTEGER NOT NULL DEFAULT 0,
            is_correction INTEGER NOT NULL DEFAULT 0,
            project_id TEXT,
            task_id TEXT,
            origin_kind TEXT NOT NULL,
            extraction_version TEXT NOT NULL,
            promotion TEXT NOT NULL,
            promotion_reason TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            promoted_observation_id TEXT,
            review_id TEXT,
            created_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_decision_candidates_span ON decision_candidates (receipt_id, extraction_version, span_sha256)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_candidates_opportunity ON decision_candidates (opportunity_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS human_resolutions (
            id TEXT PRIMARY KEY,
            opportunity_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            receipt_id TEXT,
            source_event_id TEXT,
            candidate_id TEXT,
            selected_choice TEXT NOT NULL DEFAULT '',
            correction_text TEXT NOT NULL DEFAULT '',
            stated_rationale TEXT,
            resolution_source TEXT NOT NULL,
            human_source_ref TEXT NOT NULL,
            observation_id TEXT,
            supersedes_resolution_id TEXT,
            resolved_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_human_resolutions_opportunity ON human_resolutions (opportunity_id, resolved_at DESC)")
    shadow_columns = {
        "opportunity_id": "TEXT",
        "session_id": "TEXT",
        "turn": "INTEGER",
        "decision_family": "TEXT",
        "prediction_stage": "TEXT NOT NULL DEFAULT 'retrospective'",
        "frozen_at": "TEXT",
        "evidence_cutoff_at": "TEXT",
        "evidence_revision": "TEXT",
        "prediction_shown_at": "TEXT",
        "advice_visible": "INTEGER NOT NULL DEFAULT 0",
        "resolution_state": "TEXT NOT NULL DEFAULT 'resolved'",
        "resolved_at": "TEXT",
        "resolution_source": "TEXT",
        "human_source_ref": "TEXT",
        "resolution_source_event_id": "TEXT",
        "corrections_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    for name, ddl in shadow_columns.items():
        if not _column_exists(conn, "behavior_shadow_predictions", name):
            conn.execute(f"ALTER TABLE behavior_shadow_predictions ADD COLUMN {name} {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_behavior_shadow_open ON behavior_shadow_predictions (workspace_id, subject_user_id, resolution_state, created_at DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_behavior_shadow_opportunity ON behavior_shadow_predictions (opportunity_id)")
    for name in ("opportunity_id", "origin_kind", "capture_receipt_id", "extraction_version"):
        if not _column_exists(conn, "decision_observations", name):
            conn.execute(f"ALTER TABLE decision_observations ADD COLUMN {name} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_obs_opportunity ON decision_observations (opportunity_id)")


def _ensure_task_state_schema(conn: sqlite3.Connection) -> None:
    """P2 durable task state: versioned projection, immutable task state events, planning jobs, verifications.

    Mirrors alembic revision 20260909_0037 (Full). Additive and idempotent only.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_states (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL,
            project_id TEXT,
            revision INTEGER NOT NULL DEFAULT 0,
            contract_revision INTEGER NOT NULL DEFAULT 0,
            highest_seq INTEGER NOT NULL DEFAULT 0,
            objective_text TEXT NOT NULL DEFAULT '',
            objective_hash TEXT,
            objective_set_at TEXT,
            objective_set_seq INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'awaiting_objective',
            next_permitted_action TEXT NOT NULL DEFAULT 'await_owner_objective',
            plan_state TEXT NOT NULL DEFAULT 'absent',
            plan_producer TEXT,
            plan_root_goal_id TEXT,
            planning_job_id TEXT,
            constraints_json TEXT NOT NULL DEFAULT '[]',
            open_decisions_json TEXT NOT NULL DEFAULT '[]',
            plan_json TEXT NOT NULL DEFAULT '{}',
            unresolved_effects_json TEXT NOT NULL DEFAULT '[]',
            latest_verification_json TEXT NOT NULL DEFAULT '{}',
            citations_json TEXT NOT NULL DEFAULT '[]',
            source_revision TEXT NOT NULL DEFAULT '',
            cancelled_at TEXT,
            cancelled_seq INTEGER NOT NULL DEFAULT 0,
            last_cancel_seq INTEGER NOT NULL DEFAULT 0,
            cancel_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_task_states_identity ON task_states (workspace_id, owner_id, task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_states_session ON task_states (workspace_id, session_id, updated_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_state_events (
            id TEXT PRIMARY KEY,
            task_state_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            kind TEXT NOT NULL,
            contract_revision INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            source_event_id TEXT,
            directive_id TEXT,
            goal_id TEXT,
            actor TEXT NOT NULL DEFAULT '',
            occurred_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_task_state_events_seq ON task_state_events (task_state_id, seq)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_state_events_task ON task_state_events (workspace_id, task_id, seq)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_state_events_kind ON task_state_events (task_state_id, kind, seq DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS planning_jobs (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL,
            task_state_id TEXT,
            job_kind TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            input_revision TEXT NOT NULL DEFAULT '',
            contract_revision INTEGER NOT NULL DEFAULT 0,
            objective_hash TEXT,
            objective_text TEXT NOT NULL DEFAULT '',
            charter_json TEXT NOT NULL DEFAULT '{}',
            scope_json TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL DEFAULT 'pending',
            lease_owner TEXT,
            lease_until TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            next_attempt_at TEXT,
            last_error TEXT,
            producer TEXT,
            result_json TEXT NOT NULL DEFAULT '{}',
            queue_state TEXT NOT NULL DEFAULT 'inline',
            rq_job_id TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_planning_jobs_idem ON planning_jobs (workspace_id, idempotency_key)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_planning_jobs_dispatch ON planning_jobs (state, next_attempt_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_planning_jobs_task ON planning_jobs (workspace_id, task_id, created_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_verifications (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            directive_id TEXT,
            state TEXT NOT NULL DEFAULT 'unverified',
            method TEXT NOT NULL DEFAULT 'none',
            summary TEXT NOT NULL DEFAULT '',
            evidence_event_ids_json TEXT NOT NULL DEFAULT '[]',
            recorded_by TEXT NOT NULL DEFAULT '',
            recorded_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1',
            contract_revision INTEGER NOT NULL DEFAULT 0,
            plan_id TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_verifications_task ON task_verifications (workspace_id, task_id, recorded_at DESC)"
    )
    goal_columns = {
        "plan_contract_revision": "INTEGER",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "blocked_reason": "TEXT",
        "depends_on_json": "TEXT NOT NULL DEFAULT '[]'",
        "mutating": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, ddl in goal_columns.items():
        if not _column_exists(conn, "autonomy_goals", name):
            conn.execute(f"ALTER TABLE autonomy_goals ADD COLUMN {name} {ddl}")
    handoff_columns = {
        "task_id": "TEXT",
        "task_state_revision": "INTEGER",
        "contract_revision": "INTEGER",
        "verification_refs_json": "TEXT NOT NULL DEFAULT '[]'",
        "unresolved_effects_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    for name, ddl in handoff_columns.items():
        if not _column_exists(conn, "handoff_records", name):
            conn.execute(f"ALTER TABLE handoff_records ADD COLUMN {name} {ddl}")


def _ensure_charter_effects_schema(conn: sqlite3.Connection) -> None:
    """P3 authority charter, effect journal, dispatch records, acceptance criteria and verification.

    Mirrors alembic revision 20260909_0039 (Full).  Additive and idempotent only; no ``commit()``
    here — ``init_db`` commits once at the end.

    Lite parity note: ``effect_journal`` is new in this revision, so there are no pre-existing rows
    and ``NOT NULL`` on ``enforcement_tier``/``action_tracing`` is declared directly rather than
    backfilled.  G4's live assertion therefore holds in both backends for the same reason.

    The two UNIQUE indexes on ``effect_journal`` are load-bearing, not decoration:
    ``uq_effect_journal_intent`` is what makes "exactly one row per intent" a database guarantee
    (the idempotent re-POST of ``/v1/effects``), and ``uq_effect_journal_seq`` is what stops two
    workers minting the same ``seq`` under one directive.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS authority_charters (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            project_id TEXT,
            charter_version TEXT NOT NULL,
            policy_revision TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            enforcement_tier TEXT NOT NULL,
            credential_risk_acknowledged INTEGER NOT NULL DEFAULT 0,
            source_receipt_id TEXT NOT NULL,
            permitted_roots_json TEXT NOT NULL DEFAULT '[]',
            protected_write_prefixes_json TEXT NOT NULL DEFAULT '[]',
            denied_read_paths_json TEXT NOT NULL DEFAULT '[]',
            permitted_capabilities_json TEXT NOT NULL DEFAULT '[]',
            confirm_required_capabilities_json TEXT NOT NULL DEFAULT '[]',
            egress_mode TEXT NOT NULL DEFAULT 'deny_all',
            runtime_allowlist_json TEXT NOT NULL DEFAULT '[]',
            task_families_json TEXT NOT NULL DEFAULT '[]',
            max_attempts INTEGER NOT NULL DEFAULT 3,
            max_concurrent_dispatches INTEGER NOT NULL DEFAULT 1,
            max_wall_seconds INTEGER NOT NULL DEFAULT 1800,
            budget_minor_units INTEGER NOT NULL DEFAULT 0,
            budget_currency TEXT NOT NULL DEFAULT 'USD',
            spend_enforcement TEXT NOT NULL DEFAULT 'unsupported',
            charter_digest TEXT NOT NULL,
            approved_by TEXT,
            approved_at TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            revoke_reason TEXT,
            superseded_by TEXT,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_authority_charters_scope_status "
        "ON authority_charters (workspace_id, owner_id, status, expires_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS charter_narrowings (
            id TEXT PRIMARY KEY,
            charter_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            source_receipt_id TEXT NOT NULL,
            narrowing_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            expires_at TEXT,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_charter_narrowings_session "
        "ON charter_narrowings (workspace_id, session_id, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_charter_narrowings_charter ON charter_narrowings (charter_id, created_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dispatch_records (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            task_id TEXT,
            directive_id TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            charter_id TEXT NOT NULL,
            charter_digest TEXT NOT NULL,
            claimed_by TEXT NOT NULL DEFAULT '',
            enforcement_tier TEXT NOT NULL,
            sandbox_provider TEXT NOT NULL,
            sandbox_profile_digest TEXT,
            sandbox_self_test_id TEXT,
            runtime_id TEXT NOT NULL,
            runtime_version TEXT NOT NULL,
            surface TEXT NOT NULL,
            model_id TEXT NOT NULL DEFAULT '',
            contract_digest TEXT NOT NULL DEFAULT '',
            capability_matrix_json TEXT NOT NULL DEFAULT '{}',
            provider_run_id TEXT,
            provider_turn_id TEXT,
            task_family TEXT NOT NULL DEFAULT 'unspecified',
            budget_reserved_minor_units INTEGER NOT NULL DEFAULT 0,
            budget_currency TEXT NOT NULL DEFAULT 'USD',
            spend_enforcement TEXT NOT NULL,
            cap_applied_json TEXT,
            cost_minor_units INTEGER,
            cost_source TEXT,
            tokens_input INTEGER NOT NULL DEFAULT 0,
            tokens_output INTEGER NOT NULL DEFAULT 0,
            tokens_cached_input INTEGER NOT NULL DEFAULT 0,
            tokens_reasoning INTEGER NOT NULL DEFAULT 0,
            human_intervention_count INTEGER NOT NULL DEFAULT 0,
            outcome TEXT,
            terminal_reason TEXT,
            wall_ms INTEGER,
            started_at TEXT,
            finished_at TEXT,
            reconciled_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dispatch_records_directive_attempt "
        "ON dispatch_records (directive_id, attempt)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dispatch_records_open ON dispatch_records (workspace_id, outcome, started_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS effect_journal (
            effect_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            task_id TEXT,
            directive_id TEXT NOT NULL,
            dispatch_id TEXT,
            seq INTEGER NOT NULL,
            state TEXT NOT NULL,
            kind TEXT NOT NULL,
            reversibility TEXT NOT NULL,
            capability TEXT NOT NULL,
            resource TEXT NOT NULL,
            argv_json TEXT NOT NULL DEFAULT '[]',
            description TEXT NOT NULL DEFAULT '',
            intent_digest TEXT NOT NULL,
            enforcement_tier TEXT NOT NULL,
            action_tracing TEXT NOT NULL,
            lease_generation INTEGER NOT NULL DEFAULT 0,
            claimed_executor TEXT,
            provider_run_id TEXT,
            provider_turn_id TEXT,
            runtime_id TEXT,
            runtime_version TEXT,
            model_id TEXT,
            opened_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution_source TEXT,
            resolved_by_actor TEXT,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_effect_journal_intent ON effect_journal (directive_id, intent_digest)"
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_effect_journal_seq ON effect_journal (directive_id, seq)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_effect_journal_open ON effect_journal (workspace_id, state, opened_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_effect_journal_session_open "
        "ON effect_journal (workspace_id, owner_id, session_id, state)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS acceptance_criteria (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            task_id TEXT,
            directive_id TEXT NOT NULL,
            charter_id TEXT,
            checks_json TEXT NOT NULL DEFAULT '[]',
            criteria_digest TEXT NOT NULL,
            corpus_digest TEXT NOT NULL,
            corpus_manifest_json TEXT NOT NULL DEFAULT '[]',
            frozen_at TEXT NOT NULL,
            frozen_by TEXT NOT NULL,
            policy_revision TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_acceptance_criteria_directive ON acceptance_criteria (directive_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS verification_results (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            task_id TEXT,
            directive_id TEXT NOT NULL,
            criteria_id TEXT,
            criteria_digest_at_run TEXT NOT NULL,
            corpus_digest_at_run TEXT NOT NULL,
            criteria_digest_match INTEGER NOT NULL DEFAULT 0,
            corpus_digest_match INTEGER NOT NULL DEFAULT 0,
            verdict TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            runner_principal TEXT NOT NULL,
            executing_identity TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            commit_sha TEXT,
            tree_sha TEXT,
            checks_json TEXT NOT NULL DEFAULT '[]',
            reviewer_model_json TEXT,
            recorded_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_verification_results_directive "
        "ON verification_results (directive_id, recorded_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sandbox_self_tests (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            host_id TEXT NOT NULL,
            sandbox_provider TEXT NOT NULL,
            provider_version TEXT NOT NULL DEFAULT '',
            profile_digest TEXT NOT NULL DEFAULT '',
            assertions_json TEXT NOT NULL DEFAULT '[]',
            passed INTEGER NOT NULL DEFAULT 0,
            uid_separation INTEGER NOT NULL DEFAULT 0,
            ran_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sandbox_self_tests_recent "
        "ON sandbox_self_tests (workspace_id, sandbox_provider, ran_at DESC)"
    )
    added_columns = {
        ("directive_executions", "dispatch_id"): "TEXT",
        ("directive_executions", "charter_id"): "TEXT",
        ("execution_permits", "charter_id"): "TEXT",
        ("execution_permits", "charter_version"): "TEXT",
        ("capability_grants", "charter_id"): "TEXT",
        ("handoff_records", "verification_state"): "TEXT",
    }
    for (table, column), ddl in added_columns.items():
        if not _column_exists(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _ensure_decision_policy_schema(conn: sqlite3.Connection) -> None:
    """P4 decision policy: qualification records, threshold registrations, and the columns a
    decision needs.

    Mirrors alembic revision ``20260909_0040`` (Full).  Additive and idempotent only; no
    ``commit()`` here — ``init_db`` commits once at the end.  Table names, column names and
    index names are identical to the Full revision so the two can be diffed line for line;
    only the types are rendered in the house Lite mapping (``UUID``/``TIMESTAMPTZ`` -> ``TEXT``
    holding ISO strings, ``JSONB`` -> ``TEXT`` holding JSON, ``BOOLEAN`` -> ``INTEGER`` 0/1,
    ``REAL`` -> ``REAL``).

    Two defaults are load-bearing and are the same in both backends:

    * ``decision_advice_shown`` defaults to **1** (true), the conservative direction.  A row
      written by a writer that has not been upgraded is *excluded* from the promotion gate
      rather than silently admitted to it.  The column it replaces in that role,
      ``advice_visible``, was produced as ``bool(<eight-key dict literal>)`` at three sites in
      this file's callers, so it was ``True`` unconditionally and never measured anything.
    * ``threshold_registrations.registered_at`` defaults to the **server** clock and takes no
      client-supplied value.  That is tamper-evident, not tamper-proof, and the docstring in
      ``policy_thresholds`` says so.
    """

    # Imported here rather than at module scope: `policy_store` is a P4 module and `db` is
    # imported by everything, so the one name they share travels in the direction that cannot
    # produce an import cycle. The physical column names are the Full revision's; this is the
    # sqlite rendering of the same four -- see the constant's docstring.
    from .policy_store import LITE_REPLAY_INPUT_DDL

    # Full applies its twin exactly once, as an alembic revision. This guard runs on every
    # boot, so the two statements at the bottom that are one-time DATA changes rather than
    # idempotent schema changes must be fenced. Without the fence, the fidelity-run
    # invalidation would re-fire on every restart and wipe the status of runs recorded after
    # P4 landed -- a migration that keeps migrating.
    first_migration_pass = not _column_exists(conn, "behavior_shadow_predictions", "policy_json")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_qualifications (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL,
            project_id TEXT,
            decision_family TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'not_qualified',
            decision_policy_revision TEXT NOT NULL,
            model_id TEXT NOT NULL DEFAULT '',
            runtime_version TEXT NOT NULL DEFAULT '',
            prompt_sha256 TEXT NOT NULL DEFAULT '',
            retrieval_version TEXT NOT NULL DEFAULT '',
            evidence_revision TEXT,
            evidence_cutoff_at TEXT,
            learning_eligible_at_qualification INTEGER NOT NULL DEFAULT 0,
            thresholds_sha TEXT NOT NULL,
            thresholds_version TEXT NOT NULL DEFAULT '',
            tuning_sha TEXT NOT NULL,
            split_sha256 TEXT,
            split_json TEXT NOT NULL DEFAULT '{}',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            gate_json TEXT NOT NULL DEFAULT '{}',
            shortfalls_json TEXT NOT NULL DEFAULT '[]',
            adjudicated_count INTEGER NOT NULL DEFAULT 0,
            non_abstained_count INTEGER NOT NULL DEFAULT 0,
            coverage REAL NOT NULL DEFAULT 0,
            precision_lower_bound REAL NOT NULL DEFAULT 0,
            distinct_episodes INTEGER NOT NULL DEFAULT 0,
            duplicate_context_ratio REAL NOT NULL DEFAULT 0,
            baselines_json TEXT NOT NULL DEFAULT '{}',
            exclusions_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_policy_qualifications_attempt "
        "ON policy_qualifications (workspace_id, subject_user_id, project_id, decision_family, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_policy_qualifications_live "
        "ON policy_qualifications (workspace_id, subject_user_id, decision_family, state, created_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS threshold_registrations (
            id TEXT PRIMARY KEY,
            thresholds_sha TEXT NOT NULL,
            tuning_sha TEXT NOT NULL,
            thresholds_json TEXT NOT NULL DEFAULT '{}',
            registered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_threshold_registrations_sha "
        "ON threshold_registrations (thresholds_sha, tuning_sha)"
    )

    # Same names, same order as _OBSERVATION_COLUMNS / _SHADOW_COLUMNS in the Full revision.
    observation_columns = {
        "project_id": "TEXT",
        "decision_family": "TEXT",
        "task_id": "TEXT",
        "episode_key": "TEXT",
    }
    for name, ddl in observation_columns.items():
        if not _column_exists(conn, "decision_observations", name):
            conn.execute(f"ALTER TABLE decision_observations ADD COLUMN {name} {ddl}")
    shadow_columns = {
        "decision_policy_revision": "TEXT",
        "model_id": "TEXT",
        "runtime_version": "TEXT",
        "prompt_sha256": "TEXT",
        "retrieval_version": "TEXT",
        "episode_key": "TEXT",
        "project_id": "TEXT",
        "abstain_reason": "TEXT",
        "ood_status": "TEXT",
        "conflict_status": "TEXT",
        "policy_score": "REAL NOT NULL DEFAULT 0",
        "exposed": "INTEGER NOT NULL DEFAULT 0",
        "decision_advice_shown": "INTEGER NOT NULL DEFAULT 1",
        "candidate_option_count": "INTEGER NOT NULL DEFAULT 0",
        "request_fingerprint": "TEXT",
        "policy_json": "TEXT NOT NULL DEFAULT '{}'",
        # ---- alembic 20260909_0041's four, same names, same order ------------------------
        # The inputs a replay needs and could not previously get: the OFFERED evidence set (as
        # opposed to the cited subset `policy_json` already carries), the decision's own
        # timestamp (the freeze site takes a second `now_utc()`, so `frozen_at` is later), and
        # the two advisor terms `DecisionRequest.fingerprint()` hashes. Without them a row with
        # non-empty evidence could not be reconstructed at all: replay had to re-retrieve, and a
        # re-retrieval answers a different question.
        **LITE_REPLAY_INPUT_DDL,
    }
    for name, ddl in shadow_columns.items():
        if not _column_exists(conn, "behavior_shadow_predictions", name):
            conn.execute(f"ALTER TABLE behavior_shadow_predictions ADD COLUMN {name} {ddl}")
    if not _column_exists(conn, "decision_opportunities", "episode_key"):
        conn.execute("ALTER TABLE decision_opportunities ADD COLUMN episode_key TEXT")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_obs_policy_scope "
        "ON decision_observations (workspace_id, subject_user_id, decision_family, situation_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_behavior_shadow_episode "
        "ON behavior_shadow_predictions (workspace_id, subject_user_id, decision_family, episode_key)"
    )

    if not first_migration_pass:
        return

    # ---- one-time data changes, fenced above -----------------------------------------

    # Backfill from the opportunity that promoted the observation, exactly as the Full
    # revision does. Reaches P1-era promoted rows only; everything older keeps NULL and is
    # handled by the NULL-project rule in observations_scope_predicate (admissible evidence,
    # never sufficient on its own).
    conn.execute(
        """
        UPDATE decision_observations
           SET project_id = (
                   SELECT d.project_id FROM decision_opportunities d WHERE d.id = decision_observations.opportunity_id
               ),
               decision_family = (
                   SELECT d.decision_family FROM decision_opportunities d WHERE d.id = decision_observations.opportunity_id
               ),
               task_id = (
                   SELECT d.task_id FROM decision_opportunities d WHERE d.id = decision_observations.opportunity_id
               )
         WHERE project_id IS NULL
           AND opportunity_id IS NOT NULL
           AND EXISTS (SELECT 1 FROM decision_opportunities d WHERE d.id = decision_observations.opportunity_id)
        """
    )

    # Every stored fidelity run was computed under corpus-relative recency and a row-index
    # split, so none is comparable with anything measured after this revision. Invalidated
    # rather than migrated; the qualification reporter refuses to read them.
    conn.execute("UPDATE behavior_fidelity_runs SET status = 'invalidated_by_p4'")


def _ensure_dream_proposal_schema(conn: sqlite3.Connection) -> None:
    """P5 aspirations: an append-only proposal log, the human transition vocabulary, and the
    record of every generation attempt — including the ones that refused to generate anything.

    Mirrors alembic revision ``20260909_0042`` (Full), column for column and index name for
    index name, so the two schemas can be diffed rather than reasoned about.  Type mapping is
    the house one: ``UUID`` -> ``TEXT``, ``TIMESTAMPTZ`` -> ``TEXT`` (ISO-8601 strings),
    ``JSONB`` -> ``TEXT`` with a ``'[]'`` / ``'{}'`` string default, ``BOOLEAN`` -> ``INTEGER``.

    Additive and idempotent only.  Like the Full revision, this guard never drops or recreates
    a table that exists, and it touches no ``autonomy_goals`` row: these tables hold the
    owner's own accept/reject answers, and Lite has no downgrade path that could put them back.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dream_proposals (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL DEFAULT '',
            project_id TEXT,
            scope_kind TEXT NOT NULL DEFAULT 'project',
            session_id TEXT NOT NULL DEFAULT '',
            run_id TEXT,
            revision INTEGER NOT NULL DEFAULT 0,
            highest_seq INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'proposed',
            nonresponse_state TEXT NOT NULL DEFAULT 'never_surfaced',
            surfaced_count INTEGER NOT NULL DEFAULT 0,
            surfaced_attested INTEGER NOT NULL DEFAULT 0,
            first_surfaced_at TEXT,
            last_surfaced_at TEXT,
            title TEXT NOT NULL,
            connection_text TEXT NOT NULL DEFAULT '',
            benefit_text TEXT NOT NULL DEFAULT '',
            first_step TEXT NOT NULL DEFAULT '',
            citations_json TEXT NOT NULL DEFAULT '[]',
            citation_count INTEGER NOT NULL DEFAULT 0,
            evidence_basis TEXT NOT NULL DEFAULT 'trusted_current',
            evidence_revision TEXT NOT NULL DEFAULT '',
            evidence_cutoff_at TEXT,
            theme_tokens_json TEXT NOT NULL DEFAULT '[]',
            supersedes_proposal_id TEXT,
            repropose_depth INTEGER NOT NULL DEFAULT 0,
            snooze_until TEXT,
            expires_at TEXT,
            accepted_at TEXT,
            rejected_at TEXT,
            rejection_reason TEXT NOT NULL DEFAULT '',
            task_id TEXT,
            objective_hash TEXT,
            plan_root_goal_id TEXT,
            pursuit_started_at TEXT,
            completed_at TEXT,
            abandoned_at TEXT,
            abandon_reason TEXT NOT NULL DEFAULT '',
            withdrawn_reason TEXT NOT NULL DEFAULT '',
            source_revision TEXT NOT NULL DEFAULT '',
            policy_revision TEXT NOT NULL DEFAULT 'p5-2026-09',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dream_proposals_scope_status "
        "ON dream_proposals (workspace_id, owner_id, subject_user_id, scope_kind, project_id, status, updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dream_proposals_task ON dream_proposals (workspace_id, owner_id, task_id)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dream_proposals_run ON dream_proposals (run_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dream_proposals_supersedes ON dream_proposals (supersedes_proposal_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dream_proposal_events (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            actor TEXT NOT NULL DEFAULT '',
            actor_class TEXT NOT NULL DEFAULT 'system',
            source_event_id TEXT,
            run_id TEXT,
            occurred_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    # The uniqueness that makes the append idempotent, exactly as in the Full revision: an
    # INSERT ... ON CONFLICT DO NOTHING on (proposal_id, seq) is what lets a retried CAS
    # re-issue its events without doubling them.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dream_proposal_events_seq ON dream_proposal_events (proposal_id, seq)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dream_proposal_events_kind "
        "ON dream_proposal_events (workspace_id, owner_id, kind, occurred_at DESC)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dream_generation_runs (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL DEFAULT '',
            project_id TEXT,
            scope_kind TEXT NOT NULL DEFAULT 'project',
            scope_key TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            planning_job_id TEXT,
            state TEXT NOT NULL DEFAULT 'running',
            refusal_reason TEXT NOT NULL DEFAULT '',
            pool_size INTEGER NOT NULL DEFAULT 0,
            pool_json TEXT NOT NULL DEFAULT '[]',
            pool_drops_json TEXT NOT NULL DEFAULT '{}',
            evidence_revision TEXT NOT NULL DEFAULT '',
            evidence_cutoff_at TEXT,
            candidates_returned INTEGER NOT NULL DEFAULT 0,
            proposals_written INTEGER NOT NULL DEFAULT 0,
            refusals_json TEXT NOT NULL DEFAULT '{}',
            model_provider TEXT NOT NULL DEFAULT '',
            prompt_hash TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            finished_at TEXT,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dream_generation_runs_scope "
        "ON dream_generation_runs (workspace_id, owner_id, scope_kind, project_id, started_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dream_generation_runs_job ON dream_generation_runs (planning_job_id)"
    )
    # At most one in-flight run per scope, enforced by the database rather than by a read
    # followed by a write.  SQLite supports partial unique indexes, so this is the same
    # predicate the Full revision writes rather than a Lite-shaped approximation.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dream_generation_runs_inflight "
        "ON dream_generation_runs (workspace_id, owner_id, scope_key) WHERE state = 'running'"
    )


def _ensure_pilot_enrolment_schema(conn: sqlite3.Connection) -> None:
    """P6 operational proof: an arm frozen before the work starts, and an adjudication only a
    verified human writes.

    Mirrors alembic revision ``20260909_0043`` (Full) column for column and index name for index
    name, so the two schemas can be diffed rather than reasoned about.  Type mapping is the house
    one: ``UUID`` -> ``TEXT``, ``TIMESTAMPTZ`` -> ``TEXT`` (ISO-8601 UTC), ``JSONB`` -> ``TEXT``
    with a ``'[]'`` default, ``BOOLEAN`` -> ``INTEGER``.

    ``pilot_strata`` exists to make a permuted block *exact* rather than balanced-in-expectation.
    Its slot is taken by a single ``INSERT ... ON CONFLICT(stratum_id) DO UPDATE SET next_slot =
    next_slot + 1 RETURNING next_slot - 1`` — supported from SQLite 3.35 — inside the
    ``BEGIN IMMEDIATE`` pattern, so two concurrent enrolments cannot take the same slot.

    Additive and idempotent only.  Nothing here drops or recreates a table: ``pilot_episodes``
    holds allocations frozen before the work began and ``pilot_episode_closes`` and
    ``dream_relevance_adjudications`` hold nothing but the owner's own answers, and Lite has no
    downgrade path that could put any of it back.
    """
    conn.execute(
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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pilot_strata_scope "
        "ON pilot_strata (workspace_id, subject_user_id, project_id, decision_family)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pilot_episodes (
            id TEXT PRIMARY KEY,
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
            allocated_at TEXT NOT NULL,
            revealed_at TEXT,
            enrolled_before_execution INTEGER NOT NULL DEFAULT 1,
            thresholds_sha TEXT NOT NULL DEFAULT '',
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    # The uniqueness that makes the arm un-shoppable: the same work re-enrolled collides here and
    # the store returns the ORIGINAL row with reused=true instead of drawing a second arm.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pilot_episodes_episode "
        "ON pilot_episodes (workspace_id, subject_user_id, episode_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pilot_episodes_cell "
        "ON pilot_episodes (workspace_id, subject_user_id, project_id, decision_family, arm_id)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pilot_episodes_block ON pilot_episodes (stratum_id, block_ordinal)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pilot_episodes_allocated_at ON pilot_episodes (allocated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pilot_episodes_session ON pilot_episodes (workspace_id, session_id)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pilot_episode_closes (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            adjudicator_id TEXT NOT NULL DEFAULT '',
            adjudicator_verified INTEGER NOT NULL DEFAULT 0,
            adjudication_independent INTEGER NOT NULL DEFAULT 0,
            executed_arm TEXT NOT NULL,
            deviated INTEGER NOT NULL DEFAULT 0,
            deviation_reason TEXT NOT NULL DEFAULT '',
            rescue_level TEXT NOT NULL DEFAULT 'none',
            finished INTEGER NOT NULL DEFAULT 0,
            completion_basis TEXT NOT NULL DEFAULT 'unfinished',
            review_minutes INTEGER NOT NULL DEFAULT 0,
            review_verdict TEXT NOT NULL DEFAULT 'accepted_as_is',
            unfinished_reason TEXT NOT NULL DEFAULT '',
            late_close INTEGER NOT NULL DEFAULT 0,
            closed_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pilot_episode_closes_episode ON pilot_episode_closes (episode_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pilot_episode_closes_closed_at "
        "ON pilot_episode_closes (workspace_id, closed_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pilot_episode_observations (
            id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            principal TEXT NOT NULL DEFAULT '',
            producer_class TEXT NOT NULL DEFAULT 'agent_asserted',
            agent_notes TEXT NOT NULL DEFAULT '',
            agent_declared_steps_json TEXT NOT NULL DEFAULT '[]',
            agent_self_rated_difficulty INTEGER,
            observed_at TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pilot_episode_observations_episode "
        "ON pilot_episode_observations (episode_id, observed_at)"
    )

    # The fifth table's schema is written twice on purpose, and the duplication is policed rather
    # than tolerated: ``db.py`` owns Lite's bootstrap and ``dream_adjudication_store`` owns the
    # table's contract, and ``test_dream_adjudication.py::test_the_lite_ddl_is_the_same_text_in_db_py
    # _and_in_the_store`` compares the two CREATE blocks column for column.  Both are executed —
    # both are IF NOT EXISTS — so a drift cannot hide behind whichever one happened to run first.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dream_relevance_adjudications (
            id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            subject_user_id TEXT NOT NULL DEFAULT '',
            project_id TEXT,
            adjudicator_id TEXT NOT NULL DEFAULT '',
            adjudicator_verified INTEGER NOT NULL DEFAULT 0,
            relevance TEXT NOT NULL,
            rationale TEXT NOT NULL DEFAULT '',
            blind_claimed INTEGER NOT NULL DEFAULT 0,
            blind_verified INTEGER NOT NULL DEFAULT 0,
            delivery_useful TEXT NOT NULL DEFAULT '',
            counted_for_delivery INTEGER NOT NULL DEFAULT 0,
            adjudicated_at TEXT NOT NULL,
            supersedes_adjudication_id TEXT,
            schema_version TEXT NOT NULL DEFAULT 'v1'
        )
        """
    )
    ensure_dream_adjudication_tables(conn)


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
    # Ordered plan position. Nullable because SQLite cannot add a NOT NULL column
    # without a constant default, and because NULL is the meaningful value here:
    # it marks a goal that is not part of a plan.
    if not _column_exists(conn, "autonomy_goals", "step_index"):
        conn.execute("ALTER TABLE autonomy_goals ADD COLUMN step_index INTEGER")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_autonomy_goals_plan_step
            ON autonomy_goals (session_id, workspace_id, user_id, parent_goal_id, step_index)
        """
    )
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
    _ensure_directive_lease_schema(conn)
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


def _ensure_events_fts_schema(conn: sqlite3.Connection) -> None:
    """Create and synchronize the standalone FTS5 event index once."""
    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
            event_id UNINDEXED,
            title,
            payload,
            tags,
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TRIGGER IF NOT EXISTS events_fts_insert
        AFTER INSERT ON events BEGIN
            INSERT INTO events_fts(event_id, title, payload, tags)
            VALUES (new.id, new.title, new.payload, new.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS events_fts_delete
        AFTER DELETE ON events BEGIN
            DELETE FROM events_fts WHERE event_id = old.id;
        END;

        CREATE TRIGGER IF NOT EXISTS events_fts_update
        AFTER UPDATE OF title, payload, tags ON events BEGIN
            DELETE FROM events_fts WHERE event_id = old.id;
            INSERT INTO events_fts(event_id, title, payload, tags)
            VALUES (new.id, new.title, new.payload, new.tags);
        END;
        """
    )
    sentinel = conn.execute(
        "SELECT 1 FROM runtime_settings WHERE key = 'events_fts_v1_backfilled'"
    ).fetchone()
    if sentinel is not None:
        return
    conn.execute("DELETE FROM events_fts")
    conn.execute(
        """
        INSERT INTO events_fts(event_id, title, payload, tags)
        SELECT id, title, payload, tags FROM events
        """
    )
    conn.execute(
        """
        INSERT INTO runtime_settings(key, value, updated_at)
        VALUES('events_fts_v1_backfilled', 'true', ?)
        """,
        (datetime.now(tz=UTC).isoformat(),),
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
        _ensure_events_fts_schema(conn)
        _ensure_takeover_v3_schema(conn)
        _ensure_behavior_fidelity_schema(conn)
        _ensure_continuity_v04_schema(conn)
        _ensure_continuity_v05_schema(conn)
        _ensure_trusted_capture_schema(conn)
        _ensure_task_state_schema(conn)
        _ensure_charter_effects_schema(conn)
        _ensure_decision_policy_schema(conn)
        _ensure_dream_proposal_schema(conn)
        _ensure_pilot_enrolment_schema(conn)
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
