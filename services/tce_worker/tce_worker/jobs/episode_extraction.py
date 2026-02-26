from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from tce_model_gateway import create_gateway

from ..config import get_settings
from ..db import SessionLocal


def _authority_score(level: str | None) -> float:
    normalized = str(level or "incidental").strip().lower()
    return {
        "policy": 1.0,
        "standard": 0.9,
        "preferred": 0.75,
        "observed": 0.5,
        "incidental": 0.2,
    }.get(normalized, 0.2)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def _heuristic_extract(event_row: dict[str, Any]) -> dict[str, Any]:
    payload = _as_dict(event_row.get("payload"))
    context = _as_dict(event_row.get("context"))
    decision = _as_dict(event_row.get("decision"))
    outcome = _as_dict(event_row.get("outcome"))
    goal = str(payload.get("goal") or event_row.get("title") or event_row.get("task_type") or "Untitled goal")
    return {
        "goal": goal[:240],
        "context": str(payload.get("summary") or payload.get("context") or "")[:1200],
        "decision": str(decision.get("choice") or decision.get("rationale") or ""),
        "alternatives": decision.get("alternatives") if isinstance(decision.get("alternatives"), list) else [],
        "constraints": payload.get("constraints") if isinstance(payload.get("constraints"), list) else [],
        "lessons": {
            "do_more": payload.get("do_more") if isinstance(payload.get("do_more"), list) else [],
            "do_less": payload.get("do_less") if isinstance(payload.get("do_less"), list) else [],
            "avoid": payload.get("avoid") if isinstance(payload.get("avoid"), list) else [],
        },
        "session_id": str(context.get("session_id") or "default"),
        "outcome": str(outcome.get("result") or ""),
    }


def _llm_extract(gateway: Any, event_row: dict[str, Any]) -> dict[str, Any] | None:
    payload = _as_dict(event_row.get("payload"))
    prompt = (
        "Convert this event into an episode JSON object with keys: "
        "goal, context, decision, alternatives[], constraints[], "
        "lessons{do_more[],do_less[],avoid[]}. "
        "Keep concise and faithful.\n"
        f"event_title={event_row.get('title','')}\n"
        f"event_type={event_row.get('event_type','')}\n"
        f"domain={event_row.get('domain','')}\n"
        f"task_type={event_row.get('task_type','')}\n"
        f"payload={json.dumps(payload, default=str)[:3000]}"
    )
    try:
        if hasattr(gateway, "extract_structured"):
            result = gateway.extract_structured(prompt=prompt, schema_name="tce_episode_extraction_v1")
            if isinstance(result, dict):
                return result
    except Exception:
        return None
    return None


