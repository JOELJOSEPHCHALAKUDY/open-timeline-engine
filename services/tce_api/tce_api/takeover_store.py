from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_shared.events import (
    AutonomyPolicyProfile,
    SafetyDecision,
    TakeoverClassification,
    TakeoverMode,
    TakeoverState,
)
from tce_shared.takeover import persona_defaults

from .config import get_settings


def _row_to_state(
    row: Any,
    session_id: str,
    workspace_id: str,
    user_id: str,
    persona_mode: str,
    activation_keywords: str | None,
    stop_keywords: str | None,
) -> TakeoverState:
    default_activation, default_stop = persona_defaults(persona_mode)
    resolved_activation = activation_keywords if activation_keywords is not None else default_activation
    resolved_stop = stop_keywords if stop_keywords is not None else default_stop
    if row is None:
        now = datetime.now(tz=UTC)
        return TakeoverState(
            session_id=session_id,
            workspace_id=workspace_id,
            user_id=user_id,
            active=False,
            mode=TakeoverMode.TAKEOVER,
            persona_mode=persona_mode,
            activation_keywords=resolved_activation,
            stop_keywords=resolved_stop,
            takeover_context={},
            objective_hash=None,
            working_set_json={},
            last_deliberation_at=None,
            recent_outcomes_json=[],
            autonomy_score=0.5,
            enforcement_mode="strict_takeover",
            last_tick_at=None,
            pending_directive_count=0,
            retry_backlog_count=0,
            last_safety_decision=SafetyDecision.ALLOW,
            updated_at=now,
        )
    row_context = row.takeover_context
    parsed_context: dict[str, Any]
    if isinstance(row_context, dict):
        parsed_context = row_context
    elif isinstance(row_context, str):
        try:
            loaded = json.loads(row_context)
            parsed_context = loaded if isinstance(loaded, dict) else {}
        except Exception:
            parsed_context = {}
    else:
        parsed_context = {}

    row_working_set = getattr(row, "working_set_json", {})
    parsed_working_set: dict[str, Any]
    if isinstance(row_working_set, dict):
        parsed_working_set = row_working_set
    elif isinstance(row_working_set, str):
        try:
            loaded_working_set = json.loads(row_working_set)
            parsed_working_set = loaded_working_set if isinstance(loaded_working_set, dict) else {}
        except Exception:
            parsed_working_set = {}
    else:
        parsed_working_set = {}

    row_recent_outcomes = getattr(row, "recent_outcomes_json", [])
    parsed_recent_outcomes: list[dict[str, Any]]
    if isinstance(row_recent_outcomes, list):
        parsed_recent_outcomes = [item for item in row_recent_outcomes if isinstance(item, dict)]
    elif isinstance(row_recent_outcomes, str):
        try:
            loaded_recent_outcomes = json.loads(row_recent_outcomes)
            if isinstance(loaded_recent_outcomes, list):
                parsed_recent_outcomes = [item for item in loaded_recent_outcomes if isinstance(item, dict)]
            else:
                parsed_recent_outcomes = []
        except Exception:
            parsed_recent_outcomes = []
    else:
        parsed_recent_outcomes = []

    return TakeoverState(
        session_id=row.session_id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        active=bool(row.active),
        mode=TakeoverMode(str(row.mode)),
        persona_mode=str(row.persona_mode),
        activation_keywords=str(row.activation_keywords),
        stop_keywords=str(row.stop_keywords),
        expires_at=row.expires_at,
        activated_at=row.activated_at,
        last_message_at=row.last_message_at,
        takeover_context=parsed_context,
        objective_hash=getattr(row, "objective_hash", None),
        working_set_json=parsed_working_set,
        last_deliberation_at=getattr(row, "last_deliberation_at", None),
        recent_outcomes_json=parsed_recent_outcomes,
        autonomy_score=float(getattr(row, "autonomy_score", 0.5) or 0.5),
        autonomy_policy_profile=AutonomyPolicyProfile(
            str(getattr(row, "autonomy_policy_profile", "human_consultative") or "human_consultative")
        ),
        active_goal_id=getattr(row, "active_goal_id", None),
        goal_queue_size=int(getattr(row, "goal_queue_size", 0) or 0),
        last_discovery_at=getattr(row, "last_discovery_at", None),
        continuity_violation_count=int(getattr(row, "continuity_violation_count", 0) or 0),
        enforcement_mode=str(getattr(row, "enforcement_mode", "strict_takeover") or "strict_takeover"),
        last_tick_at=getattr(row, "last_tick_at", None),
        pending_directive_count=int(getattr(row, "pending_directive_count", 0) or 0),
        retry_backlog_count=int(getattr(row, "retry_backlog_count", 0) or 0),
        last_classification=TakeoverClassification(str(row.last_classification))
        if row.last_classification
        else None,
        last_safety_decision=SafetyDecision(str(row.last_safety_decision)),
        updated_at=row.updated_at,
    )


