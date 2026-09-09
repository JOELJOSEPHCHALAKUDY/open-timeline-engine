from __future__ import annotations

import json
import secrets
import sqlite3
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from tce_shared.behavior_control import (
    capability_policy,
    constant_time_token_match,
    hash_capability_token,
    mine_process_models,
    normalize_capability_operation,
    shadow_evaluation_metrics,
)
from tce_shared.decision_capture import resolve_prediction_fields


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def issue_capability_grant(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    now = _now()
    operation = normalize_capability_operation(
        capability=str(body.get("capability") or ""),
        action=str(body.get("action") or ""),
        resource=str(body.get("resource") or ""),
        arguments=dict(body.get("arguments") or {}),
    )
    policy = capability_policy(operation["capability"])
    decision, reason = str(policy["decision"]), str(policy["reason"])
    permit_id = str(body["permit_id"]) if body.get("permit_id") else None
    directive_id = str(body["directive_id"]) if body.get("directive_id") else None
    session_id = str(body.get("session_id") or "default")
    if policy["mutating"] and (not permit_id or not directive_id):
        decision, reason = "blocked", "mutating capabilities require directive_id and permit_id"
    elif policy["mutating"]:
        open_obligation = conn.execute(
            """
            SELECT id FROM capability_grants
            WHERE workspace_id = ? AND owner_id = ? AND session_id = ?
              AND status = 'consumed' AND completion_required = 1
              AND completion_recorded_at IS NULL
            LIMIT 1
            """,
            (workspace_id, owner_id, session_id),
        ).fetchone()
        if open_obligation is not None:
            decision, reason = "blocked", "previous mutating action requires completion capture"
        permit = conn.execute(
            """
            SELECT p.decision, p.expires_at, d.state, d.permit_id
            FROM execution_permits p
            JOIN directive_executions d ON d.directive_id = ?
            WHERE p.id = ? AND p.workspace_id = ? AND p.session_id = ?
              AND d.workspace_id = ? AND d.user_id = ? AND d.session_id = ?
            LIMIT 1
            """,
            (directive_id, permit_id, workspace_id, session_id, workspace_id, owner_id, session_id),
        ).fetchone()
        if decision == "blocked":
            pass
        elif permit is None:
            decision, reason = "blocked", "matching directive and permit not found"
        elif str(permit["decision"]) != "allow":
            decision, reason = "blocked", "permit is not allowed"
        elif permit["expires_at"] and datetime.fromisoformat(str(permit["expires_at"])) < now:
            decision, reason = "blocked", "permit expired"
        elif str(permit["state"]) != "in_progress":
            decision, reason = "blocked", "directive must be claimed and in progress"
        elif str(permit["permit_id"] or "") != str(permit_id):
            decision, reason = "blocked", "directive is not bound to this permit"

    grant_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(32) if decision == "allow" else None
    expires_at = now + timedelta(seconds=max(30, min(900, int(body.get("ttl_seconds", 120) or 120))))
    conn.execute(
        """
        INSERT INTO capability_grants (
            id, workspace_id, owner_id, session_id, directive_id, permit_id,
            capability, action, resource, action_digest, token_hash, status,
            decision, reason, risk_tier, mutating, redaction_applied,
            created_at, expires_at, consumed_at, completion_required
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            grant_id,
            workspace_id,
            owner_id,
            session_id,
            str(directive_id) if directive_id else None,
            str(permit_id) if permit_id else None,
            operation["capability"],
            operation["action"],
            operation["resource"],
            operation["action_digest"],
            hash_capability_token(token) if token else "",
            "authorized" if decision == "allow" else "denied",
            decision,
            reason,
            policy["risk_tier"],
            1 if policy["mutating"] else 0,
            1 if operation["redaction_applied"] else 0,
            now.isoformat(),
            expires_at.isoformat(),
            1 if policy["mutating"] else 0,
        ),
    )
    conn.commit()
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
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    now = _now()
    operation = normalize_capability_operation(
        capability=str(body.get("capability") or ""),
        action=str(body.get("action") or ""),
        resource=str(body.get("resource") or ""),
        arguments=dict(body.get("arguments") or {}),
    )
    row = conn.execute(
        """
        SELECT token_hash, action_digest, status, expires_at FROM capability_grants
        WHERE id = ? AND workspace_id = ? AND owner_id = ? LIMIT 1
        """,
        (str(body["grant_id"]), workspace_id, owner_id),
    ).fetchone()
    authorized, reason = False, "capability grant not found"
    if row is not None:
        if str(row["status"]) != "authorized":
            reason = "capability grant is not active"
        elif datetime.fromisoformat(str(row["expires_at"])) < now:
            reason = "capability grant expired"
        elif str(row["action_digest"]) != operation["action_digest"]:
            reason = "operation does not match authorized digest"
        elif not constant_time_token_match(str(body.get("token") or ""), str(row["token_hash"])):
            reason = "invalid capability token"
        else:
            updated = conn.execute(
                """
                UPDATE capability_grants SET status = 'consumed', consumed_at = ?
                WHERE id = ? AND workspace_id = ? AND owner_id = ?
                  AND status = 'authorized' AND consumed_at IS NULL AND expires_at >= ?
                """,
                (now.isoformat(), str(body["grant_id"]), workspace_id, owner_id, now.isoformat()),
            )
            authorized = updated.rowcount == 1
            reason = "authorized" if authorized else "capability grant was already consumed"
    if row is not None and datetime.fromisoformat(str(row["expires_at"])) < now and str(row["status"]) == "authorized":
        conn.execute("UPDATE capability_grants SET status = 'expired' WHERE id = ?", (str(body["grant_id"]),))
    conn.commit()
    return {
        "grant_id": body["grant_id"],
        "authorized": authorized,
        "status": "consumed" if authorized else "denied",
        "reason": reason,
        "action_digest": operation["action_digest"],
        "consumed_at": now if authorized else None,
    }


def load_process_source_rows(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    lookback_days: int,
    limit: int,
) -> list[dict[str, Any]]:
    cutoff = (_now() - timedelta(days=max(1, lookback_days))).isoformat()
    rows = conn.execute(
        """
        SELECT id, session_id, action_kind, result, ts FROM takeover_action_log
        WHERE workspace_id = ? AND user_id = ? AND ts >= ? ORDER BY ts ASC LIMIT ?
        """,
        (workspace_id, owner_id, cutoff, max(10, min(limit, 5000))),
    ).fetchall()
    directive_rows = conn.execute(
        """
        SELECT directive_id AS id, session_id,
               'directive:' || action_kind || ':' || state AS action_kind,
               state AS result, updated_at AS ts
        FROM directive_executions
        WHERE workspace_id = ? AND user_id = ? AND updated_at >= ?
        ORDER BY updated_at ASC LIMIT ?
        """,
        (workspace_id, owner_id, cutoff, max(10, min(limit, 5000))),
    ).fetchall()
    output = [dict(row) for row in rows] + [dict(row) for row in directive_rows]
    output.sort(key=lambda row: (str(row.get("ts") or ""), str(row.get("id") or "")))
    return output[: max(10, min(limit, 5000))]


def save_process_models(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    models: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    now = _now()
    output: list[dict[str, Any]] = []
    for model in models:
        existing = conn.execute(
            """
            SELECT id, created_at, status FROM behavior_process_models
            WHERE workspace_id = ? AND subject_user_id = ? AND process_signature = ?
            """,
            (workspace_id, subject_user_id, model["process_signature"]),
        ).fetchone()
        process_id = str(existing["id"]) if existing else str(uuid.uuid4())
        created_at = datetime.fromisoformat(str(existing["created_at"])) if existing else now
        status = str(existing["status"]) if existing else "candidate"
        conn.execute(
            """
            INSERT INTO behavior_process_models (
                id, workspace_id, subject_user_id, process_signature, name, steps_json,
                transitions_json, support, success_rate, reliability, source_sessions_json,
                evidence_ids_json, status, created_at, updated_at, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1')
            ON CONFLICT(workspace_id, subject_user_id, process_signature) DO UPDATE SET
                name = excluded.name, steps_json = excluded.steps_json,
                transitions_json = excluded.transitions_json, support = excluded.support,
                success_rate = excluded.success_rate, reliability = excluded.reliability,
                source_sessions_json = excluded.source_sessions_json,
                evidence_ids_json = excluded.evidence_ids_json, updated_at = excluded.updated_at
            """,
            (
                process_id,
                workspace_id,
                subject_user_id,
                model["process_signature"],
                model["name"],
                _json(model["steps"]),
                _json(model["transitions"]),
                model["support"],
                model["success_rate"],
                model["reliability"],
                _json(model["source_sessions"]),
                _json(model["evidence_ids"]),
                status,
                created_at.isoformat(),
                now.isoformat(),
            ),
        )
        output.append({**model, "process_id": process_id, "status": status, "created_at": created_at, "updated_at": now})
    conn.commit()
    return output


def list_process_models(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    status: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    sql = """
        SELECT * FROM behavior_process_models
        WHERE workspace_id = ? AND subject_user_id = ?
    """
    params: list[Any] = [workspace_id, subject_user_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY reliability DESC, support DESC, updated_at DESC LIMIT ?"
    params.append(max(1, min(limit, 100)))
    rows = conn.execute(sql, params).fetchall()
    reviews = conn.execute(
        """
        SELECT target_id, id FROM behavior_memory_reviews
        WHERE workspace_id = ? AND subject_user_id = ? AND target_type = 'process_model'
        """,
        (workspace_id, subject_user_id),
    ).fetchall()
    review_by_target = {str(row["target_id"]): str(row["id"]) for row in reviews}
    output = []
    for row in rows:
        item = dict(row)
        output.append(
            {
                "process_id": item["id"],
                "process_signature": item["process_signature"],
                "name": item["name"],
                "steps": json.loads(item["steps_json"]),
                "transitions": json.loads(item["transitions_json"]),
                "support": item["support"],
                "success_rate": item["success_rate"],
                "reliability": item["reliability"],
                "source_sessions": json.loads(item["source_sessions_json"]),
                "evidence_ids": json.loads(item["evidence_ids_json"]),
                "status": item["status"],
                "review_id": review_by_target.get(str(item["id"])),
                "created_at": datetime.fromisoformat(item["created_at"]),
                "updated_at": datetime.fromisoformat(item["updated_at"]),
                "schema_version": item["schema_version"],
            }
        )
    return output


def create_memory_review(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    target_type: str,
    target_id: str,
    title: str,
    rationale: str,
    source: str,
    score: float,
    status: str = "pending",
) -> str:
    existing = conn.execute(
        """
        SELECT id FROM behavior_memory_reviews
        WHERE workspace_id = ? AND subject_user_id = ? AND target_type = ? AND target_id = ?
        """,
        (workspace_id, subject_user_id, target_type, target_id),
    ).fetchone()
    if existing:
        return str(existing["id"])
    review_id = str(uuid.uuid4())
    now = _now().isoformat()
    conn.execute(
        """
        INSERT INTO behavior_memory_reviews (
            id, workspace_id, subject_user_id, target_type, target_id, title, rationale,
            status, proposed_action, source, score, reviewer_id, review_note,
            created_at, resolved_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'promote', ?, ?, NULL, '', ?, ?, 'v1')
        """,
        (
            review_id,
            workspace_id,
            subject_user_id,
            target_type,
            target_id,
            title[:500],
            rationale[:1000],
            status,
            source[:80],
            max(0.0, min(1.0, float(score))),
            now,
            None if status == "pending" else now,
        ),
    )
    conn.commit()
    return review_id


def list_memory_reviews(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    status: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM behavior_memory_reviews WHERE workspace_id = ? AND subject_user_id = ?"
    params: list[Any] = [workspace_id, subject_user_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    output = []
    for row in conn.execute(sql, params).fetchall():
        item = dict(row)
        item["review_id"] = item.pop("id")
        item["created_at"] = datetime.fromisoformat(item["created_at"])
        item["resolved_at"] = datetime.fromisoformat(item["resolved_at"]) if item["resolved_at"] else None
        output.append(item)
    return output


def resolve_memory_review(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    review_id: str,
    reviewer_id: str,
    decision: str,
    note: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT target_type, target_id FROM behavior_memory_reviews
        WHERE id = ? AND workspace_id = ? AND subject_user_id = ? AND status = 'pending'
        """,
        (review_id, workspace_id, subject_user_id),
    ).fetchone()
    if row is None:
        return None
    status = "promoted" if decision == "promote" else "rejected"
    now = _now().isoformat()
    conn.execute(
        """
        UPDATE behavior_memory_reviews SET status = ?, reviewer_id = ?, review_note = ?, resolved_at = ?
        WHERE id = ?
        """,
        (status, reviewer_id, note[:1000], now, review_id),
    )
    if row["target_type"] == "evidence":
        conn.execute(
            """
            UPDATE decision_observations SET learning_eligible = ?, storage_decision = ?,
                lifecycle_status = CASE WHEN ? = 1 THEN lifecycle_status ELSE 'rejected' END
            WHERE id = ? AND workspace_id = ? AND subject_user_id = ?
            """,
            (
                1 if decision == "promote" else 0,
                "learn" if decision == "promote" else "rejected",
                1 if decision == "promote" else 0,
                row["target_id"],
                workspace_id,
                subject_user_id,
            ),
        )
    elif row["target_type"] == "process_model":
        process = conn.execute(
            """
            SELECT name, steps_json, support, success_rate, reliability, process_signature
            FROM behavior_process_models
            WHERE id = ? AND workspace_id = ? AND subject_user_id = ?
            """,
            (row["target_id"], workspace_id, subject_user_id),
        ).fetchone()
        conn.execute(
            """
            UPDATE behavior_process_models SET status = ?, updated_at = ?
            WHERE id = ? AND workspace_id = ? AND subject_user_id = ?
            """,
            ("active" if decision == "promote" else "rejected", now, row["target_id"], workspace_id, subject_user_id),
        )
        if decision == "promote" and process is not None:
            success_count = int(round(float(process["success_rate"]) * int(process["support"])))
            graph = {
                "steps": json.loads(process["steps_json"]),
                "success_count": success_count,
                "failure_count": max(0, int(process["support"]) - success_count),
                "reliability": float(process["reliability"]),
                "source": "behavior_process_mining",
            }
            conn.execute(
                """
                INSERT INTO workflow_templates(id, name, domain, graph, triggers, version, updated_at)
                VALUES(?, ?, 'behavior', ?, ?, 1, ?)
                """,
                (
                    str(uuid.uuid4()),
                    str(process["name"]),
                    _json(graph),
                    _json(
                        {
                            "workspace_id": workspace_id,
                            "subject_user_id": subject_user_id,
                            "process_model_id": str(row["target_id"]),
                            "process_signature": str(process["process_signature"]),
                        }
                    ),
                    now,
                ),
            )
    conn.commit()
    return next(
        (item for item in list_memory_reviews(conn, workspace_id=workspace_id, subject_user_id=subject_user_id, status=None, limit=200) if item["review_id"] == review_id),
        None,
    )


def save_shadow_prediction(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    observation_id: str,
    actual_choice: str,
    query: dict[str, Any],
    prediction: dict[str, Any],
    evidence_count: int,
    latency_ms: int,
) -> str:
    prediction_id = str(uuid.uuid4())
    predicted = prediction.get("predicted_choice")
    abstained = bool(prediction.get("abstained", True))
    correct = None if abstained else str(predicted).strip().casefold() == actual_choice.strip().casefold()
    created_at = _now().isoformat()
    # A prediction made in the same request as the answer is retrospective by construction.
    conn.execute(
        """
        INSERT INTO behavior_shadow_predictions (
            id, workspace_id, subject_user_id, observation_id, predicted_choice,
            actual_choice, confidence, abstained, correct, evidence_count, latency_ms,
            query_json, citations_json, created_at, schema_version,
            prediction_stage, resolution_state, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1', 'retrospective', 'resolved', ?)
        """,
        (
            prediction_id,
            workspace_id,
            subject_user_id,
            observation_id,
            predicted,
            actual_choice,
            float(prediction.get("confidence", 0.0) or 0.0),
            1 if abstained else 0,
            None if correct is None else (1 if correct else 0),
            evidence_count,
            max(0, latency_ms),
            _json(query),
            _json(prediction.get("citations") or []),
            created_at,
            created_at,
        ),
    )
    conn.commit()
    return prediction_id


def freeze_shadow_prediction(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    opportunity_id: str,
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
) -> str:
    """Store a prospective prediction (or abstention) BEFORE the human answer exists. No commit."""
    prediction_id = str(uuid.uuid4())
    abstained = bool(prediction.get("abstained", True))
    frozen_iso = frozen_at.isoformat()
    conn.execute(
        """
        INSERT INTO behavior_shadow_predictions (
            id, workspace_id, subject_user_id, observation_id, predicted_choice,
            actual_choice, confidence, abstained, correct, evidence_count, latency_ms,
            query_json, citations_json, created_at, schema_version,
            opportunity_id, session_id, turn, decision_family, prediction_stage,
            frozen_at, evidence_cutoff_at, evidence_revision, prediction_shown_at, advice_visible,
            resolution_state, corrections_json
        ) VALUES (?, ?, ?, NULL, ?, '', ?, ?, NULL, ?, ?, ?, ?, ?, 'v1',
                  ?, ?, ?, ?, 'prospective', ?, ?, ?, NULL, ?, 'pending', '[]')
        """,
        (
            prediction_id,
            workspace_id,
            subject_user_id,
            prediction.get("predicted_choice"),
            float(prediction.get("confidence", 0.0) or 0.0),
            1 if abstained else 0,
            max(0, int(evidence_count)),
            max(0, int(latency_ms)),
            _json(query),
            _json(prediction.get("citations") or []),
            frozen_iso,
            opportunity_id,
            session_id,
            max(0, int(turn)),
            decision_family,
            frozen_iso,
            evidence_cutoff_at.isoformat() if isinstance(evidence_cutoff_at, datetime) else None,
            evidence_revision,
            1 if advice_visible else 0,
        ),
    )
    return prediction_id


def resolve_shadow_prediction(
    conn: sqlite3.Connection,
    *,
    prediction_id: str,
    actual_choice: str,
    observation_id: str | None,
    resolution_source: str,
    human_source_ref: str,
    resolution_source_event_id: str | None,
    resolved_at: datetime,
    retrospective: bool = False,
) -> bool:
    """Resolve a pending prospective prediction against an authenticated human answer. No commit."""
    row = conn.execute(
        "SELECT predicted_choice, abstained FROM behavior_shadow_predictions WHERE id = ? AND resolution_state = 'pending'",
        (prediction_id,),
    ).fetchone()
    if row is None:
        return False
    correct = resolve_prediction_fields(row["predicted_choice"], bool(row["abstained"]), actual_choice)["correct"]
    cursor = conn.execute(
        """
        UPDATE behavior_shadow_predictions
        SET actual_choice = ?, correct = ?, observation_id = ?, resolved_at = ?, resolution_state = 'resolved',
            resolution_source = ?, human_source_ref = ?, resolution_source_event_id = ?,
            prediction_stage = CASE WHEN ? THEN 'retrospective' ELSE prediction_stage END
        WHERE id = ? AND resolution_state = 'pending'
        """,
        (
            actual_choice,
            None if correct is None else (1 if correct else 0),
            observation_id,
            resolved_at.isoformat(),
            resolution_source,
            human_source_ref,
            resolution_source_event_id,
            1 if retrospective else 0,
            prediction_id,
        ),
    )
    return int(cursor.rowcount or 0) == 1


def append_shadow_correction(conn: sqlite3.Connection, *, prediction_id: str, correction: dict[str, Any]) -> None:
    """Append-only correction history; never rewrites actual_choice/correct."""
    row = conn.execute("SELECT corrections_json FROM behavior_shadow_predictions WHERE id = ?", (prediction_id,)).fetchone()
    if row is None:
        return
    try:
        existing = json.loads(str(row["corrections_json"] or "[]"))
    except (TypeError, ValueError):
        existing = []
    if not isinstance(existing, list):
        existing = []
    existing.append(dict(correction))
    conn.execute("UPDATE behavior_shadow_predictions SET corrections_json = ? WHERE id = ?", (_json(existing), prediction_id))


def mark_shadow_unresolved(
    conn: sqlite3.Connection,
    *,
    prediction_id: str,
    resolution_state: str,
    resolution_source: str | None,
) -> None:
    """Move a pending prediction into a separate denominator (unanswered/missing_label/...). Only when pending."""
    conn.execute(
        """
        UPDATE behavior_shadow_predictions
        SET resolution_state = ?, resolution_source = COALESCE(?, resolution_source)
        WHERE id = ? AND resolution_state = 'pending'
        """,
        (resolution_state, resolution_source, prediction_id),
    )


def shadow_status(
    conn: sqlite3.Connection, *, workspace_id: str, subject_user_id: str, limit: int
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT id, observation_id, predicted_choice, actual_choice, confidence, abstained,
               correct, evidence_count, latency_ms, created_at, schema_version,
               prediction_stage, resolution_state, opportunity_id, decision_family, frozen_at, resolved_at
        FROM behavior_shadow_predictions
        WHERE workspace_id = ? AND subject_user_id = ? ORDER BY created_at DESC LIMIT ?
        """,
        (workspace_id, subject_user_id, max(1, min(limit, 500))),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["abstained"] = bool(item["abstained"])
        item["correct"] = None if item["correct"] is None else bool(item["correct"])
        item["created_at"] = datetime.fromisoformat(item["created_at"])
        item["prediction_stage"] = str(item.get("prediction_stage") or "retrospective")
        item["resolution_state"] = str(item.get("resolution_state") or "resolved")
        for key in ("frozen_at", "resolved_at"):
            value = item.get(key)
            item[key] = datetime.fromisoformat(str(value)) if value else None
        items.append(item)
    return {
        "metrics": shadow_evaluation_metrics(items),
        "recent": [{"prediction_id": item.pop("id"), **item} for item in items[:20]],
    }


def create_counterfactual(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    owner_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    counterfactual_id = str(uuid.uuid4())
    now = _now().isoformat()
    review_at = body.get("review_at")
    if isinstance(review_at, datetime):
        review_at = review_at.isoformat()
    conn.execute(
        """
        INSERT INTO behavior_counterfactuals (
            id, workspace_id, subject_user_id, owner_id, observation_id, session_id,
            directive_id, decision, alternative, expected_outcome, assumptions_json,
            confidence, status, assessment, observed_outcome, lesson, regret_score,
            redaction_applied, review_at, created_at, resolved_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, '', '', NULL, ?, ?, ?, NULL, 'v1')
        """,
        (
            counterfactual_id,
            workspace_id,
            subject_user_id,
            owner_id,
            str(body["observation_id"]) if body.get("observation_id") else None,
            body.get("session_id") or "default",
            str(body["directive_id"]) if body.get("directive_id") else None,
            body["decision"],
            body["alternative"],
            body["expected_outcome"],
            _json(body.get("assumptions") or []),
            body.get("confidence", 0.5),
            1 if body.get("redaction_applied") else 0,
            review_at,
            now,
        ),
    )
    conn.commit()
    return get_counterfactual(conn, workspace_id=workspace_id, subject_user_id=subject_user_id, counterfactual_id=counterfactual_id) or {}


def get_counterfactual(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    counterfactual_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM behavior_counterfactuals
        WHERE id = ? AND workspace_id = ? AND subject_user_id = ?
        """,
        (counterfactual_id, workspace_id, subject_user_id),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["counterfactual_id"] = item.pop("id")
    item.pop("workspace_id", None)
    item.pop("subject_user_id", None)
    item.pop("owner_id", None)
    item["assumptions"] = json.loads(item.pop("assumptions_json"))
    item["redaction_applied"] = bool(item["redaction_applied"])
    for key in ("review_at", "created_at", "resolved_at"):
        item[key] = datetime.fromisoformat(item[key]) if item.get(key) else None
    return item


def list_counterfactuals(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    status: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    sql = "SELECT id FROM behavior_counterfactuals WHERE workspace_id = ? AND subject_user_id = ?"
    params: list[Any] = [workspace_id, subject_user_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    ids = [str(row["id"]) for row in conn.execute(sql, params).fetchall()]
    return [item for value in ids if (item := get_counterfactual(conn, workspace_id=workspace_id, subject_user_id=subject_user_id, counterfactual_id=value))]


def resolve_counterfactual(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    subject_user_id: str,
    counterfactual_id: str,
    body: dict[str, Any],
) -> dict[str, Any] | None:
    now = _now().isoformat()
    result = conn.execute(
        """
        UPDATE behavior_counterfactuals SET status = 'resolved', assessment = ?,
            observed_outcome = ?, lesson = ?, regret_score = ?,
            redaction_applied = CASE WHEN redaction_applied = 1 OR ? = 1 THEN 1 ELSE 0 END,
            resolved_at = ?
        WHERE id = ? AND workspace_id = ? AND subject_user_id = ? AND status = 'open'
        """,
        (
            body["assessment"],
            str(body["observed_outcome"])[:1000],
            str(body.get("lesson") or "")[:1000],
            body.get("regret_score"),
            1 if body.get("redaction_applied") else 0,
            now,
            counterfactual_id,
            workspace_id,
            subject_user_id,
        ),
    )
    conn.commit()
    if result.rowcount != 1:
        return None
    return get_counterfactual(conn, workspace_id=workspace_id, subject_user_id=subject_user_id, counterfactual_id=counterfactual_id)


def mine_and_time(rows: list[dict[str, Any]], *, min_support: int, max_steps: int) -> tuple[list[dict[str, Any]], int]:
    started = time.perf_counter()
    models = mine_process_models(rows, min_support=min_support, max_steps=max_steps)
    return models, max(0, int((time.perf_counter() - started) * 1000))
