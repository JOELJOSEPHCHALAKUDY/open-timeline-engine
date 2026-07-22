from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any


class BehaviorPilotConflict(ValueError):
    pass


class BehaviorPilotNotFound(LookupError):
    pass


class BehaviorPilotExpired(ValueError):
    pass


def _as_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


def _assignment_from_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["citations"] = _loads(item.pop("citations_json", "[]"), [])
    item["context_payload"] = _loads(item.pop("context_json", "{}"), {})
    item["request"] = _loads(item.pop("request_json", "{}"), {})
    item["assigned_at"] = _as_datetime(item["assigned_at"])
    item["expires_at"] = _as_datetime(item["expires_at"])
    item["redaction_applied"] = bool(item.get("redaction_applied"))
    return item


def create_or_get_assignment_lite(
    conn: sqlite3.Connection,
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
    assignment_id = str(uuid.uuid4())
    inserted = conn.execute(
        """
        INSERT OR IGNORE INTO behavior_projection_pilot_assignments (
            id, workspace_id, subject_user_id, owner_id, trial_key, request_digest,
            variant, situation_type, situation_summary, objective_text, request_json,
            context_json, context_sha256, source_revision, citations_json,
            injected_tokens, retrieval_latency_ms, redaction_applied, assigned_at,
            expires_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1')
        """,
        (
            assignment_id,
            workspace_id,
            subject_user_id,
            owner_id,
            request["trial_key"],
            request_digest,
            variant,
            request["situation_type"],
            request["situation_summary"],
            request["objective"],
            json.dumps(request, sort_keys=True),
            json.dumps(context_payload, sort_keys=True),
            context_sha256,
            source_revision,
            json.dumps(citations),
            max(0, injected_tokens),
            max(0, retrieval_latency_ms),
            1 if redaction_applied else 0,
            assigned_at.isoformat(),
            expires_at.isoformat(),
        ),
    )
    row = conn.execute(
        """
        SELECT id, workspace_id, subject_user_id, owner_id, trial_key, request_digest,
               variant, situation_type, situation_summary, objective_text, request_json,
               context_json, context_sha256, source_revision, citations_json,
               injected_tokens, retrieval_latency_ms, redaction_applied, assigned_at,
               expires_at, schema_version
        FROM behavior_projection_pilot_assignments
        WHERE workspace_id = ? AND subject_user_id = ? AND trial_key = ?
        LIMIT 1
        """,
        (workspace_id, subject_user_id, request["trial_key"]),
    ).fetchone()
    if row is None:
        conn.rollback()
        raise RuntimeError("behavior pilot assignment was not persisted")
    item = _assignment_from_row(row)
    if str(item["request_digest"]) != request_digest:
        conn.rollback()
        raise BehaviorPilotConflict("trial_key already exists with a different request")
    conn.commit()
    return item, bool(inserted.rowcount)


def load_assignment_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    assignment_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, workspace_id, subject_user_id, owner_id, trial_key, request_digest,
               variant, situation_type, situation_summary, objective_text, request_json,
               context_json, context_sha256, source_revision, citations_json,
               injected_tokens, retrieval_latency_ms, redaction_applied, assigned_at,
               expires_at, schema_version
        FROM behavior_projection_pilot_assignments
        WHERE id = ? AND workspace_id = ? AND subject_user_id = ?
        LIMIT 1
        """,
        (assignment_id, workspace_id, subject_user_id),
    ).fetchone()
    if row is None:
        raise BehaviorPilotNotFound("behavior pilot assignment was not found")
    return _assignment_from_row(row)


def record_outcome_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    reporter_id: str,
    assignment_id: str,
    outcome: dict[str, Any],
    outcome_digest: str,
    active_evidence_ids: set[str],
    redaction_applied: bool,
    reported_at: datetime,
) -> tuple[dict[str, Any], bool]:
    assignment = load_assignment_lite(
        conn,
        workspace_id=workspace_id,
        subject_user_id=subject_user_id,
        assignment_id=assignment_id,
    )
    if reported_at > assignment["expires_at"]:
        raise BehaviorPilotExpired("behavior pilot assignment has expired")
    used_ids = [str(item) for item in outcome.get("used_evidence_ids") or []]
    assigned_ids = {str(item) for item in assignment.get("citations") or []}
    stale_evidence_used = any(
        evidence_id not in active_evidence_ids or evidence_id not in assigned_ids
        for evidence_id in used_ids
    )
    outcome_id = str(uuid.uuid4())
    inserted = conn.execute(
        """
        INSERT OR IGNORE INTO behavior_projection_pilot_outcomes (
            id, assignment_id, workspace_id, subject_user_id, reporter_id,
            outcome_digest, agent_choice, top3_choices_json, actual_choice,
            agent_confidence, abstained, action_similarity, workflow_similarity,
            correction_required, outcome_regret, irrelevant_personalization,
            malicious_memory_activated, stale_evidence_used, used_evidence_ids_json,
            notes, redaction_applied, reported_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1')
        """,
        (
            outcome_id,
            assignment_id,
            workspace_id,
            subject_user_id,
            reporter_id,
            outcome_digest,
            outcome.get("agent_choice"),
            json.dumps(outcome.get("top3_choices") or []),
            outcome["actual_choice"],
            float(outcome.get("agent_confidence") or 0.0),
            1 if outcome.get("abstained") else 0,
            float(outcome.get("action_similarity") or 0.0),
            float(outcome.get("workflow_similarity") or 0.0),
            1 if outcome.get("correction_required") else 0,
            1 if outcome.get("outcome_regret") else 0,
            1 if outcome.get("irrelevant_personalization") else 0,
            1 if outcome.get("malicious_memory_activated") else 0,
            1 if stale_evidence_used else 0,
            json.dumps(used_ids),
            str(outcome.get("notes") or ""),
            1 if redaction_applied else 0,
            reported_at.isoformat(),
        ),
    )
    row = conn.execute(
        """
        SELECT id, assignment_id, outcome_digest, stale_evidence_used, reported_at
        FROM behavior_projection_pilot_outcomes
        WHERE assignment_id = ?
        LIMIT 1
        """,
        (assignment_id,),
    ).fetchone()
    if row is None:
        conn.rollback()
        raise RuntimeError("behavior pilot outcome was not persisted")
    item = dict(row)
    if str(item["outcome_digest"]) != outcome_digest:
        conn.rollback()
        raise BehaviorPilotConflict("assignment already has a different outcome")
    conn.commit()
    item["stale_evidence_used"] = bool(item.get("stale_evidence_used"))
    item["reported_at"] = _as_datetime(item["reported_at"])
    return item, bool(inserted.rowcount)


def list_pilot_rows_lite(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    rows = conn.execute(
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
        WHERE a.workspace_id = ? AND a.subject_user_id = ?
        ORDER BY a.assigned_at ASC
        LIMIT ?
        """,
        (workspace_id, subject_user_id, max(1, min(limit, 10000))),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["assigned_at"] = _as_datetime(item["assigned_at"])
        item["expires_at"] = _as_datetime(item["expires_at"])
        item["top3_choices"] = _loads(item.pop("top3_choices_json", "[]"), [])
        if item.get("reported_at"):
            item["reported_at"] = _as_datetime(item["reported_at"])
        for key in (
            "abstained",
            "correction_required",
            "outcome_regret",
            "irrelevant_personalization",
            "malicious_memory_activated",
            "stale_evidence_used",
        ):
            item[key] = bool(item.get(key))
        output.append(item)
    return output