def load_takeover_state(
    db: Session,
    session_id: str,
    workspace_id: str,
    user_id: str,
    persona_mode: str,
    activation_keywords: str | None,
    stop_keywords: str | None,
) -> TakeoverState:
    try:
        row = db.execute(
            text(
                """
                SELECT session_id, workspace_id, user_id, active, mode, persona_mode,
                       activation_keywords, stop_keywords, expires_at, activated_at,
                       last_message_at, takeover_context, objective_hash, working_set_json,
                       last_deliberation_at, recent_outcomes_json, autonomy_score,
                       autonomy_policy_profile, active_goal_id, goal_queue_size,
                       last_discovery_at, continuity_violation_count, enforcement_mode,
                       last_tick_at, pending_directive_count, retry_backlog_count,
                       last_classification, last_safety_decision, updated_at
                FROM takeover_sessions
                WHERE session_id = :session_id AND workspace_id = :workspace_id AND user_id = :user_id
                """
            ),
            {"session_id": session_id, "workspace_id": workspace_id, "user_id": user_id},
        ).first()
    except Exception:
        row = db.execute(
            text(
                """
                SELECT session_id, workspace_id, user_id, active, mode, persona_mode,
                       activation_keywords, stop_keywords, expires_at, activated_at,
                       last_message_at, takeover_context, last_classification,
                       last_safety_decision, updated_at
                FROM takeover_sessions
                WHERE session_id = :session_id AND workspace_id = :workspace_id AND user_id = :user_id
                """
            ),
            {"session_id": session_id, "workspace_id": workspace_id, "user_id": user_id},
        ).first()
    return _row_to_state(
        row=row,
        session_id=session_id,
        workspace_id=workspace_id,
        user_id=user_id,
        persona_mode=persona_mode,
        activation_keywords=activation_keywords,
        stop_keywords=stop_keywords,
    )


