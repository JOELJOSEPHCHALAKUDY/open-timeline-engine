from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tce_shared.autonomy_context import SUMMARY_VERSION, summarize_event_record
from tce_shared.continuity import progress_patch, summarize_attempts
from tce_shared.handoff import normalize_objective_text
from tce_shared.redaction import redact_payload, redact_text

from .models import ContinuityResumeAttempt, Event, HandoffOutbox, HandoffRecord


def _safe_change_summary(raw: Any) -> tuple[dict[str, dict[str, Any]], bool]:
    if not isinstance(raw, dict):
        return {}, False
    output: dict[str, dict[str, Any]] = {}
    redacted_any = False
    for path, value in list(raw.items())[:40]:
        safe_path, path_redacted = redact_text(str(path or "").strip()[:240])
        if not safe_path:
            continue
        item = value if isinstance(value, dict) else {}
        intent, intent_redacted = redact_text(str(item.get("intent") or "").strip()[:160])
        try:
            added = max(0, int(item.get("added", item.get("added_lines", 0)) or 0))
        except (TypeError, ValueError):
            added = 0
        try:
            removed = max(0, int(item.get("removed", item.get("removed_lines", 0)) or 0))
        except (TypeError, ValueError):
            removed = 0
        output[safe_path] = {"added": added, "removed": removed, "intent": intent}
        redacted_any = redacted_any or bool(path_redacted) or bool(intent_redacted)
    return output, redacted_any


def enqueue_handoff(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    behavior_subject_id: str,
    session_id: str,
    directive_id: uuid.UUID | None,
    completion_key: str,
    terminal_state: str,
    milestone: dict[str, Any],
    source: str,
    redaction_applied: bool,
    now: datetime | None = None,
) -> HandoffOutbox:
    created_at = now or datetime.now(tz=UTC)
    event_id = uuid.uuid4()
    handoff_record_id = uuid.uuid4()
    outbox_id = uuid.uuid4()
    statement = pg_insert(HandoffOutbox).values(
        id=outbox_id,
        workspace_id=workspace_id,
        owner_id=owner_id,
        behavior_subject_id=behavior_subject_id,
        session_id=session_id,
        directive_id=directive_id,
        completion_key=str(completion_key)[:240],
        terminal_state=terminal_state,
        milestone_json=milestone,
        source=str(source or "native")[:64],
        status="pending",
        attempts=0,
        next_attempt_at=created_at,
        event_id=event_id,
        handoff_record_id=handoff_record_id,
        redaction_applied=redaction_applied,
        created_at=created_at,
        updated_at=created_at,
    ).on_conflict_do_nothing(index_elements=["workspace_id", "owner_id", "completion_key"])
    db.execute(statement)
    row = db.execute(
        select(HandoffOutbox).where(
            HandoffOutbox.workspace_id == workspace_id,
            HandoffOutbox.owner_id == owner_id,
            HandoffOutbox.completion_key == str(completion_key)[:240],
        )
    ).scalar_one()
    return row


