"""Trusted-capture persistence: input receipts, decision opportunities, human resolutions.

Raw ``text()`` SQL against the tables added in alembic ``20260909_0036``. Every function takes
the caller's ``Session`` and never commits (the request handler owns the transaction), except
where noted. Everything human-owned is scoped by ``(workspace_id, subject_user_id)``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_shared.decision_capture import HumanResolution

_RECEIPT_COLUMNS = """
    id, workspace_id, owner_id, subject_user_id, host_session_id, sequence, prompt_id,
    delivery_key, content_sha256, origin_kind, capture_principal, host_client, event_id,
    project_id, observed_at, ingested_at, original_char_count, content_truncated,
    redaction_applied_json, spool_depth, spool_failures, gap_since, queue_state,
    extraction_state, extraction_lease_until, extraction_attempts, extraction_last_error,
    extraction_version_done, next_extraction_at, schema_version
"""

_OPPORTUNITY_COLUMNS = """
    id, workspace_id, subject_user_id, owner_id, session_id, turn, objective_hash, task_id,
    project_id, decision_family, situation_type, question_text, alternatives_json,
    pre_answer_snapshot_json, evidence_cutoff_at, evidence_revision, advice_exposure_json,
    shadow_prediction_id, source_event_id, status, relayed_answer, relayed_at, resolved_at,
    expires_at, created_at, frozen_at, schema_version
"""


def _as_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _row_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    for key in ("alternatives_json", "pre_answer_snapshot_json", "advice_exposure_json", "redaction_applied_json"):
        if key in item and isinstance(item[key], str):
            try:
                item[key] = json.loads(item[key])
            except (TypeError, ValueError):
                pass
    return item


def get_receipt_by_delivery_key(db: Session, *, workspace_id: str, owner_id: str, delivery_key: str) -> dict[str, Any] | None:
    row = db.execute(
        text(
            f"""
            SELECT {_RECEIPT_COLUMNS}
            FROM trusted_input_receipts
            WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND delivery_key = :delivery_key
            LIMIT 1
            """
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id, "delivery_key": delivery_key},
    ).mappings().first()
    return _row_dict(row) if row is not None else None


def get_receipt(db: Session, *, receipt_id: UUID, workspace_id: str, subject_user_id: str) -> dict[str, Any] | None:
    row = db.execute(
        text(
            f"""
            SELECT {_RECEIPT_COLUMNS}
            FROM trusted_input_receipts
            WHERE id = :id AND workspace_id = :workspace_id AND subject_user_id = :subject_user_id
            LIMIT 1
            """
        ),
        {"id": receipt_id, "workspace_id": workspace_id, "subject_user_id": subject_user_id},
    ).mappings().first()
    return _row_dict(row) if row is not None else None


def insert_receipt(
    db: Session,
    *,
    receipt_id: UUID,
    workspace_id: str,
    owner_id: str,
    subject_user_id: str,
    host_session_id: str,
    sequence: int | None,
    prompt_id: str | None,
    delivery_key: str,
    content_sha256: str,
    origin_kind: str,
    capture_principal: str,
    host_client: str,
    event_id: UUID | None,
    project_id: str | None,
    observed_at: datetime,
    ingested_at: datetime,
    original_char_count: int,
    content_truncated: bool,
    redaction_applied: list[str],
    spool_depth: int,
    spool_failures: int,
    gap_since: datetime | None,
    queue_state: str,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO trusted_input_receipts (
                id, workspace_id, owner_id, subject_user_id, host_session_id, sequence, prompt_id,
                delivery_key, content_sha256, origin_kind, capture_principal, host_client, event_id,
                project_id, observed_at, ingested_at, original_char_count, content_truncated,
                redaction_applied_json, spool_depth, spool_failures, gap_since, queue_state,
                extraction_state, extraction_attempts, schema_version
            ) VALUES (
                :id, :workspace_id, :owner_id, :subject_user_id, :host_session_id, :sequence, :prompt_id,
                :delivery_key, :content_sha256, :origin_kind, :capture_principal, :host_client, :event_id,
                :project_id, :observed_at, :ingested_at, :original_char_count, :content_truncated,
                CAST(:redaction_applied_json AS jsonb), :spool_depth, :spool_failures, :gap_since, :queue_state,
                'pending', 0, 'v1'
            )
            """
        ),
        {
            "id": receipt_id,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "subject_user_id": subject_user_id,
            "host_session_id": host_session_id,
            "sequence": sequence,
            "prompt_id": prompt_id,
            "delivery_key": delivery_key,
            "content_sha256": content_sha256,
            "origin_kind": origin_kind,
            "capture_principal": capture_principal,
            "host_client": host_client,
            "event_id": event_id,
            "project_id": project_id,
            "observed_at": observed_at,
            "ingested_at": ingested_at,
            "original_char_count": int(original_char_count),
            "content_truncated": bool(content_truncated),
            "redaction_applied_json": json.dumps(sorted(set(redaction_applied))),
            "spool_depth": int(spool_depth),
            "spool_failures": int(spool_failures),
            "gap_since": gap_since,
            "queue_state": queue_state,
        },
    )