def save_takeover_state(db: Session, state: TakeoverState) -> TakeoverState:
    stmt = text(
        """
        INSERT INTO takeover_sessions(
            session_id, workspace_id, user_id, active, mode, persona_mode,
            activation_keywords, stop_keywords, expires_at, activated_at,
            last_message_at, takeover_context, objective_hash, working_set_json,
            last_deliberation_at, recent_outcomes_json, autonomy_score,
            autonomy_policy_profile, active_goal_id, goal_queue_size, last_discovery_at,
            continuity_violation_count, enforcement_mode, last_tick_at,
            pending_directive_count, retry_backlog_count,
            last_classification, last_safety_decision, updated_at
        )
        VALUES(
            :session_id, :workspace_id, :user_id, :active, :mode, :persona_mode,
            :activation_keywords, :stop_keywords, :expires_at, :activated_at,
            :last_message_at, :takeover_context, :objective_hash, :working_set_json,
            :last_deliberation_at, :recent_outcomes_json, :autonomy_score,
            :autonomy_policy_profile, :active_goal_id, :goal_queue_size, :last_discovery_at,
            :continuity_violation_count, :enforcement_mode, :last_tick_at,
            :pending_directive_count, :retry_backlog_count,
            :last_classification, :last_safety_decision, :updated_at
        )
        ON CONFLICT(session_id, workspace_id, user_id)
        DO UPDATE SET
            active = EXCLUDED.active,
            mode = EXCLUDED.mode,
            persona_mode = EXCLUDED.persona_mode,
            activation_keywords = EXCLUDED.activation_keywords,
            stop_keywords = EXCLUDED.stop_keywords,
            expires_at = EXCLUDED.expires_at,
            activated_at = EXCLUDED.activated_at,
            last_message_at = EXCLUDED.last_message_at,
            takeover_context = EXCLUDED.takeover_context,
            objective_hash = EXCLUDED.objective_hash,
            working_set_json = EXCLUDED.working_set_json,
            last_deliberation_at = EXCLUDED.last_deliberation_at,
            recent_outcomes_json = EXCLUDED.recent_outcomes_json,
            autonomy_score = EXCLUDED.autonomy_score,
            autonomy_policy_profile = EXCLUDED.autonomy_policy_profile,
            active_goal_id = EXCLUDED.active_goal_id,
            goal_queue_size = EXCLUDED.goal_queue_size,
            last_discovery_at = EXCLUDED.last_discovery_at,
            continuity_violation_count = EXCLUDED.continuity_violation_count,
            enforcement_mode = EXCLUDED.enforcement_mode,
            last_tick_at = EXCLUDED.last_tick_at,
            pending_directive_count = EXCLUDED.pending_directive_count,
            retry_backlog_count = EXCLUDED.retry_backlog_count,
            last_classification = EXCLUDED.last_classification,
            last_safety_decision = EXCLUDED.last_safety_decision,
            updated_at = EXCLUDED.updated_at
        """
    )
    params = {
        "session_id": state.session_id,
        "workspace_id": state.workspace_id,
        "user_id": state.user_id,
        "active": state.active,
        "mode": state.mode.value,
        "persona_mode": state.persona_mode,
        "activation_keywords": state.activation_keywords,
        "stop_keywords": state.stop_keywords,
        "expires_at": state.expires_at,
        "activated_at": state.activated_at,
        "last_message_at": state.last_message_at,
        "takeover_context": json.dumps(state.takeover_context),
        "objective_hash": state.objective_hash,
        "working_set_json": json.dumps(state.working_set_json),
        "last_deliberation_at": state.last_deliberation_at,
        "recent_outcomes_json": json.dumps(state.recent_outcomes_json),
        "autonomy_score": float(state.autonomy_score),
        "autonomy_policy_profile": state.autonomy_policy_profile.value,
        "active_goal_id": state.active_goal_id,
        "goal_queue_size": int(state.goal_queue_size),
        "last_discovery_at": state.last_discovery_at,
        "continuity_violation_count": int(state.continuity_violation_count),
        "enforcement_mode": state.enforcement_mode,
        "last_tick_at": state.last_tick_at,
        "pending_directive_count": int(state.pending_directive_count),
        "retry_backlog_count": int(state.retry_backlog_count),
        "last_classification": state.last_classification.value if state.last_classification else None,
        "last_safety_decision": state.last_safety_decision.value,
        "updated_at": state.updated_at,
    }
    try:
        db.execute(stmt, params)
        db.commit()
    except Exception:
        db.rollback()
        legacy_stmt = text(
            """
            INSERT INTO takeover_sessions(
                session_id, workspace_id, user_id, active, mode, persona_mode,
                activation_keywords, stop_keywords, expires_at, activated_at,
                last_message_at, takeover_context, last_classification,
                last_safety_decision, updated_at
            )
            VALUES(
                :session_id, :workspace_id, :user_id, :active, :mode, :persona_mode,
                :activation_keywords, :stop_keywords, :expires_at, :activated_at,
                :last_message_at, :takeover_context, :last_classification,
                :last_safety_decision, :updated_at
            )
            ON CONFLICT(session_id, workspace_id, user_id)
            DO UPDATE SET
                active = EXCLUDED.active,
                mode = EXCLUDED.mode,
                persona_mode = EXCLUDED.persona_mode,
                activation_keywords = EXCLUDED.activation_keywords,
                stop_keywords = EXCLUDED.stop_keywords,
                expires_at = EXCLUDED.expires_at,
                activated_at = EXCLUDED.activated_at,
                last_message_at = EXCLUDED.last_message_at,
                takeover_context = EXCLUDED.takeover_context,
                last_classification = EXCLUDED.last_classification,
                last_safety_decision = EXCLUDED.last_safety_decision,
                updated_at = EXCLUDED.updated_at
            """
        )
        db.execute(legacy_stmt, params)
        db.commit()
    return state


def _snapshot_payload_from_state(state: TakeoverState, reason: str) -> dict[str, Any]:
    return {
        "reason": reason,
        "objective_hash": state.objective_hash,
        "takeover_context": state.takeover_context if isinstance(state.takeover_context, dict) else {},
        "working_set_json": state.working_set_json if isinstance(state.working_set_json, dict) else {},
        "recent_outcomes_json": (
            state.recent_outcomes_json if isinstance(state.recent_outcomes_json, list) else []
        ),
        "autonomy_score": float(state.autonomy_score),
        "saved_at": datetime.now(tz=UTC).isoformat(),
    }


def save_session_memory_snapshot(
    db: Session,
    *,
    state: TakeoverState,
    reason: str,
    max_per_session: int | None = None,
) -> str | None:
    objective_hash_value = str(state.objective_hash or "").strip()
    takeover_context = state.takeover_context if isinstance(state.takeover_context, dict) else {}
    working_set = state.working_set_json if isinstance(state.working_set_json, dict) else {}
    if not objective_hash_value or (not takeover_context and not working_set):
        return None
    snapshot_id = str(uuid.uuid4())
    payload = _snapshot_payload_from_state(state, reason=reason)
    db.execute(
        text(
            """
            INSERT INTO session_memory_snapshots(
                id, workspace_id, user_id, session_id, objective_hash, snapshot_json, created_at
            )
            VALUES(
                :id, :workspace_id, :user_id, :session_id, :objective_hash, CAST(:snapshot_json AS JSONB), :created_at
            )
            """
        ),
        {
            "id": snapshot_id,
            "workspace_id": state.workspace_id,
            "user_id": state.user_id,
            "session_id": state.session_id,
            "objective_hash": objective_hash_value,
            "snapshot_json": json.dumps(payload),
            "created_at": datetime.now(tz=UTC),
        },
    )
    keep_limit = max(1, int(max_per_session or get_settings().snapshot_max_per_session))
    db.execute(
        text(
            """
            DELETE FROM session_memory_snapshots
            WHERE id IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY workspace_id, user_id, session_id
                               ORDER BY created_at DESC
                           ) AS rn
                    FROM session_memory_snapshots
                    WHERE workspace_id = :workspace_id
                      AND user_id = :user_id
                      AND session_id = :session_id
                ) ranked
                WHERE rn > :keep_limit
            )
            """
        ),
        {
            "workspace_id": state.workspace_id,
            "user_id": state.user_id,
            "session_id": state.session_id,
            "keep_limit": keep_limit,
        },
    )
    return snapshot_id


