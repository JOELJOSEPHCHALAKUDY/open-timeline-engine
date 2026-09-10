from __future__ import annotations

import json
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_shared.behavior_control import (
    capability_policy,
    constant_time_token_match,
    hash_capability_token,
    mine_process_models,
    normalize_capability_operation,
    shadow_evaluation_metrics,
)
from tce_shared.decision_capture import resolve_prediction_fields


def issue_capability_grant(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    body: dict[str, Any],
    charter_id: UUID | None = None,
    effect_journal_enabled: bool = False,
) -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    operation = normalize_capability_operation(
        capability=str(body.get("capability") or ""),
        action=str(body.get("action") or ""),
        resource=str(body.get("resource") or ""),
        arguments=dict(body.get("arguments") or {}),
    )
    policy = capability_policy(operation["capability"])
    decision = str(policy["decision"])
    reason = str(policy["reason"])
    permit_id = body.get("permit_id")
    directive_id = body.get("directive_id")
    session_id = str(body.get("session_id") or "default")
    if policy["mutating"] and (not permit_id or not directive_id):
        decision, reason = "blocked", "mutating capabilities require directive_id and permit_id"
    elif policy["mutating"]:
        open_obligation = db.execute(
            text(
                """
                SELECT id FROM capability_grants
                WHERE workspace_id = :workspace_id AND owner_id = :owner_id
                  AND session_id = :session_id AND status = 'consumed'
                  AND completion_required = TRUE AND completion_recorded_at IS NULL
                LIMIT 1
                """
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id, "session_id": session_id},
        ).scalar_one_or_none()
        if open_obligation is not None:
            decision, reason = "blocked", "previous mutating action requires completion capture"
        elif effect_journal_enabled:
            # The completion obligation widened to the effect journal, with the caller's OWN
            # directive excluded.  A dispatch opens its root effect and holds it at 'running' for
            # the whole run — exactly the window in which the agent asks for its grants — so
            # without the exclusion every healthy dispatch deadlocks on itself.
            open_effect = db.execute(
                text(
                    """
                    SELECT 1 FROM effect_journal ej
                     WHERE ej.workspace_id = :workspace_id
                       AND ej.owner_id = :owner_id
                       AND ej.session_id = :session_id
                       AND ej.state IN ('prepared', 'running', 'unknown')
                       AND (:current_directive_id IS NULL OR ej.directive_id <> :current_directive_id)
                     LIMIT 1
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "owner_id": owner_id,
                    "session_id": session_id,
                    "current_directive_id": directive_id,
                },
            ).scalar_one_or_none()
            if open_effect is not None:
                decision, reason = "blocked", "an effect from another directive in this session is unresolved"
        permit = db.execute(
            text(
                """
                SELECT p.decision, p.expires_at, d.state, d.permit_id
                FROM execution_permits p
                JOIN directive_executions d ON d.directive_id = :directive_id
                WHERE p.id = :permit_id AND p.workspace_id = :workspace_id
                  AND p.session_id = :session_id AND d.workspace_id = :workspace_id
                  AND d.user_id = :owner_id AND d.session_id = :session_id
                LIMIT 1
                """
            ),
            {
                "directive_id": directive_id,
                "permit_id": permit_id,
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "session_id": session_id,
            },
        ).mappings().first()
        if decision == "blocked":
            pass
        elif permit is None:
            decision, reason = "blocked", "matching directive and permit not found"
        elif str(permit["decision"]) != "allow":
            decision, reason = "blocked", "permit is not allowed"
        elif permit["expires_at"] and permit["expires_at"] < now:
            decision, reason = "blocked", "permit expired"
        elif str(permit["state"]) != "in_progress":
            decision, reason = "blocked", "directive must be claimed and in progress"
        elif str(permit["permit_id"] or "") != str(permit_id):
            decision, reason = "blocked", "directive is not bound to this permit"

    grant_id = uuid.uuid4()
    token = secrets.token_urlsafe(32) if decision == "allow" else None
    expires_at = now + timedelta(seconds=max(30, min(900, int(body.get("ttl_seconds", 120) or 120))))
    db.execute(
        text(
            """
            INSERT INTO capability_grants (
                id, workspace_id, owner_id, session_id, directive_id, permit_id,
                capability, action, resource, action_digest, token_hash, status,
                decision, reason, risk_tier, mutating, redaction_applied,
                created_at, expires_at, consumed_at, completion_required, charter_id
            ) VALUES (
                :id, :workspace_id, :owner_id, :session_id, :directive_id, :permit_id,
                :capability, :action, :resource, :action_digest, :token_hash, :status,
                :decision, :reason, :risk_tier, :mutating, :redaction_applied,
                :created_at, :expires_at, NULL, :completion_required, :charter_id
            )
            """
        ),
        {
            "id": grant_id,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "session_id": session_id,
            "directive_id": directive_id,
            "permit_id": permit_id,
            **operation,
            "token_hash": hash_capability_token(token) if token else "",
            "status": "authorized" if decision == "allow" else "denied",
            "decision": decision,
            "reason": reason,
            "risk_tier": policy["risk_tier"],
            "mutating": bool(policy["mutating"]),
            "completion_required": bool(policy["mutating"]),
            "charter_id": charter_id,
            "created_at": now,
            "expires_at": expires_at,
        },
    )
    db.commit()
    return {
        "grant_id": grant_id,
        "token": token,
        "status": "authorized" if decision == "allow" else "denied",
        "decision": decision,
        "reason": reason,
        "risk_tier": policy["risk_tier"],
        "mutating": bool(policy["mutating"]),
        "expires_at": expires_at,
        **{key: operation[key] for key in ("capability", "action", "resource", "action_digest")},
    }


def consume_capability_grant(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    operation = normalize_capability_operation(
        capability=str(body.get("capability") or ""),
        action=str(body.get("action") or ""),
        resource=str(body.get("resource") or ""),
        arguments=dict(body.get("arguments") or {}),
    )
    row = db.execute(
        text(
            """
            SELECT token_hash, action_digest, status, expires_at
            FROM capability_grants
            WHERE id = :id AND workspace_id = :workspace_id AND owner_id = :owner_id
            LIMIT 1
            """
        ),
        {"id": body["grant_id"], "workspace_id": workspace_id, "owner_id": owner_id},
    ).mappings().first()
    reason = "capability grant not found"
    authorized = False
    if row is not None:
        if str(row["status"]) != "authorized":
            reason = "capability grant is not active"
        elif row["expires_at"] < now:
            reason = "capability grant expired"
        elif str(row["action_digest"]) != operation["action_digest"]:
            reason = "operation does not match authorized digest"
        elif not constant_time_token_match(str(body.get("token") or ""), str(row["token_hash"])):
            reason = "invalid capability token"
        else:
            updated = db.execute(
                text(
                    """
                    UPDATE capability_grants
                    SET status = 'consumed', consumed_at = :consumed_at
                    WHERE id = :id AND workspace_id = :workspace_id AND owner_id = :owner_id
                      AND status = 'authorized' AND consumed_at IS NULL AND expires_at >= :consumed_at
                    """
                ),
                {
                    "id": body["grant_id"],
                    "workspace_id": workspace_id,
                    "owner_id": owner_id,
                    "consumed_at": now,
                },
            )
            authorized = int(getattr(updated, "rowcount", 0) or 0) == 1
            reason = "authorized" if authorized else "capability grant was already consumed"
    if row is not None and row["expires_at"] < now and str(row["status"]) == "authorized":
        db.execute(
            text("UPDATE capability_grants SET status = 'expired' WHERE id = :id AND status = 'authorized'"),
            {"id": body["grant_id"]},
        )
    db.commit()
    return {
        "grant_id": body["grant_id"],
        "authorized": authorized,
        "status": "consumed" if authorized else "denied",
        "reason": reason,
        "action_digest": operation["action_digest"],
        "consumed_at": now if authorized else None,
    }


def load_process_source_rows(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    lookback_days: int,
    limit: int,
) -> list[dict[str, Any]]:
    cutoff = datetime.now(tz=UTC) - timedelta(days=max(1, lookback_days))
    rows = db.execute(
        text(
            """
            SELECT id, session_id, action_kind, result, ts
            FROM takeover_action_log
            WHERE workspace_id = :workspace_id AND user_id = :owner_id
              AND ts >= :cutoff
            ORDER BY ts ASC
            LIMIT :limit
            """
        ),
        {
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "cutoff": cutoff,
            "limit": max(10, min(limit, 5000)),
        },
    ).mappings().all()
    directive_rows = db.execute(
        text(
            """
            SELECT directive_id AS id, session_id,
                   CONCAT('directive:', action_kind, ':', state) AS action_kind,
                   state AS result, updated_at AS ts
            FROM directive_executions
            WHERE workspace_id = :workspace_id AND user_id = :owner_id
              AND updated_at >= :cutoff
            ORDER BY updated_at ASC
            LIMIT :limit
            """
        ),
        {
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "cutoff": cutoff,
            "limit": max(10, min(limit, 5000)),
        },
    ).mappings().all()
    output = [dict(row) for row in rows] + [dict(row) for row in directive_rows]
    output.sort(key=lambda row: (str(row.get("ts") or ""), str(row.get("id") or "")))
    return output[: max(10, min(limit, 5000))]


def save_process_models(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    models: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    now = datetime.now(tz=UTC)
    output: list[dict[str, Any]] = []
    for model in models:
        process_id = db.execute(
            text(
                """
                INSERT INTO behavior_process_models (
                    id, workspace_id, subject_user_id, process_signature, name,
                    steps_json, transitions_json, support, success_rate, reliability,
                    source_sessions_json, evidence_ids_json, status, created_at, updated_at, schema_version
                ) VALUES (
                    :id, :workspace_id, :subject_user_id, :process_signature, :name,
                    CAST(:steps AS jsonb), CAST(:transitions AS jsonb), :support, :success_rate, :reliability,
                    CAST(:sessions AS jsonb), CAST(:evidence AS jsonb), 'candidate', :now, :now, 'v1'
                )
                ON CONFLICT (workspace_id, subject_user_id, process_signature)
                DO UPDATE SET name = EXCLUDED.name, steps_json = EXCLUDED.steps_json,
                    transitions_json = EXCLUDED.transitions_json, support = EXCLUDED.support,
                    success_rate = EXCLUDED.success_rate, reliability = EXCLUDED.reliability,
                    source_sessions_json = EXCLUDED.source_sessions_json,
                    evidence_ids_json = EXCLUDED.evidence_ids_json, updated_at = EXCLUDED.updated_at
                RETURNING id
                """
            ),
            {
                "id": uuid.uuid4(),
                "workspace_id": workspace_id,
                "subject_user_id": subject_user_id,
                "process_signature": model["process_signature"],
                "name": model["name"],
                "steps": json.dumps(model["steps"]),
                "transitions": json.dumps(model["transitions"]),
                "support": model["support"],
                "success_rate": model["success_rate"],
                "reliability": model["reliability"],
                "sessions": json.dumps(model["source_sessions"]),
                "evidence": json.dumps(model["evidence_ids"]),
                "now": now,
            },
        ).scalar_one()
        output.append({**model, "process_id": process_id, "created_at": now, "updated_at": now})
    db.commit()
    return output


def list_process_models(
    db: Session, *, workspace_id: str, subject_user_id: str, status: str | None, limit: int
) -> list[dict[str, Any]]:
    status_clause = "AND status = :status" if status else ""
    rows = db.execute(
        text(
            f"""
            SELECT id, process_signature, name, steps_json, transitions_json, support,
                   success_rate, reliability, source_sessions_json, evidence_ids_json,
                   status, created_at, updated_at, schema_version
            FROM behavior_process_models
            WHERE workspace_id = :workspace_id AND subject_user_id = :subject_user_id {status_clause}
            ORDER BY reliability DESC, support DESC, updated_at DESC
            LIMIT :limit
            """
        ),
        {
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "status": status,
            "limit": max(1, min(limit, 100)),
        },
    ).mappings().all()
    review_rows = db.execute(
        text(
            """
            SELECT target_id, id FROM behavior_memory_reviews
            WHERE workspace_id = :workspace_id AND subject_user_id = :subject_user_id
              AND target_type = 'process_model'
            """
        ),
        {"workspace_id": workspace_id, "subject_user_id": subject_user_id},
    ).all()
    review_by_target = {str(row[0]): row[1] for row in review_rows}
    return [
        {
            "process_id": row["id"],
            "process_signature": row["process_signature"],
            "name": row["name"],
            "steps": list(row["steps_json"] or []),
            "transitions": list(row["transitions_json"] or []),
            "support": row["support"],
            "success_rate": row["success_rate"],
            "reliability": row["reliability"],
            "source_sessions": list(row["source_sessions_json"] or []),
            "evidence_ids": list(row["evidence_ids_json"] or []),
            "status": row["status"],
            "review_id": review_by_target.get(str(row["id"])),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "schema_version": row["schema_version"],
        }
        for row in rows
    ]


def create_memory_review(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    target_type: str,
    target_id: UUID,
    title: str,
    rationale: str,
    source: str,
    score: float,
    status: str = "pending",
) -> UUID:
    review_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    result = db.execute(
        text(
            """
            INSERT INTO behavior_memory_reviews (
                id, workspace_id, subject_user_id, target_type, target_id, title,
                rationale, status, proposed_action, source, score, reviewer_id,
                review_note, created_at, resolved_at, schema_version
            ) VALUES (
                :id, :workspace_id, :subject_user_id, :target_type, :target_id, :title,
                :rationale, :status, 'promote', :source, :score, NULL, '', :created_at,
                CASE WHEN :status = 'pending' THEN NULL ELSE :created_at END, 'v1'
            )
            ON CONFLICT (workspace_id, subject_user_id, target_type, target_id)
            DO NOTHING RETURNING id
            """
        ),
        {
            "id": review_id,
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "target_type": target_type,
            "target_id": target_id,
            "title": title[:500],
            "rationale": rationale[:1000],
            "status": status,
            "source": source[:80],
            "score": max(0.0, min(1.0, float(score))),
            "created_at": now,
        },
    ).first()
    if result is None:
        review_id = db.execute(
            text(
                """
                SELECT id FROM behavior_memory_reviews
                WHERE workspace_id = :workspace_id AND subject_user_id = :subject_user_id
                  AND target_type = :target_type AND target_id = :target_id
                """
            ),
            {
                "workspace_id": workspace_id,
                "subject_user_id": subject_user_id,
                "target_type": target_type,
                "target_id": target_id,
            },
        ).scalar_one()
    db.commit()
    return review_id


def list_memory_reviews(
    db: Session, *, workspace_id: str, subject_user_id: str, status: str | None, limit: int
) -> list[dict[str, Any]]:
    status_clause = "AND status = :status" if status else ""
    rows = db.execute(
        text(
            f"""
            SELECT id, target_type, target_id, title, rationale, status, proposed_action,
                   source, score, reviewer_id, review_note, created_at, resolved_at, schema_version
            FROM behavior_memory_reviews
            WHERE workspace_id = :workspace_id AND subject_user_id = :subject_user_id {status_clause}
            ORDER BY created_at DESC LIMIT :limit
            """
        ),
        {
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "status": status,
            "limit": max(1, min(limit, 200)),
        },
    ).mappings().all()
    return [{"review_id": row["id"], **{key: value for key, value in dict(row).items() if key != "id"}} for row in rows]


def resolve_memory_review(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    review_id: UUID,
    reviewer_id: str,
    decision: str,
    note: str,
) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT target_type, target_id FROM behavior_memory_reviews
            WHERE id = :id AND workspace_id = :workspace_id AND subject_user_id = :subject_user_id
              AND status = 'pending'
            LIMIT 1
            """
        ),
        {"id": review_id, "workspace_id": workspace_id, "subject_user_id": subject_user_id},
    ).mappings().first()
    if row is None:
        return None
    status = "promoted" if decision == "promote" else "rejected"
    now = datetime.now(tz=UTC)
    db.execute(
        text(
            """
            UPDATE behavior_memory_reviews SET status = :status, reviewer_id = :reviewer_id,
                review_note = :note, resolved_at = :resolved_at WHERE id = :id
            """
        ),
        {"status": status, "reviewer_id": reviewer_id, "note": note[:1000], "resolved_at": now, "id": review_id},
    )
    if row["target_type"] == "evidence":
        db.execute(
            text(
                """
                UPDATE decision_observations
                SET learning_eligible = :eligible,
                    storage_decision = :storage_decision,
                    lifecycle_status = CASE WHEN :eligible THEN lifecycle_status ELSE 'rejected' END
                WHERE id = :target_id AND workspace_id = :workspace_id
                  AND subject_user_id = :subject_user_id
                """
            ),
            {
                "eligible": decision == "promote",
                "storage_decision": "learn" if decision == "promote" else "rejected",
                "target_id": row["target_id"],
                "workspace_id": workspace_id,
                "subject_user_id": subject_user_id,
            },
        )
    elif row["target_type"] == "process_model":
        process = db.execute(
            text(
                """
                SELECT name, steps_json, support, success_rate, reliability, process_signature
                FROM behavior_process_models
                WHERE id = :target_id AND workspace_id = :workspace_id
                  AND subject_user_id = :subject_user_id
                """
            ),
            {
                "target_id": row["target_id"],
                "workspace_id": workspace_id,
                "subject_user_id": subject_user_id,
            },
        ).mappings().first()
        db.execute(
            text(
                """
                UPDATE behavior_process_models SET status = :status, updated_at = :updated_at
                WHERE id = :target_id AND workspace_id = :workspace_id
                  AND subject_user_id = :subject_user_id
                """
            ),
            {
                "status": "active" if decision == "promote" else "rejected",
                "updated_at": now,
                "target_id": row["target_id"],
                "workspace_id": workspace_id,
                "subject_user_id": subject_user_id,
            },
        )
        if decision == "promote" and process is not None:
            graph = {
                "steps": list(process["steps_json"] or []),
                "success_count": int(round(float(process["success_rate"]) * int(process["support"]))),
                "failure_count": max(0, int(process["support"]) - int(round(float(process["success_rate"]) * int(process["support"])))),
                "reliability": float(process["reliability"]),
                "source": "behavior_process_mining",
            }
            db.execute(
                text(
                    """
                    INSERT INTO workflow_templates(id, name, domain, graph, triggers, version, updated_at)
                    VALUES(:id, :name, 'behavior', CAST(:graph AS jsonb), CAST(:triggers AS jsonb), 1, :updated_at)
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "name": str(process["name"]),
                    "graph": json.dumps(graph),
                    "triggers": json.dumps(
                        {
                            "workspace_id": workspace_id,
                            "subject_user_id": subject_user_id,
                            "process_model_id": str(row["target_id"]),
                            "process_signature": str(process["process_signature"]),
                        }
                    ),
                    "updated_at": now,
                },
            )
    db.commit()
    return next(
        (item for item in list_memory_reviews(db, workspace_id=workspace_id, subject_user_id=subject_user_id, status=None, limit=200) if item["review_id"] == review_id),
        None,
    )


def save_shadow_prediction(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    observation_id: UUID,
    actual_choice: str,
    query: dict[str, Any],
    prediction: dict[str, Any],
    evidence_count: int,
    latency_ms: int,
) -> UUID:
    prediction_id = uuid.uuid4()
    predicted = prediction.get("predicted_choice")
    abstained = bool(prediction.get("abstained", True))
    correct = None if abstained else str(predicted).strip().casefold() == actual_choice.strip().casefold()
    db.execute(
        text(
            """
            INSERT INTO behavior_shadow_predictions (
                id, workspace_id, subject_user_id, observation_id, predicted_choice,
                actual_choice, confidence, abstained, correct, evidence_count, latency_ms,
                query_json, citations_json, created_at, schema_version,
                prediction_stage, resolution_state, resolved_at
            ) VALUES (
                :id, :workspace_id, :subject_user_id, :observation_id, :predicted_choice,
                :actual_choice, :confidence, :abstained, :correct, :evidence_count, :latency_ms,
                CAST(:query_json AS jsonb), CAST(:citations_json AS jsonb), :created_at, 'v1',
                'retrospective', 'resolved', :created_at
            )
            """
        ),
        {
            "id": prediction_id,
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "observation_id": observation_id,
            "predicted_choice": predicted,
            "actual_choice": actual_choice,
            "confidence": float(prediction.get("confidence", 0.0) or 0.0),
            "abstained": abstained,
            "correct": correct,
            "evidence_count": evidence_count,
            "latency_ms": max(0, latency_ms),
            "query_json": json.dumps(query),
            "citations_json": json.dumps(prediction.get("citations") or []),
            "created_at": datetime.now(tz=UTC),
        },
    )
    db.commit()
    return prediction_id


def shadow_status(db: Session, *, workspace_id: str, subject_user_id: str, limit: int) -> dict[str, Any]:
    rows = db.execute(
        text(
            """
            SELECT id, observation_id, predicted_choice, actual_choice, confidence, abstained,
                   correct, evidence_count, latency_ms, created_at, schema_version,
                   prediction_stage, resolution_state, opportunity_id, decision_family,
                   frozen_at, resolved_at
            FROM behavior_shadow_predictions
            WHERE workspace_id = :workspace_id AND subject_user_id = :subject_user_id
            ORDER BY created_at DESC LIMIT :limit
            """
        ),
        {"workspace_id": workspace_id, "subject_user_id": subject_user_id, "limit": max(1, min(limit, 500))},
    ).mappings().all()
    items = [dict(row) for row in rows]
    return {
        "metrics": shadow_evaluation_metrics(items),
        "recent": [{"prediction_id": item.pop("id"), **item} for item in items[:20]],
    }


def freeze_shadow_prediction(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    opportunity_id: UUID,
    session_id: str,
    turn: int,
    decision_family: str,
    query: dict[str, Any],
    prediction: dict[str, Any],
    evidence_count: int,
    latency_ms: int,
    evidence_cutoff_at: datetime | None,
    evidence_revision: str,
    advice_visible: bool,
    frozen_at: datetime,
) -> UUID:
    """Persist a prospective prediction (or abstention) BEFORE the human answer exists.

    No commit: the caller's transaction (takeover state save) commits it together with
    the decision opportunity so the two never disagree.
    """
    prediction_id = uuid.uuid4()
    predicted = prediction.get("predicted_choice")
    abstained = bool(prediction.get("abstained", True))
    db.execute(
        text(
            """
            INSERT INTO behavior_shadow_predictions (
                id, workspace_id, subject_user_id, observation_id, predicted_choice,
                actual_choice, confidence, abstained, correct, evidence_count, latency_ms,
                query_json, citations_json, created_at, schema_version,
                opportunity_id, session_id, turn, decision_family, prediction_stage,
                frozen_at, evidence_cutoff_at, evidence_revision, prediction_shown_at,
                advice_visible, resolution_state
            ) VALUES (
                :id, :workspace_id, :subject_user_id, NULL, :predicted_choice,
                '', :confidence, :abstained, NULL, :evidence_count, :latency_ms,
                CAST(:query_json AS jsonb), CAST(:citations_json AS jsonb), :created_at, 'v1',
                :opportunity_id, :session_id, :turn, :decision_family, 'prospective',
                :frozen_at, :evidence_cutoff_at, :evidence_revision, NULL,
                :advice_visible, 'pending'
            )
            """
        ),
        {
            "id": prediction_id,
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "predicted_choice": None if abstained else (str(predicted) if predicted is not None else None),
            "confidence": float(prediction.get("confidence", 0.0) or 0.0),
            "abstained": abstained,
            "evidence_count": int(evidence_count),
            "latency_ms": max(0, int(latency_ms)),
            "query_json": json.dumps(query, default=str),
            "citations_json": json.dumps(prediction.get("citations") or [], default=str),
            "created_at": frozen_at,
            "opportunity_id": opportunity_id,
            "session_id": session_id,
            "turn": int(turn),
            "decision_family": decision_family,
            "frozen_at": frozen_at,
            "evidence_cutoff_at": evidence_cutoff_at,
            "evidence_revision": evidence_revision,
            # Passed through, never re-derived. The caller decides what "the advisor's output
            # was rendered" means for its site; a `bool(<payload>)` here is how this column came
            # to be True on every row ever written.
            "advice_visible": advice_visible,
        },
    )
    return prediction_id


def resolve_shadow_prediction(
    db: Session,
    *,
    prediction_id: UUID,
    actual_choice: str,
    observation_id: UUID | None,
    resolution_source: str,
    human_source_ref: str,
    resolution_source_event_id: UUID | None,
    resolved_at: datetime,
) -> bool:
    """Resolve a pending prospective prediction against an authenticated human answer. No commit."""
    row = db.execute(
        text("SELECT predicted_choice, abstained FROM behavior_shadow_predictions WHERE id = :id AND resolution_state = 'pending'"),
        {"id": prediction_id},
    ).mappings().first()
    if row is None:
        return False
    correct = resolve_prediction_fields(row.get("predicted_choice"), bool(row.get("abstained", True)), actual_choice)["correct"]
    result = db.execute(
        text(
            """
            UPDATE behavior_shadow_predictions
            SET actual_choice = :actual_choice,
                correct = :correct,
                observation_id = :observation_id,
                resolved_at = :resolved_at,
                resolution_state = 'resolved',
                resolution_source = :resolution_source,
                human_source_ref = :human_source_ref,
                resolution_source_event_id = :resolution_source_event_id
            WHERE id = :id AND resolution_state = 'pending'
            """
        ),
        {
            "actual_choice": actual_choice,
            "correct": correct,
            "observation_id": observation_id,
            "resolved_at": resolved_at,
            "resolution_source": resolution_source,
            "human_source_ref": human_source_ref,
            "resolution_source_event_id": resolution_source_event_id,
            "id": prediction_id,
        },
    )
    return int(getattr(result, "rowcount", 0) or 0) == 1


def append_shadow_correction(db: Session, *, prediction_id: UUID, correction: dict[str, Any]) -> None:
    """Append-only correction history; never rewrites actual_choice/correct."""
    db.execute(
        text(
            """
            UPDATE behavior_shadow_predictions
            SET corrections_json = COALESCE(corrections_json, '[]'::jsonb) || CAST(:entry AS jsonb)
            WHERE id = :id
            """
        ),
        {"id": prediction_id, "entry": json.dumps([correction], default=str)},
    )


def mark_shadow_unresolved(db: Session, *, prediction_id: UUID, resolution_state: str, resolution_source: str | None) -> None:
    """Move a still-pending prediction into a separate non-resolved denominator."""
    if resolution_state not in {"unanswered", "missing_label", "extraction_error", "missed_capture", "abandoned"}:
        raise ValueError(f"invalid unresolved state: {resolution_state}")
    db.execute(
        text(
            """
            UPDATE behavior_shadow_predictions
            SET resolution_state = :resolution_state,
                resolution_source = :resolution_source
            WHERE id = :id AND resolution_state = 'pending'
            """
        ),
        {"id": prediction_id, "resolution_state": resolution_state, "resolution_source": resolution_source},
    )


def create_counterfactual(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    owner_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    counterfactual_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    db.execute(
        text(
            """
            INSERT INTO behavior_counterfactuals (
                id, workspace_id, subject_user_id, owner_id, observation_id, session_id,
                directive_id, decision, alternative, expected_outcome, assumptions_json,
                confidence, status, assessment, observed_outcome, lesson, regret_score,
                redaction_applied, review_at, created_at, resolved_at, schema_version
            ) VALUES (
                :id, :workspace_id, :subject_user_id, :owner_id, :observation_id, :session_id,
                :directive_id, :decision, :alternative, :expected_outcome,
                CAST(:assumptions AS jsonb), :confidence, 'open', NULL, '', '', NULL,
                :redaction_applied, :review_at, :created_at, NULL, 'v1'
            )
            """
        ),
        {
            "id": counterfactual_id,
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "owner_id": owner_id,
            "observation_id": body.get("observation_id"),
            "session_id": body.get("session_id") or "default",
            "directive_id": body.get("directive_id"),
            "decision": body["decision"],
            "alternative": body["alternative"],
            "expected_outcome": body["expected_outcome"],
            "assumptions": json.dumps(body.get("assumptions") or []),
            "confidence": body.get("confidence", 0.5),
            "redaction_applied": bool(body.get("redaction_applied")),
            "review_at": body.get("review_at"),
            "created_at": now,
        },
    )
    db.commit()
    return get_counterfactual(db, workspace_id=workspace_id, subject_user_id=subject_user_id, counterfactual_id=counterfactual_id) or {}


def get_counterfactual(
    db: Session, *, workspace_id: str, subject_user_id: str, counterfactual_id: UUID
) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT id, observation_id, session_id, directive_id, decision, alternative,
                   expected_outcome, assumptions_json, confidence, status, assessment,
                   observed_outcome, lesson, regret_score, redaction_applied, review_at,
                   created_at, resolved_at, schema_version
            FROM behavior_counterfactuals
            WHERE id = :id AND workspace_id = :workspace_id AND subject_user_id = :subject_user_id
            """
        ),
        {"id": counterfactual_id, "workspace_id": workspace_id, "subject_user_id": subject_user_id},
    ).mappings().first()
    if row is None:
        return None
    item = dict(row)
    item["counterfactual_id"] = item.pop("id")
    item["assumptions"] = list(item.pop("assumptions_json") or [])
    return item