def set_receipt_queue_state(db: Session, receipt_id: UUID, queue_state: str) -> None:
    db.execute(
        text("UPDATE trusted_input_receipts SET queue_state = :queue_state WHERE id = :id"),
        {"id": receipt_id, "queue_state": queue_state},
    )


def latest_receipt_for_subject(db: Session, *, workspace_id: str, subject_user_id: str) -> dict[str, Any] | None:
    row = db.execute(
        text(
            f"""
            SELECT {_RECEIPT_COLUMNS}
            FROM trusted_input_receipts
            WHERE workspace_id = :workspace_id AND subject_user_id = :subject_user_id
            ORDER BY ingested_at DESC
            LIMIT 1
            """
        ),
        {"workspace_id": workspace_id, "subject_user_id": subject_user_id},
    ).mappings().first()
    return _row_dict(row) if row is not None else None


def capture_delivery_state(db: Session, *, workspace_id: str, subject_user_id: str, stale_seconds: int, now: datetime) -> str:
    """Health of the trusted human-input channel for this subject, derived from the latest receipt."""
    row = latest_receipt_for_subject(db, workspace_id=workspace_id, subject_user_id=subject_user_id)
    if row is None:
        return "unavailable"
    if row.get("gap_since") is not None:
        return "gap"
    if int(row.get("spool_failures") or 0) > 0:
        return "degraded"
    if int(row.get("spool_depth") or 0) > 0:
        return "spooling"
    ingested_at = _as_utc(row.get("ingested_at"))
    if ingested_at is None or (now - ingested_at) > timedelta(seconds=max(0, int(stale_seconds))):
        return "unavailable"
    return "healthy"