def load_recent_session_memory_snapshot(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    session_id: str,
    objective_hash: str,
    max_age_days: int,
) -> dict[str, Any] | None:
    objective_hash_value = str(objective_hash or "").strip()
    if not objective_hash_value:
        return None
    cutoff = datetime.now(tz=UTC) - timedelta(days=max(1, int(max_age_days)))
    row = db.execute(
        text(
            """
            SELECT id, snapshot_json, created_at
            FROM session_memory_snapshots
            WHERE workspace_id = :workspace_id
              AND user_id = :user_id
              AND session_id = :session_id
              AND objective_hash = :objective_hash
              AND created_at >= :cutoff
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "session_id": session_id,
            "objective_hash": objective_hash_value,
            "cutoff": cutoff,
        },
    ).mappings().first()
    if row is None:
        return None
    raw_payload = row.get("snapshot_json")
    if isinstance(raw_payload, dict):
        payload = raw_payload
    elif isinstance(raw_payload, str):
        try:
            decoded = json.loads(raw_payload)
            payload = decoded if isinstance(decoded, dict) else {}
        except Exception:
            payload = {}
    else:
        payload = {}
    created_at = row.get("created_at")
    created_dt = created_at if isinstance(created_at, datetime) else datetime.now(tz=UTC)
    age_hours = int(max(0.0, (datetime.now(tz=UTC) - created_dt).total_seconds() / 3600.0))
    return {
        "snapshot_id": str(row.get("id")),
        "age_hours": age_hours,
        "payload": payload,
    }


def reset_takeover_state(
    db: Session,
    session_id: str,
    workspace_id: str,
    user_id: str,
    persona_mode: str,
    activation_keywords: str | None,
    stop_keywords: str | None,
) -> TakeoverState:
    existing = load_takeover_state(
        db=db,
        session_id=session_id,
        workspace_id=workspace_id,
        user_id=user_id,
        persona_mode=persona_mode,
        activation_keywords=activation_keywords,
        stop_keywords=stop_keywords,
    )
    if (
        str(existing.objective_hash or "").strip()
        and (
            (isinstance(existing.takeover_context, dict) and bool(existing.takeover_context))
            or (isinstance(existing.working_set_json, dict) and bool(existing.working_set_json))
        )
    ):
        save_session_memory_snapshot(db, state=existing, reason="takeover_reset")
    db.execute(
        text(
            """
            DELETE FROM takeover_sessions
            WHERE session_id = :session_id AND workspace_id = :workspace_id AND user_id = :user_id
            """
        ),
        {"session_id": session_id, "workspace_id": workspace_id, "user_id": user_id},
    )
    db.commit()
    return load_takeover_state(
        db=db,
        session_id=session_id,
        workspace_id=workspace_id,
        user_id=user_id,
        persona_mode=persona_mode,
        activation_keywords=activation_keywords,
        stop_keywords=stop_keywords,
    )


def record_takeover_action(
    db: Session,
    *,
    session_id: str,
    workspace_id: str,
    user_id: str,
    turn: int,
    objective_hash: str | None,
    action_kind: str,
    result: str,
    latency_ms: int,
    meta: dict[str, Any],
) -> None:
    try:
        db.execute(
            text(
                """
                INSERT INTO takeover_action_log(
                    id, session_id, workspace_id, user_id, turn, objective_hash,
                    action_kind, result, latency_ms, meta, ts
                )
                VALUES(
                    :id, :session_id, :workspace_id, :user_id, :turn, :objective_hash,
                    :action_kind, :result, :latency_ms, CAST(:meta AS JSONB), :ts
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "session_id": session_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "turn": max(0, int(turn)),
                "objective_hash": objective_hash,
                "action_kind": action_kind,
                "result": result,
                "latency_ms": max(0, int(latency_ms)),
                "meta": json.dumps(meta),
                "ts": datetime.now(tz=UTC),
            },
        )
        db.commit()
    except Exception:
        db.rollback()