def list_counterfactuals(
    db: Session, *, workspace_id: str, subject_user_id: str, status: str | None, limit: int
) -> list[dict[str, Any]]:
    status_clause = "AND status = :status" if status else ""
    ids = db.execute(
        text(
            f"""
            SELECT id FROM behavior_counterfactuals
            WHERE workspace_id = :workspace_id AND subject_user_id = :subject_user_id {status_clause}
            ORDER BY created_at DESC LIMIT :limit
            """
        ),
        {
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "status": status,
            "limit": max(1, min(limit, 200)),
        },
    ).scalars().all()
    return [item for value in ids if (item := get_counterfactual(db, workspace_id=workspace_id, subject_user_id=subject_user_id, counterfactual_id=value))]


def resolve_counterfactual(
    db: Session,
    *,
    workspace_id: str,
    subject_user_id: str,
    counterfactual_id: UUID,
    body: dict[str, Any],
) -> dict[str, Any] | None:
    observed = str(body.get("observed_outcome") or "")[:1000]
    lesson = str(body.get("lesson") or "")[:1000]
    result = db.execute(
        text(
            """
            UPDATE behavior_counterfactuals SET status = 'resolved', assessment = :assessment,
                observed_outcome = :observed_outcome, lesson = :lesson,
                regret_score = :regret_score,
                redaction_applied = redaction_applied OR :redaction_applied,
                resolved_at = :resolved_at
            WHERE id = :id AND workspace_id = :workspace_id AND subject_user_id = :subject_user_id
              AND status = 'open'
            """
        ),
        {
            "assessment": body["assessment"],
            "observed_outcome": observed,
            "lesson": lesson,
            "regret_score": body.get("regret_score"),
            "redaction_applied": bool(body.get("redaction_applied")),
            "resolved_at": datetime.now(tz=UTC),
            "id": counterfactual_id,
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
        },
    )
    db.commit()
    if int(getattr(result, "rowcount", 0) or 0) != 1:
        return None
    return get_counterfactual(db, workspace_id=workspace_id, subject_user_id=subject_user_id, counterfactual_id=counterfactual_id)


def mine_and_time(rows: list[dict[str, Any]], *, min_support: int, max_steps: int) -> tuple[list[dict[str, Any]], int]:
    started = time.perf_counter()
    models = mine_process_models(rows, min_support=min_support, max_steps=max_steps)
    return models, max(0, int((time.perf_counter() - started) * 1000))
