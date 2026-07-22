from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


class BehaviorPilotConflict(ValueError):
    pass


class BehaviorPilotNotFound(LookupError):
    pass


class BehaviorPilotExpired(ValueError):
    pass


def _assignment_from_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["citations"] = list(item.pop("citations_json", []) or [])
    item["context_payload"] = dict(item.pop("context_json", {}) or {})
    item["request"] = dict(item.pop("request_json", {}) or {})
    return item


def create_or_get_assignment(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    owner_id: str,
    request: dict[str, Any],
    request_digest: str,
    variant: str,
    context_payload: dict[str, Any],
    context_sha256: str,
    source_revision: str,
    citations: list[str],
    injected_tokens: int,
    retrieval_latency_ms: int,
    redaction_applied: bool,
    assigned_at: datetime,
    expires_at: datetime,
) -> tuple[dict[str, Any], bool]:
    assignment_id = uuid.uuid4()
    inserted = db.execute(
        text(
            """
            INSERT INTO behavior_projection_pilot_assignments (
                id, workspace_id, subject_user_id, owner_id, trial_key, request_digest,
                variant, situation_type, situation_summary, objective_text, request_json,
                context_json, context_sha256, source_revision, citations_json,
                injected_tokens, retrieval_latency_ms, redaction_applied, assigned_at,
                expires_at, schema_version
            ) VALUES (
                :id, :workspace_id, :subject_user_id, :owner_id, :trial_key, :request_digest,
                :variant, :situation_type, :situation_summary, :objective_text,
                CAST(:request_json AS jsonb), CAST(:context_json AS jsonb), :context_sha256,
                :source_revision, CAST(:citations_json AS jsonb), :injected_tokens,
                :retrieval_latency_ms, :redaction_applied, :assigned_at, :expires_at, 'v1'
            )
            ON CONFLICT (workspace_id, subject_user_id, trial_key) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": assignment_id,
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "owner_id": owner_id,
            "trial_key": request["trial_key"],
            "request_digest": request_digest,
            "variant": variant,
            "situation_type": request["situation_type"],
            "situation_summary": request["situation_summary"],
            "objective_text": request["objective"],
            "request_json": json.dumps(request),
            "context_json": json.dumps(context_payload),
            "context_sha256": context_sha256,
            "source_revision": source_revision,
            "citations_json": json.dumps(citations),
            "injected_tokens": max(0, injected_tokens),
            "retrieval_latency_ms": max(0, retrieval_latency_ms),
            "redaction_applied": redaction_applied,
            "assigned_at": assigned_at,
            "expires_at": expires_at,
        },
    ).first()
    row = db.execute(
        text(
            """
            SELECT id, workspace_id, subject_user_id, owner_id, trial_key, request_digest,
                   variant, situation_type, situation_summary, objective_text, request_json,
                   context_json, context_sha256, source_revision, citations_json,
                   injected_tokens, retrieval_latency_ms, redaction_applied, assigned_at,
                   expires_at, schema_version
            FROM behavior_projection_pilot_assignments
            WHERE workspace_id = :workspace_id
              AND subject_user_id = :subject_user_id
              AND trial_key = :trial_key
            LIMIT 1
            """
        ),
        {
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "trial_key": request["trial_key"],
        },
    ).mappings().first()
    if row is None:
        db.rollback()
        raise RuntimeError("behavior pilot assignment was not persisted")
    item = _assignment_from_row(row)
    if str(item["request_digest"]) != request_digest:
        db.rollback()
        raise BehaviorPilotConflict("trial_key already exists with a different request")
    db.commit()
    return item, inserted is not None


def load_assignment(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    assignment_id: uuid.UUID,
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT id, workspace_id, subject_user_id, owner_id, trial_key, request_digest,
                   variant, situation_type, situation_summary, objective_text, request_json,
                   context_json, context_sha256, source_revision, citations_json,
                   injected_tokens, retrieval_latency_ms, redaction_applied, assigned_at,
                   expires_at, schema_version
            FROM behavior_projection_pilot_assignments
            WHERE id = :assignment_id
              AND workspace_id = :workspace_id
              AND subject_user_id = :subject_user_id
            LIMIT 1
            """
        ),
        {
            "assignment_id": assignment_id,
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
        },
    ).mappings().first()
    if row is None:
        raise BehaviorPilotNotFound("behavior pilot assignment was not found")
    return _assignment_from_row(row)