def run(event_id: str, workspace_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    parsed_id = UUID(event_id)
    with SessionLocal() as db:
        event_row = db.execute(
            text(
                """
                SELECT id, ts, title, domain, task_type, event_type, payload, context, decision, outcome, authority_level
                FROM events
                WHERE id = :event_id
                """
            ),
            {"event_id": parsed_id},
        ).mappings().first()
        if not event_row:
            return {"status": "missing", "event_id": event_id}

        context = _as_dict(event_row.get("context"))
        scoped_workspace = workspace_id or str(context.get("_tce_workspace") or "")
        scoped_user = user_id or str(context.get("_tce_owner") or "")
        if not scoped_workspace or not scoped_user:
            return {"status": "skipped", "event_id": event_id, "reason": "missing_scope"}

        extracted = _heuristic_extract(dict(event_row))
        if settings.episode_extraction_model_mode == "llm_first":
            try:
                gateway = create_gateway(settings)
                llm_result = _llm_extract(gateway, dict(event_row))
                if llm_result:
                    extracted.update(
                        {
                            "goal": str(llm_result.get("goal") or extracted["goal"])[:240],
                            "context": str(llm_result.get("context") or extracted["context"])[:1200],
                            "decision": str(llm_result.get("decision") or extracted["decision"])[:500],
                            "alternatives": list(llm_result.get("alternatives") or extracted["alternatives"]),
                            "constraints": list(llm_result.get("constraints") or extracted["constraints"]),
                            "lessons": llm_result.get("lessons") if isinstance(llm_result.get("lessons"), dict) else extracted["lessons"],
                        }
                    )
            except Exception:
                pass

        now = datetime.now(tz=UTC)
        session_id = str(extracted.get("session_id") or "default")
        goal = str(extracted.get("goal") or "Untitled goal")[:240]
        existing = db.execute(
            text(
                """
                SELECT id
                FROM episodes
                WHERE workspace_id = :workspace_id
                  AND user_id = :user_id
                  AND session_id = :session_id
                  AND goal = :goal
                  AND status IN ('open', 'in_progress')
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {
                "workspace_id": scoped_workspace,
                "user_id": scoped_user,
                "session_id": session_id,
                "goal": goal,
            },
        ).mappings().first()

        authority_score = _authority_score(event_row.get("authority_level"))
        if existing is None:
            episode_id = uuid.uuid4()
            db.execute(
                text(
                    """
                    INSERT INTO episodes (
                        id, workspace_id, user_id, session_id, goal, context, outcome,
                        confidence, status, authority_score, stability_score, created_at, updated_at
                    )
                    VALUES (
                        :id, :workspace_id, :user_id, :session_id, :goal, :context, :outcome,
                        :confidence, 'open', :authority_score, :stability_score, :created_at, :updated_at
                    )
                    """
                ),
                {
                    "id": episode_id,
                    "workspace_id": scoped_workspace,
                    "user_id": scoped_user,
                    "session_id": session_id,
                    "goal": goal,
                    "context": str(extracted.get("context") or "")[:1200],
                    "outcome": str(extracted.get("outcome") or "")[:300],
                    "confidence": max(0.0, min(1.0, 0.5 + (authority_score * 0.3))),
                    "authority_score": authority_score,
                    "stability_score": 0.2,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        else:
            episode_id = UUID(str(existing["id"]))
            db.execute(
                text(
                    """
                    UPDATE episodes
                    SET context = COALESCE(NULLIF(:context, ''), context),
                        outcome = COALESCE(NULLIF(:outcome, ''), outcome),
                        authority_score = GREATEST(authority_score, :authority_score),
                        updated_at = :updated_at
                    WHERE id = :episode_id
                    """
                ),
                {
                    "episode_id": episode_id,
                    "context": str(extracted.get("context") or "")[:1200],
                    "outcome": str(extracted.get("outcome") or "")[:300],
                    "authority_score": authority_score,
                    "updated_at": now,
                },
            )

        db.execute(
            text(
                """
                INSERT INTO episode_event_links (id, episode_id, event_id, created_at)
                VALUES (:id, :episode_id, :event_id, :created_at)
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": uuid.uuid4(), "episode_id": episode_id, "event_id": parsed_id, "created_at": now},
        )

        if extracted.get("decision"):
            db.execute(
                text(
                    """
                    INSERT INTO episode_decisions (id, episode_id, decision, why, alternatives_json, created_at)
                    VALUES (:id, :episode_id, :decision, :why, CAST(:alternatives_json AS jsonb), :created_at)
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "episode_id": episode_id,
                    "decision": str(extracted["decision"])[:500],
                    "why": "episode_extraction",
                    "alternatives_json": json.dumps(list(extracted.get("alternatives") or [])),
                    "created_at": now,
                },
            )

        lessons = extracted.get("lessons") if isinstance(extracted.get("lessons"), dict) else {}
        db.execute(
            text(
                """
                INSERT INTO episode_lessons (episode_id, do_more_json, do_less_json, avoid_json, updated_at)
                VALUES (
                    :episode_id,
                    CAST(:do_more_json AS jsonb),
                    CAST(:do_less_json AS jsonb),
                    CAST(:avoid_json AS jsonb),
                    :updated_at
                )
                ON CONFLICT (episode_id) DO UPDATE SET
                    do_more_json = excluded.do_more_json,
                    do_less_json = excluded.do_less_json,
                    avoid_json = excluded.avoid_json,
                    updated_at = excluded.updated_at
                """
            ),
            {
                "episode_id": episode_id,
                "do_more_json": json.dumps(list(lessons.get("do_more") or [])),
                "do_less_json": json.dumps(list(lessons.get("do_less") or [])),
                "avoid_json": json.dumps(list(lessons.get("avoid") or [])),
                "updated_at": now,
            },
        )

        db.commit()
        return {
            "status": "ok",
            "event_id": event_id,
            "episode_id": str(episode_id),
            "workspace_id": scoped_workspace,
            "user_id": scoped_user,
        }