def open_opportunity_for_session(
    db: Session, *, workspace_id: str, subject_user_id: str, session_id: str, decision_family: str | None = None
) -> dict[str, Any] | None:
    """Newest still-open opportunity for the session, optionally narrowed to one decision family."""
    family_clause = "AND decision_family = :decision_family" if decision_family else ""
    params: dict[str, Any] = {"workspace_id": workspace_id, "subject_user_id": subject_user_id, "session_id": session_id}
    if decision_family:
        params["decision_family"] = decision_family
    row = db.execute(
        text(
            f"""
            SELECT {_OPPORTUNITY_COLUMNS}
            FROM decision_opportunities
            WHERE workspace_id = :workspace_id AND subject_user_id = :subject_user_id
              AND session_id = :session_id AND status = 'open' {family_clause}
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        params,
    ).mappings().first()
    return _row_dict(row) if row is not None else None


def get_opportunity(db: Session, *, opportunity_id: UUID, workspace_id: str, subject_user_id: str) -> dict[str, Any] | None:
    row = db.execute(
        text(
            f"""
            SELECT {_OPPORTUNITY_COLUMNS}
            FROM decision_opportunities
            WHERE id = :id AND workspace_id = :workspace_id AND subject_user_id = :subject_user_id
            LIMIT 1
            """
        ),
        {"id": opportunity_id, "workspace_id": workspace_id, "subject_user_id": subject_user_id},
    ).mappings().first()
    return _row_dict(row) if row is not None else None


def open_opportunities_for_subject(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    project_id: str | None,
    since: datetime,
) -> list[dict[str, Any]]:
    """Subject-scoped open opportunities; narrowed to the project only when both sides carry one."""
    project_clause = ""
    params: dict[str, Any] = {"workspace_id": workspace_id, "subject_user_id": subject_user_id, "since": since}
    if project_id:
        project_clause = "AND (project_id IS NULL OR project_id = :project_id)"
        params["project_id"] = project_id
    rows = db.execute(
        text(
            f"""
            SELECT {_OPPORTUNITY_COLUMNS}
            FROM decision_opportunities
            WHERE workspace_id = :workspace_id AND subject_user_id = :subject_user_id
              AND status = 'open' AND created_at >= :since
              {project_clause}
            ORDER BY created_at DESC
            """
        ),
        params,
    ).mappings().all()
    return [_row_dict(row) for row in rows]


def insert_opportunity(
    db: Session,
    *,
    opportunity_id: UUID,
    workspace_id: str,
    subject_user_id: str,
    owner_id: str,
    session_id: str,
    turn: int | None,
    objective_hash: str | None,
    task_id: str | None,
    project_id: str | None,
    decision_family: str,
    situation_type: str,
    question_text: str,
    alternatives: list[str],
    pre_answer_snapshot: dict[str, Any],
    evidence_cutoff_at: datetime | None,
    evidence_revision: str | None,
    advice_exposure: dict[str, Any],
    shadow_prediction_id: UUID | None,
    source_event_id: UUID | None,
    expires_at: datetime | None,
    created_at: datetime,
    frozen_at: datetime,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO decision_opportunities (
                id, workspace_id, subject_user_id, owner_id, session_id, turn, objective_hash, task_id,
                project_id, decision_family, situation_type, question_text, alternatives_json,
                pre_answer_snapshot_json, evidence_cutoff_at, evidence_revision, advice_exposure_json,
                shadow_prediction_id, source_event_id, status, expires_at, created_at, frozen_at, schema_version
            ) VALUES (
                :id, :workspace_id, :subject_user_id, :owner_id, :session_id, :turn, :objective_hash, :task_id,
                :project_id, :decision_family, :situation_type, :question_text, CAST(:alternatives_json AS jsonb),
                CAST(:pre_answer_snapshot_json AS jsonb), :evidence_cutoff_at, :evidence_revision,
                CAST(:advice_exposure_json AS jsonb), :shadow_prediction_id, :source_event_id, 'open',
                :expires_at, :created_at, :frozen_at, 'v1'
            )
            """
        ),
        {
            "id": opportunity_id,
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "owner_id": owner_id,
            "session_id": session_id,
            "turn": turn,
            "objective_hash": objective_hash,
            "task_id": task_id,
            "project_id": project_id,
            "decision_family": decision_family,
            "situation_type": situation_type,
            "question_text": question_text[:4000],
            "alternatives_json": json.dumps([str(item) for item in alternatives]),
            "pre_answer_snapshot_json": json.dumps(pre_answer_snapshot, default=str),
            "evidence_cutoff_at": evidence_cutoff_at,
            "evidence_revision": evidence_revision,
            "advice_exposure_json": json.dumps(advice_exposure, default=str),
            "shadow_prediction_id": shadow_prediction_id,
            "source_event_id": source_event_id,
            "expires_at": expires_at,
            "created_at": created_at,
            "frozen_at": frozen_at,
        },
    )


def mark_opportunity_relayed(db: Session, opportunity_id: UUID, relayed_answer: str, relayed_at: datetime) -> None:
    """Record what the executor relayed; this is NOT a resolution (executor text is not a label)."""
    db.execute(
        text(
            """
            UPDATE decision_opportunities
            SET relayed_answer = :relayed_answer, relayed_at = :relayed_at
            WHERE id = :id AND status = 'open'
            """
        ),
        {"id": opportunity_id, "relayed_answer": relayed_answer[:500], "relayed_at": relayed_at},
    )


def resolve_opportunity(db: Session, *, opportunity_id: UUID, resolved_at: datetime) -> bool:
    result = db.execute(
        text("UPDATE decision_opportunities SET status = 'resolved', resolved_at = :resolved_at WHERE id = :id AND status = 'open'"),
        {"id": opportunity_id, "resolved_at": resolved_at},
    )
    return int(getattr(result, "rowcount", 0) or 0) == 1


def close_opportunities(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    session_id: str,
    status: str,
    resolved_at: datetime,
) -> list[UUID]:
    """Close every open opportunity for the session as abandoned/expired and park its shadow rows."""
    if status not in {"abandoned", "expired"}:
        raise ValueError(f"invalid close status: {status}")
    rows = db.execute(
        text(
            """
            UPDATE decision_opportunities
            SET status = :status, resolved_at = :resolved_at
            WHERE workspace_id = :workspace_id AND subject_user_id = :subject_user_id
              AND session_id = :session_id AND status = 'open'
            RETURNING id, shadow_prediction_id
            """
        ),
        {
            "status": status,
            "resolved_at": resolved_at,
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "session_id": session_id,
        },
    ).mappings().all()
    closed: list[UUID] = []
    shadow_state = "abandoned" if status == "abandoned" else "unanswered"
    for row in rows:
        closed.append(UUID(str(row["id"])))
        shadow_id = row.get("shadow_prediction_id")
        if shadow_id is not None:
            db.execute(
                text(
                    """
                    UPDATE behavior_shadow_predictions
                    SET resolution_state = :resolution_state, resolution_source = 'sweep'
                    WHERE id = :id AND resolution_state = 'pending'
                    """
                ),
                {"id": shadow_id, "resolution_state": shadow_state},
            )
    return closed