def record_outcome(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    reporter_id: str,
    assignment_id: uuid.UUID,
    outcome: dict[str, Any],
    outcome_digest: str,
    active_evidence_ids: set[str],
    redaction_applied: bool,
    reported_at: datetime,
) -> tuple[dict[str, Any], bool]:
    assignment = load_assignment(
        db,
        workspace_id=workspace_id,
        subject_user_id=subject_user_id,
        assignment_id=assignment_id,
    )
    expires_at = assignment["expires_at"]
    if isinstance(expires_at, datetime) and reported_at > expires_at.astimezone(UTC):
        raise BehaviorPilotExpired("behavior pilot assignment has expired")
    used_ids = [str(item) for item in outcome.get("used_evidence_ids") or []]
    assigned_ids = {str(item) for item in assignment.get("citations") or []}
    stale_evidence_used = any(
        evidence_id not in active_evidence_ids or evidence_id not in assigned_ids
        for evidence_id in used_ids
    )
    outcome_id = uuid.uuid4()
    inserted = db.execute(
        text(
            """
            INSERT INTO behavior_projection_pilot_outcomes (
                id, assignment_id, workspace_id, subject_user_id, reporter_id,
                outcome_digest, agent_choice, top3_choices_json, actual_choice,
                agent_confidence, abstained, action_similarity, workflow_similarity,
                correction_required, outcome_regret, irrelevant_personalization,
                malicious_memory_activated, stale_evidence_used, used_evidence_ids_json,
                notes, redaction_applied, reported_at, schema_version
            ) VALUES (
                :id, :assignment_id, :workspace_id, :subject_user_id, :reporter_id,
                :outcome_digest, :agent_choice, CAST(:top3_choices_json AS jsonb),
                :actual_choice, :agent_confidence, :abstained, :action_similarity,
                :workflow_similarity, :correction_required, :outcome_regret,
                :irrelevant_personalization, :malicious_memory_activated,
                :stale_evidence_used, CAST(:used_evidence_ids_json AS jsonb), :notes,
                :redaction_applied, :reported_at, 'v1'
            )
            ON CONFLICT (assignment_id) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": outcome_id,
            "assignment_id": assignment_id,
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "reporter_id": reporter_id,
            "outcome_digest": outcome_digest,
            "agent_choice": outcome.get("agent_choice"),
            "top3_choices_json": json.dumps(outcome.get("top3_choices") or []),
            "actual_choice": outcome["actual_choice"],
            "agent_confidence": float(outcome.get("agent_confidence") or 0.0),
            "abstained": bool(outcome.get("abstained")),
            "action_similarity": float(outcome.get("action_similarity") or 0.0),
            "workflow_similarity": float(outcome.get("workflow_similarity") or 0.0),
            "correction_required": bool(outcome.get("correction_required")),
            "outcome_regret": bool(outcome.get("outcome_regret")),
            "irrelevant_personalization": bool(outcome.get("irrelevant_personalization")),
            "malicious_memory_activated": bool(outcome.get("malicious_memory_activated")),
            "stale_evidence_used": stale_evidence_used,
            "used_evidence_ids_json": json.dumps(used_ids),
            "notes": str(outcome.get("notes") or ""),
            "redaction_applied": redaction_applied,
            "reported_at": reported_at,
        },
    ).first()
    row = db.execute(
        text(
            """
            SELECT id, assignment_id, outcome_digest, stale_evidence_used, reported_at
            FROM behavior_projection_pilot_outcomes
            WHERE assignment_id = :assignment_id
            LIMIT 1
            """
        ),
        {"assignment_id": assignment_id},
    ).mappings().first()
    if row is None:
        db.rollback()
        raise RuntimeError("behavior pilot outcome was not persisted")
    item = dict(row)
    if str(item["outcome_digest"]) != outcome_digest:
        db.rollback()
        raise BehaviorPilotConflict("assignment already has a different outcome")
    db.commit()
    return item, inserted is not None


def list_pilot_rows(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT a.id AS assignment_id, a.variant, a.assigned_at, a.expires_at,
                   a.injected_tokens, a.retrieval_latency_ms,
                   o.id AS outcome_id, o.agent_choice, o.top3_choices_json,
                   o.actual_choice, o.agent_confidence, o.abstained,
                   o.action_similarity, o.workflow_similarity, o.correction_required,
                   o.outcome_regret, o.irrelevant_personalization,
                   o.malicious_memory_activated, o.stale_evidence_used, o.reported_at
            FROM behavior_projection_pilot_assignments a
            LEFT JOIN behavior_projection_pilot_outcomes o ON o.assignment_id = a.id
            WHERE a.workspace_id = :workspace_id
              AND a.subject_user_id = :subject_user_id
            ORDER BY a.assigned_at ASC
            LIMIT :limit
            """
        ),
        {
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "limit": max(1, min(limit, 10000)),
        },
    ).mappings().all()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["top3_choices"] = list(item.pop("top3_choices_json", []) or [])
        output.append(item)
    return output