def deliver_handoff(db: Session, *, outbox_id: uuid.UUID, retention_days: int) -> HandoffOutbox:
    row = db.execute(
        select(HandoffOutbox).where(HandoffOutbox.id == outbox_id).with_for_update()
    ).scalar_one()
    if row.status == "delivered":
        return row
    now = datetime.now(tz=UTC)
    milestone: dict[str, Any] = dict(row.milestone_json or {})
    payload_root_value = milestone.get("payload")
    payload_root: dict[str, Any] = payload_root_value if isinstance(payload_root_value, dict) else {}
    outcome_value = milestone.get("outcome")
    outcome: dict[str, Any] = outcome_value if isinstance(outcome_value, dict) else {}
    files = [str(value)[:240] for value in list(payload_root.get("files") or [])[:40]]
    payload = {
        "directive_id": str(row.directive_id) if row.directive_id else None,
        "files": files,
        "decision": str(milestone.get("decision") or "")[:500],
        "outcome": outcome,
        "git": dict(milestone.get("git") or {}),
        "anchors": list(milestone.get("anchors") or [])[:40],
        "milestone_schema": "v1",
        "workspace_id": row.workspace_id,
        "user_id": row.owner_id,
    }
    redacted_payload, payload_redacted = redact_payload(payload)
    payload = redacted_payload if isinstance(redacted_payload, dict) else {}
    title, title_redacted = redact_text(str(milestone.get("title") or "Completion captured")[:160])
    context = {
        "session_id": row.session_id,
        "_tce_workspace": row.workspace_id,
        "_tce_owner": row.owner_id,
        "_tce_behavior_subject": row.behavior_subject_id,
    }
    event_outcome = {
        "success": row.terminal_state == "succeeded",
        "metrics": {"state": row.terminal_state, "outbox_id": str(row.id)},
        "followups": [str(outcome.get("next_step") or "")[:300]],
    }
    summary_l0, summary_l1 = summarize_event_record(
        title=title,
        task_type="completion_handoff",
        domain="continuity",
        payload=payload,
        decision=None,
        outcome=event_outcome,
    )
    canonical = json.dumps(
        {"title": title, "payload": payload, "context": context, "ts": row.created_at.isoformat()},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    if db.get(Event, row.event_id) is None:
        db.add(
            Event(
                id=row.event_id,
                ts=row.created_at,
                actor=row.owner_id,
                source="tce-completion-outbox",
                domain="continuity",
                task_type="completion_handoff",
                event_type="TASK_DONE" if row.terminal_state == "succeeded" else "TASK_DECISION",
                title=title,
                summary_l0=summary_l0,
                summary_l1_json=summary_l1,
                summary_version=SUMMARY_VERSION,
                summary_updated_at=row.created_at,
                payload=payload,
                context=context,
                inputs={},
                steps=[],
                decision=None,
                outcome=event_outcome,
                style=None,
                links=None,
                tags=["handoff", "completion", "milestone-v1"],
                sensitivity=1,
                redaction_hints=[],
                source_id=str(row.id),
                source_seq=1,
                vector_clock={},
                idempotency_key=f"completion:{row.completion_key}",
                authority_level="high",
                hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                schema_version=1,
            )
        )
    decision_text = str(milestone.get("decision") or "")[:500]
    next_step = str(outcome.get("next_step") or "")[:300]
    objective_text, objective_redacted = normalize_objective_text(
        title=title,
        decision=decision_text,
        next_step=next_step,
    )
    change_summary, summary_redacted = _safe_change_summary(
        milestone.get("change_summary_json") or milestone.get("change_summary")
    )
    if db.get(HandoffRecord, row.handoff_record_id) is None:
        db.add(
            HandoffRecord(
                id=row.handoff_record_id,
                workspace_id=row.workspace_id,
                owner_id=row.owner_id,
                session_id=row.session_id,
                directive_id=row.directive_id,
                ts=row.created_at,
                title=title,
                decision=decision_text,
                next_step=next_step,
                status=row.terminal_state,
                files_json=files,
                anchors_json=list(milestone.get("anchors") or [])[:40],
                git_json=dict(milestone.get("git") or {}),
                change_summary_json=change_summary,
                objective_text=objective_text,
                source=row.source,
                event_id=row.event_id,
                schema_version="v1",
                redaction_applied=bool(
                    row.redaction_applied or payload_redacted or title_redacted or objective_redacted or summary_redacted
                ),
                expires_at=row.created_at + timedelta(days=max(1, retention_days)),
            )
        )
    db.execute(
        text(
            """
            UPDATE capability_grants
            SET completion_outbox_id = :outbox_id,
                completion_recorded_at = :recorded_at
            WHERE workspace_id = :workspace_id
              AND owner_id = :owner_id
              AND session_id = :session_id
              AND completion_required = TRUE
              AND status = 'consumed'
              AND completion_recorded_at IS NULL
            """
        ),
        {
            "outbox_id": row.id,
            "recorded_at": now,
            "workspace_id": row.workspace_id,
            "owner_id": row.owner_id,
            "session_id": row.session_id,
        },
    )
    row.status = "delivered"
    row.attempts += 1
    row.last_error = None
    row.delivered_at = now
    row.updated_at = now
    db.commit()
    return row


def deliver_handoff_safely(db: Session, *, outbox_id: uuid.UUID, retention_days: int) -> HandoffOutbox:
    try:
        return deliver_handoff(db, outbox_id=outbox_id, retention_days=retention_days)
    except Exception as exc:
        db.rollback()
        row = db.get(HandoffOutbox, outbox_id)
        if row is None:
            raise
        now = datetime.now(tz=UTC)
        row.attempts += 1
        row.status = "dead" if row.attempts >= 10 else "pending"
        row.last_error = str(exc)[:1000]
        row.next_attempt_at = now + timedelta(seconds=min(3600, 2 ** min(row.attempts, 10)))
        row.updated_at = now
        db.commit()
        return row


def record_resume_attempt(
    db: Session,
    *,
    packet_id: uuid.UUID,
    workspace_id: str,
    requesting_owner_id: str,
    target_owner_id: str,
    session_id: str,
    selected_record_id: uuid.UUID,
    query_text: str,
    top_file: str | None,
    recommended_files: list[str],
    requested_at: datetime,
    returned_at: datetime,
    handoff_ts: datetime,
) -> None:
    db.add(
        ContinuityResumeAttempt(
            packet_id=packet_id,
            workspace_id=workspace_id,
            requesting_owner_id=requesting_owner_id,
            target_owner_id=target_owner_id,
            session_id=str(session_id or "default")[:160],
            selected_record_id=selected_record_id,
            query_text=query_text[:500],
            top_file=(top_file or "")[:240] or None,
            requested_at=requested_at,
            returned_at=returned_at,
            latency_ms=max(0, int((returned_at - requested_at).total_seconds() * 1000)),
            time_since_handoff_ms=max(0, int((requested_at - handoff_ts).total_seconds() * 1000)),
            recommended_files_json=[str(value)[:240] for value in recommended_files[:40]],
        )
    )
    db.commit()


def record_resume_progress(
    db: Session,
    *,
    workspace_id: str,
    requesting_owner_id: str,
    packet_id: uuid.UUID | None = None,
    session_id: str | None = None,
    phase: str,
    opened_file: str | None = None,
    correct_file: bool | None = None,
    correct_anchor: bool | None = None,
    opened_file_rank: int | None = None,
    correction_required: bool | None = None,
    correction_reason: str | None = None,
    archaeology_tool_calls: int | None = None,
    archaeology_tokens: int | None = None,
    outcome_status: str | None = None,
    progress_source: str = "manual",
    max_age_hours: int = 24,
) -> tuple[ContinuityResumeAttempt, str] | None:
    statement = select(ContinuityResumeAttempt).where(
        ContinuityResumeAttempt.workspace_id == workspace_id,
        ContinuityResumeAttempt.requesting_owner_id == requesting_owner_id,
        ContinuityResumeAttempt.requested_at >= datetime.now(tz=UTC) - timedelta(hours=max(1, max_age_hours)),
    )
    if packet_id is not None:
        statement = statement.where(ContinuityResumeAttempt.packet_id == packet_id)
    if session_id:
        statement = statement.where(ContinuityResumeAttempt.session_id == str(session_id)[:160])
    row = db.execute(statement.order_by(ContinuityResumeAttempt.requested_at.desc()).limit(1)).scalar_one_or_none()
    if row is None:
        return None
    safe_file, _ = redact_text(str(opened_file or "")[:240])
    safe_reason, _ = redact_text(str(correction_reason or "")[:500])
    patch = progress_patch(
        {
            "recommended_files_json": row.recommended_files_json,
            "first_file_opened_at": row.first_file_opened_at,
            "productive_at": row.productive_at,
            "completed_at": row.completed_at,
        },
        phase=phase,
        now=datetime.now(tz=UTC),
        opened_file=safe_file or None,
        correct_file=correct_file,
        correct_anchor=correct_anchor,
        opened_file_rank=opened_file_rank,
        correction_required=correction_required,
        correction_reason=safe_reason if correction_reason is not None else None,
        archaeology_tool_calls=archaeology_tool_calls,
        archaeology_tokens=archaeology_tokens,
        outcome_status=outcome_status,
        progress_source=progress_source,
    )
    normalized_phase = str(patch.pop("phase"))
    for key, value in patch.items():
        setattr(row, key, value)
    db.commit()
    return row, normalized_phase


def pilot_metrics(db: Session, *, workspace_id: str, days: int) -> dict[str, Any]:
    since = datetime.now(tz=UTC) - timedelta(days=max(1, days))
    coverage = db.execute(
        text(
            """
            WITH eligible AS (
                SELECT 'directive:' || directive_id::text AS completion_key
                FROM directive_executions
                WHERE workspace_id = :workspace_id AND updated_at >= :since
                  AND state IN ('succeeded', 'failed', 'blocked', 'abandoned')
                UNION
                SELECT 'directive:' || directive_id::text AS completion_key
                FROM capability_grants
                WHERE workspace_id = :workspace_id AND consumed_at >= :since
                  AND completion_required = TRUE AND status = 'consumed' AND directive_id IS NOT NULL
                UNION
                SELECT 'standalone:' || id::text AS completion_key
                FROM handoff_outbox
                WHERE workspace_id = :workspace_id AND created_at >= :since AND directive_id IS NULL
            ), captured AS (
                SELECT DISTINCT CASE
                    WHEN directive_id IS NULL THEN 'standalone:' || id::text
                    ELSE 'directive:' || directive_id::text
                END AS completion_key
                FROM handoff_outbox
                WHERE workspace_id = :workspace_id AND created_at >= :since AND status = 'delivered'
            )
            SELECT COUNT(*) AS eligible,
                   COUNT(*) FILTER (WHERE captured.completion_key IS NOT NULL) AS captured
            FROM eligible LEFT JOIN captured USING (completion_key)
            """
        ),
        {"workspace_id": workspace_id, "since": since},
    ).mappings().one()
    outbox = db.execute(
        text(
            """
            SELECT COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                   COUNT(*) FILTER (WHERE status = 'dead') AS dead
            FROM handoff_outbox
            WHERE workspace_id = :workspace_id AND created_at >= :since
            """
        ),
        {"workspace_id": workspace_id, "since": since},
    ).mappings().one()
    attempt_rows = db.execute(
        select(ContinuityResumeAttempt).where(
            ContinuityResumeAttempt.workspace_id == workspace_id,
            ContinuityResumeAttempt.requested_at >= since,
        )
    ).scalars().all()
    attempt_metrics = summarize_attempts(
        {
            "requested_at": row.requested_at,
            "latency_ms": row.latency_ms,
            "time_since_handoff_ms": row.time_since_handoff_ms,
            "first_file_opened_at": row.first_file_opened_at,
            "productive_at": row.productive_at,
            "completed_at": row.completed_at,
            "opened_file_rank": row.opened_file_rank,
            "correct_file": row.correct_file,
            "correct_anchor": row.correct_anchor,
            "correction_required": row.correction_required,
            "archaeology_tool_calls": row.archaeology_tool_calls,
            "archaeology_tokens": row.archaeology_tokens,
            "feedback_at": row.feedback_at,
        }
        for row in attempt_rows
    )
    eligible = int(coverage.get("eligible") or 0)
    captured = int(coverage.get("captured") or 0)
    return {
        "eligible_completion_count": eligible,
        "captured_completion_count": captured,
        "handoff_capture_coverage": float(captured / eligible) if eligible else 0.0,
        "outbox_pending_count": int(outbox.get("pending") or 0),
        "outbox_dead_count": int(outbox.get("dead") or 0),
        **attempt_metrics,
    }