def insert_human_resolution(
    db: Session,
    *,
    resolution: HumanResolution,
    workspace_id: str,
    subject_user_id: str,
    receipt_id: UUID | None,
    source_event_id: UUID | None,
    candidate_id: UUID | None,
    observation_id: UUID | None,
) -> UUID:
    resolution_id = uuid.uuid4()
    db.execute(
        text(
            """
            INSERT INTO human_resolutions (
                id, opportunity_id, workspace_id, subject_user_id, receipt_id, source_event_id, candidate_id,
                selected_choice, correction_text, stated_rationale, resolution_source, human_source_ref,
                observation_id, supersedes_resolution_id, resolved_at, created_at, schema_version
            ) VALUES (
                :id, :opportunity_id, :workspace_id, :subject_user_id, :receipt_id, :source_event_id, :candidate_id,
                :selected_choice, :correction_text, :stated_rationale, :resolution_source, :human_source_ref,
                :observation_id, :supersedes_resolution_id, :resolved_at, :created_at, 'v1'
            )
            """
        ),
        {
            "id": resolution_id,
            "opportunity_id": UUID(str(resolution.opportunity_id)),
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "receipt_id": receipt_id,
            "source_event_id": source_event_id,
            "candidate_id": candidate_id,
            "selected_choice": resolution.selected_choice[:500],
            "correction_text": resolution.correction_text[:2000],
            "stated_rationale": resolution.stated_rationale,
            "resolution_source": resolution.resolution_source,
            "human_source_ref": resolution.human_source_ref,
            "observation_id": observation_id,
            "supersedes_resolution_id": UUID(str(resolution.supersedes_resolution_id)) if resolution.supersedes_resolution_id else None,
            "resolved_at": resolution.resolved_at,
            "created_at": datetime.now(tz=UTC),
        },
    )
    return resolution_id


def validate_source_event_provenance(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    event_ids: list[UUID],
) -> dict[UUID, dict[str, Any]]:
    """Server-side ownership/origin check for caller-supplied source_event_ids.

    ``owned`` requires the event to live in the caller's workspace AND (when a receipt exists)
    the receipt subject to be the caller's behavior subject. ``origin_kind`` is only ever taken
    from a trusted receipt row whose event context names a ``host:`` capture principal; an
    event without such a receipt has origin ``None`` regardless of what its context claims.
    """
    output: dict[UUID, dict[str, Any]] = {}
    for event_id in event_ids:
        row = db.execute(
            text(
                """
                SELECT e.id AS event_id,
                       e.context->>'_tce_workspace' AS event_workspace,
                       e.context->>'capture_principal' AS capture_principal,
                       r.id AS receipt_id,
                       r.origin_kind AS origin_kind,
                       r.subject_user_id AS receipt_subject,
                       r.workspace_id AS receipt_workspace
                FROM events e
                LEFT JOIN trusted_input_receipts r ON r.event_id = e.id
                WHERE e.id = :event_id
                LIMIT 1
                """
            ),
            {"event_id": event_id},
        ).mappings().first()
        if row is None:
            output[event_id] = {"owned": False, "origin_kind": None, "receipt_id": None, "capture_principal": None}
            continue
        owned = str(row.get("event_workspace") or "") == workspace_id
        receipt_id = row.get("receipt_id")
        if receipt_id is not None:
            owned = owned and str(row.get("receipt_subject") or "") == subject_user_id and str(row.get("receipt_workspace") or "") == workspace_id
        principal = row.get("capture_principal")
        trusted = receipt_id is not None and isinstance(principal, str) and principal.startswith("host:")
        output[event_id] = {
            "owned": bool(owned),
            "origin_kind": str(row.get("origin_kind")) if trusted and row.get("origin_kind") else None,
            "receipt_id": UUID(str(receipt_id)) if (receipt_id is not None and trusted) else None,
            "capture_principal": str(principal) if trusted else None,
        }
    return output
