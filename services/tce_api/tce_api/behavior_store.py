from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


def _uuid_list(values: list[Any]) -> list[UUID]:
    output: list[UUID] = []
    for value in values[:40]:
        try:
            output.append(UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return output


def save_behavior_evidence(
    db: Session,
    *,
    consumer_id: str,
    workspace_id: str,
    subject_user_id: str,
    evidence: dict[str, Any],
    storage_gate: dict[str, Any],
) -> UUID:
    observation_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    valid_from = evidence.get("valid_from") or now
    source_event_ids = _uuid_list(list(evidence.get("source_event_ids") or []))
    supersedes = evidence.get("supersedes_observation_id")
    db.execute(
        text(
            """
            INSERT INTO decision_observations (
                id, consumer_id, workspace_id, subject_user_id, ts, situation_type, situation_summary,
                context_snapshot, user_response, response_reasoning, outcome,
                outcome_sentiment, source_event_ids, confidence, superseded_by,
                objective_text, constraints_json, available_choices_json, selected_choice,
                action_taken, correction_text, memory_class, evidence_source,
                lifecycle_status, valid_from, valid_until, contradicts_ids_json,
                confirmed_at, behavior_schema_version, redaction_applied,
                learning_eligible, storage_score, storage_decision
            ) VALUES (
                :id, :consumer_id, :workspace_id, :subject_user_id, :ts, :situation_type, :situation_summary,
                CAST(:context_snapshot AS jsonb), :user_response, :response_reasoning, :outcome,
                :outcome_sentiment, :source_event_ids, :confidence, NULL,
                :objective_text, CAST(:constraints_json AS jsonb), CAST(:available_choices_json AS jsonb),
                :selected_choice, :action_taken, :correction_text, :memory_class,
                :evidence_source, :lifecycle_status, :valid_from, :valid_until,
                CAST(:contradicts_ids_json AS jsonb), :confirmed_at, :behavior_schema_version,
                :redaction_applied, :learning_eligible, :storage_score, :storage_decision
            )
            """
        ),
        {
            "id": observation_id,
            "consumer_id": consumer_id,
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "ts": now,
            "situation_type": str(evidence.get("situation_type") or "routine_task"),
            "situation_summary": str(evidence.get("situation_summary") or ""),
            "context_snapshot": json.dumps(evidence.get("context_snapshot") or {}),
            "user_response": str(evidence.get("selected_choice") or ""),
            "response_reasoning": str(evidence.get("rationale") or "") or None,
            "outcome": str(evidence.get("outcome") or "") or None,
            "outcome_sentiment": evidence.get("outcome_sentiment"),
            "source_event_ids": source_event_ids,
            "confidence": float(evidence.get("confidence", 1.0) or 0.0),
            "objective_text": str(evidence.get("objective_text") or ""),
            "constraints_json": json.dumps(evidence.get("constraints") or {}),
            "available_choices_json": json.dumps(evidence.get("available_choices") or []),
            "selected_choice": str(evidence.get("selected_choice") or ""),
            "action_taken": str(evidence.get("action_taken") or ""),
            "correction_text": str(evidence.get("correction_text") or ""),
            "memory_class": str(evidence.get("memory_class") or "decision"),
            "evidence_source": str(evidence.get("evidence_source") or "explicit"),
            "lifecycle_status": str(evidence.get("lifecycle_status") or "active"),
            "valid_from": valid_from,
            "valid_until": evidence.get("valid_until"),
            "contradicts_ids_json": json.dumps(evidence.get("contradicts_observation_ids") or []),
            "confirmed_at": evidence.get("confirmed_at"),
            "behavior_schema_version": str(evidence.get("schema_version") or "v1"),
            "redaction_applied": bool(evidence.get("redaction_applied", False)),
            "learning_eligible": bool(storage_gate.get("learning_eligible", False)),
            "storage_score": float(storage_gate.get("score", 0.0) or 0.0),
            "storage_decision": str(storage_gate.get("decision") or "audit_only"),
        },
    )
    if supersedes:
        db.execute(
            text(
                """
                UPDATE decision_observations
                SET superseded_by = :new_id,
                    lifecycle_status = 'superseded'
                WHERE id = :old_id
                  AND workspace_id = :workspace_id
                  AND subject_user_id = :subject_user_id
                  AND superseded_by IS NULL
                """
            ),
            {
                "new_id": observation_id,
                "old_id": supersedes,
                "workspace_id": workspace_id,
                "subject_user_id": subject_user_id,
            },
        )
    db.commit()
    return observation_id


def load_behavior_evidence(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    limit: int = 5000,
    eligible_only: bool = True,
) -> list[dict[str, Any]]:
    eligibility = "AND learning_eligible = true" if eligible_only else ""
    rows = db.execute(
        text(
            f"""
            SELECT id, consumer_id, workspace_id, subject_user_id, ts, situation_type, situation_summary,
                   context_snapshot, user_response, response_reasoning, outcome,
                   outcome_sentiment, source_event_ids, confidence, superseded_by,
                   objective_text, constraints_json, available_choices_json, selected_choice,
                   action_taken, correction_text, memory_class, evidence_source,
                   lifecycle_status, valid_from, valid_until, contradicts_ids_json,
                   confirmed_at, behavior_schema_version, redaction_applied,
                   learning_eligible, storage_score, storage_decision
            FROM decision_observations
            WHERE workspace_id = :workspace_id
              AND subject_user_id = :subject_user_id
              {eligibility}
            ORDER BY ts DESC
            LIMIT :limit
            """
        ),
        {
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "limit": max(1, min(limit, 5000)),
        },
    ).mappings().all()
    evidence: list[dict[str, Any]] = []
    for row in reversed(rows):
        item = dict(row)
        item["constraints"] = item.pop("constraints_json", {}) or {}
        item["available_choices"] = item.pop("available_choices_json", []) or []
        item["contradicts_observation_ids"] = item.pop("contradicts_ids_json", []) or []
        item["learning_eligible"] = bool(item.get("learning_eligible"))
        evidence.append(item)
    return evidence


def save_fidelity_run(
    db: Session,
    *,
    consumer_id: str,
    workspace_id: str,
    subject_user_id: str,
    config: dict[str, Any],
    result: dict[str, Any],
) -> tuple[UUID, datetime]:
    run_id = uuid.uuid4()
    created_at = datetime.now(tz=UTC)
    metrics = dict(result.get("metrics") or {})
    db.execute(
        text(
            """
            INSERT INTO behavior_fidelity_runs (
                id, consumer_id, workspace_id, subject_user_id, created_at, status, config_json,
                metrics_json, gate_json, case_results_json, evidence_count,
                duration_ms, schema_version
            ) VALUES (
                :id, :consumer_id, :workspace_id, :subject_user_id, :created_at, :status,
                CAST(:config_json AS jsonb), CAST(:metrics_json AS jsonb),
                CAST(:gate_json AS jsonb), CAST(:case_results_json AS jsonb),
                :evidence_count, :duration_ms, 'v1'
            )
            """
        ),
        {
            "id": run_id,
            "consumer_id": consumer_id,
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "created_at": created_at,
            "status": str(result.get("status") or "completed"),
            "config_json": json.dumps(config),
            "metrics_json": json.dumps(metrics),
            "gate_json": json.dumps(result.get("gate") or {}),
            "case_results_json": json.dumps(result.get("case_results") or []),
            "evidence_count": int(metrics.get("eligible_evidence_count", 0) or 0),
            "duration_ms": int(result.get("duration_ms", 0) or 0),
        },
    )
    db.commit()
    return run_id, created_at


def list_fidelity_runs(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT id, status, config_json, metrics_json, gate_json, case_results_json,
                   created_at, duration_ms, schema_version
            FROM behavior_fidelity_runs
            WHERE workspace_id = :workspace_id
              AND subject_user_id = :subject_user_id
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "limit": max(1, min(limit, 100)),
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def latest_fidelity_gate(db: Session, *, workspace_id: str, subject_user_id: str) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT gate_json, metrics_json, created_at
            FROM behavior_fidelity_runs
            WHERE workspace_id = :workspace_id
              AND subject_user_id = :subject_user_id
              AND status = 'completed'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"workspace_id": workspace_id, "subject_user_id": subject_user_id},
    ).mappings().first()
    if not row:
        return {"passed": False, "reason": "no_completed_fidelity_run"}
    evaluated_at = row.get("created_at")
    return {
        **dict(row.get("gate_json") or {}),
        "metrics": dict(row.get("metrics_json") or {}),
        "evaluated_at": evaluated_at.isoformat() if isinstance(evaluated_at, datetime) else None,
    }
