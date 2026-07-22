from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_model_gateway import get_gateway
from tce_shared.situation import SITUATION_TYPES, classify_situation

from .cache_clients import get_redis_client
from .config import get_settings

logger = logging.getLogger(__name__)
_OBS_EMBED_CACHE_TTL = 600
_DECISION_OBS_SCHEMA_READY = False
_SITUATION_ALIAS_MAP: dict[str, str] = {
    "restart_safety": "escalation_point",
    "uncertainty": "unknown_territory",
    "fallback_reliability": "error_occurred",
    "provider_selection": "choice_required",
    "decision_required": "choice_required",
    "goal_conflict": "conflict_detected",
    "setup_configuration": "routine_task",
    "opportunity_detected": "prioritization_needed",
    "planning": "prioritization_needed",
    "risk_assessment": "escalation_point",
}


def _ensure_decision_observation_schema(db: Session) -> None:
    global _DECISION_OBS_SCHEMA_READY
    if _DECISION_OBS_SCHEMA_READY:
        return
    try:
        db.execute(text("ALTER TABLE decision_observations ADD COLUMN IF NOT EXISTS superseded_by UUID"))
        db.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_decision_observations_superseded
                ON decision_observations (workspace_id, superseded_by, ts DESC)
                """
            )
        )
        _DECISION_OBS_SCHEMA_READY = True
    except Exception:
        logger.warning("failed to ensure decision_observations superseded schema", exc_info=True)


def _canonical_situation_type(raw: str | None) -> str:
    token = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    valid = set(SITUATION_TYPES)
    if token in valid:
        return token
    if token in _SITUATION_ALIAS_MAP:
        mapped = _SITUATION_ALIAS_MAP[token]
        if mapped in valid:
            return mapped
    if token:
        settings = get_settings()
        inferred = classify_situation(
            token,
            semantic_enabled=bool(getattr(settings, "semantic_classifier_enabled", False)),
            semantic_threshold=float(getattr(settings, "semantic_classifier_situation_threshold", 0.61)),
            semantic_margin=float(getattr(settings, "semantic_classifier_margin", 0.06)),
        )
        if inferred in valid:
            return inferred
    return "routine_task"


def _situation_candidates(raw: str | None) -> list[str]:
    canonical = _canonical_situation_type(raw)
    aliases = [key for key, value in _SITUATION_ALIAS_MAP.items() if value == canonical]
    return [canonical, *aliases]


def query_similar_observations(
    db: Session,
    consumer_id: str,
    workspace_id: str,
    subject_user_id: str,
    situation_type: str,
    situation_text: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Query promoted observations for one behavior subject.

    Executor identity is intentionally not part of this filter: multiple
    executors may contribute evidence for the same authorized human subject.
    """
    settings = get_settings()
    _ensure_decision_observation_schema(db)
    candidates = _situation_candidates(situation_type)
    rows = db.execute(
        text(
            """
            SELECT id, situation_type, situation_summary, context_snapshot,
                   user_response, response_reasoning, outcome, outcome_sentiment,
                   source_event_ids, confidence, ts
            FROM decision_observations
            WHERE workspace_id = :workspace_id
              AND subject_user_id = :subject_user_id
              AND situation_type = ANY(:situation_types)
              AND learning_eligible = true
              AND superseded_by IS NULL
              AND lifecycle_status = 'active'
              AND valid_from <= NOW()
              AND (valid_until IS NULL OR valid_until > NOW())
            ORDER BY ts DESC
            LIMIT :limit
            """
        ),
        {
            "workspace_id": workspace_id,
            "subject_user_id": subject_user_id,
            "situation_types": candidates,
            "limit": limit,
        },
    ).fetchall()
    results = [
        {
            "id": str(row[0]),
            "situation_type": row[1],
            "situation_summary": row[2],
            "context_snapshot": row[3] or {},
            "user_response": row[4],
            "response_reasoning": row[5],
            "outcome": row[6],
            "outcome_sentiment": row[7],
            "source_event_ids": row[8] or [],
            "confidence": row[9],
            "ts": row[10].isoformat() if row[10] else None,
            "recall_source": "exact",
            "similarity": None,
        }
        for row in rows
    ]
    if (
        len(results) >= limit
        or not situation_text
        or not _should_use_semantic_fallback(
            db,
            workspace_id=workspace_id,
            exact_count=len(results),
            settings=settings,
        )
    ):
        return results[:limit]

    query_embedding = _get_observation_embedding(situation_text.strip(), settings=settings)
    if not query_embedding:
        return results[:limit]

    try:
        timeout_ms = max(1, int(getattr(settings, "obs_semantic_query_timeout_ms", 80)))
        with db.begin_nested():
            db.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
            vector_rows = db.execute(
                text(
                    """
                    SELECT id, situation_type, situation_summary, context_snapshot,
                           user_response, response_reasoning, outcome, outcome_sentiment,
                           source_event_ids, confidence, ts,
                           1 - (embedding <=> CAST(:query_embedding AS vector)) AS similarity
                    FROM decision_observations
                    WHERE workspace_id = :workspace_id
                      AND subject_user_id = :subject_user_id
                      AND learning_eligible = true
                      AND embedding IS NOT NULL
                      AND superseded_by IS NULL
                      AND lifecycle_status = 'active'
                      AND valid_from <= NOW()
                      AND (valid_until IS NULL OR valid_until > NOW())
                    ORDER BY embedding <=> CAST(:query_embedding AS vector)
                    LIMIT :limit
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "subject_user_id": subject_user_id,
                    "query_embedding": _embedding_as_vector_literal(query_embedding),
                    "limit": max(limit, settings.obs_semantic_top_k),
                },
            ).fetchall()
    except Exception as exc:
        logger.warning("semantic observation recall failed; using exact-only observations: %s", exc)
        return results[:limit]

    seen = {item["id"] for item in results}
    for row in vector_rows:
        row_id = str(row[0])
        if row_id in seen:
            continue
        results.append(
            {
                "id": row_id,
                "situation_type": row[1],
                "situation_summary": row[2],
                "context_snapshot": row[3] or {},
                "user_response": row[4],
                "response_reasoning": row[5],
                "outcome": row[6],
                "outcome_sentiment": row[7],
                "source_event_ids": row[8] or [],
                "confidence": row[9],
                "ts": row[10].isoformat() if row[10] else None,
                "recall_source": "semantic",
                "similarity": float(row[11]) if row[11] is not None else None,
            }
        )
        seen.add(row_id)
        if len(results) >= limit:
            break
    return results[:limit]


def _should_use_semantic_fallback(
    db: Session,
    *,
    workspace_id: str,
    exact_count: int,
    settings: Any,
) -> bool:
    _ensure_decision_observation_schema(db)
    if exact_count >= settings.obs_semantic_fallback_min_results:
        return False
    if settings.obs_semantic_enabled:
        return True
    if not settings.obs_semantic_autogate:
        return False
    try:
        obs_count = int(
            db.execute(
                text(
                    """
                    SELECT COUNT(1)
                    FROM decision_observations
                    WHERE workspace_id = :workspace_id
                      AND superseded_by IS NULL
                    """
                ),
                {"workspace_id": workspace_id},
            ).scalar()
            or 0
        )
    except Exception:
        return False
    return obs_count >= settings.obs_semantic_min_observations


def _embedding_as_vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in embedding) + "]"


def _get_cached_observation_embedding(query: str, settings: Any) -> list[float] | None:
    try:
        cache = get_redis_client(settings.redis_url)
        if cache is None:
            return None
        key = f"tce:obs-embed:{hashlib.sha256(query.encode('utf-8')).hexdigest()[:16]}"
        raw = cache.get(key)
        if raw:
            return json.loads(raw)
    except Exception:
        return None
    return None


def _set_cached_observation_embedding(query: str, embedding: list[float], settings: Any) -> None:
    try:
        cache = get_redis_client(settings.redis_url)
        if cache is None:
            return
        key = f"tce:obs-embed:{hashlib.sha256(query.encode('utf-8')).hexdigest()[:16]}"
        cache.setex(key, _OBS_EMBED_CACHE_TTL, json.dumps(embedding))
    except Exception:
        pass


def _get_observation_embedding(query: str, settings: Any) -> list[float] | None:
    cached = _get_cached_observation_embedding(query, settings=settings)
    if cached:
        return cached
    try:
        gateway = get_gateway(settings)
        if hasattr(gateway, "aembed"):
            embedding = asyncio.run(gateway.aembed(query))
        else:
            embedding = gateway.embed(query)
        if embedding:
            _set_cached_observation_embedding(query, embedding, settings=settings)
        return embedding
    except Exception as exc:
        logger.warning("observation embedding unavailable: %s", exc)
    return None


def _sentiment_polarity(value: str | None) -> int:
    text_value = str(value or "").strip().lower()
    if not text_value:
        return 0
    positive_tokens = (
        "positive",
        "success",
        "succeeded",
        "resolved",
        "fixed",
        "helpful",
        "approved",
        "allow",
        "completed",
        "done",
    )
    negative_tokens = (
        "negative",
        "fail",
        "failed",
        "error",
        "blocked",
        "unhelpful",
        "denied",
        "timeout",
        "abandon",
        "rollback",
    )
    if any(token in text_value for token in positive_tokens):
        return 1
    if any(token in text_value for token in negative_tokens):
        return -1
    return 0


def _observation_polarity(outcome: str | None, outcome_sentiment: str | None) -> int:
    sentiment_polarity = _sentiment_polarity(outcome_sentiment)
    if sentiment_polarity != 0:
        return sentiment_polarity
    return _sentiment_polarity(outcome)


def _mark_superseded_observations(
    db: Session,
    *,
    observation_id: UUID,
    workspace_id: str,
    consumer_id: str,
    situation_type: str,
    situation_summary: str,
    outcome: str | None,
    outcome_sentiment: str | None,
) -> None:
    new_polarity = _observation_polarity(outcome, outcome_sentiment)
    normalized_summary = " ".join(str(situation_summary or "").strip().lower().split())
    if new_polarity == 0 or not normalized_summary:
        return
    rows = db.execute(
        text(
            """
            SELECT id, outcome, outcome_sentiment
            FROM decision_observations
            WHERE workspace_id = :workspace_id
              AND consumer_id = :consumer_id
              AND situation_type = :situation_type
              AND superseded_by IS NULL
              AND id <> :observation_id
              AND lower(regexp_replace(situation_summary, '\\s+', ' ', 'g')) = :normalized_summary
            ORDER BY ts DESC
            LIMIT 50
            """
        ),
        {
            "workspace_id": workspace_id,
            "consumer_id": consumer_id,
            "situation_type": situation_type,
            "observation_id": observation_id,
            "normalized_summary": normalized_summary,
        },
    ).mappings().all()
    stale_ids = [
        row["id"]
        for row in rows
        if _observation_polarity(row.get("outcome"), row.get("outcome_sentiment")) == (new_polarity * -1)
    ]
    for stale_id in stale_ids:
        db.execute(
            text(
                """
                UPDATE decision_observations
                SET superseded_by = :observation_id
                WHERE id = :stale_id
                  AND superseded_by IS NULL
                """
            ),
            {"observation_id": observation_id, "stale_id": stale_id},
        )


def save_clone_feedback(
    db: Session,
    *,
    observation_id: UUID,
    session_id: str,
    feedback_type: str,
    correction_text: str | None = None,
) -> UUID:
    feedback_id = uuid.uuid4()
    db.execute(
        text(
            """
            INSERT INTO clone_feedback (id, observation_id, session_id, feedback_type, correction_text, ts)
            VALUES (:id, :observation_id, :session_id, :feedback_type, :correction_text, :ts)
            """
        ),
        {
            "id": feedback_id,
            "observation_id": observation_id,
            "session_id": session_id,
            "feedback_type": feedback_type,
            "correction_text": correction_text,
            "ts": datetime.now(tz=UTC),
        },
    )
    db.commit()
    return feedback_id


def load_fingerprint(db: Session, consumer_id: str, workspace_id: str) -> dict[str, Any] | None:
    """Load one exact behavioral profile key without workspace-wide fallback."""
    row = db.execute(
        text(
            """
            SELECT fingerprint
            FROM behavioral_fingerprints
            WHERE workspace_id = :workspace_id AND consumer_id = :consumer_id
            ORDER BY last_updated_at DESC
            LIMIT 1
            """
        ),
        {"consumer_id": consumer_id, "workspace_id": workspace_id},
    ).fetchone()
    if not row:
        return None
    return row[0] if isinstance(row[0], dict) else {}


def save_fingerprint(
    db: Session,
    consumer_id: str,
    workspace_id: str,
    fingerprint: dict[str, Any],
    observation_count: int,
) -> None:
    import json

    now = datetime.now(tz=UTC)
    db.execute(
        text(
            """
            INSERT INTO behavioral_fingerprints (consumer_id, workspace_id, fingerprint, observation_count, last_updated_at, created_at)
            VALUES (:consumer_id, :workspace_id, CAST(:fingerprint AS jsonb), :observation_count, :now, :now)
            ON CONFLICT (consumer_id, workspace_id)
            DO UPDATE SET fingerprint = CAST(:fingerprint AS jsonb), observation_count = :observation_count, last_updated_at = :now
            """
        ),
        {
            "consumer_id": consumer_id,
            "workspace_id": workspace_id,
            "fingerprint": json.dumps(fingerprint),
            "observation_count": observation_count,
            "now": now,
        },
    )
    db.commit()


def save_observation(db: Session, observation: dict[str, Any]) -> UUID:
    import json

    _ensure_decision_observation_schema(db)
    now = datetime.now(tz=UTC)
    normalized_situation_type = _canonical_situation_type(observation.get("situation_type"))
    normalized_summary = " ".join(str(observation.get("situation_summary", "")).strip().split())
    result = db.execute(
        text(
            """
            INSERT INTO decision_observations
                (consumer_id, workspace_id, ts, situation_type, situation_summary,
                 context_snapshot, user_response, response_reasoning, outcome,
                 outcome_sentiment, source_event_ids, confidence, superseded_by)
            VALUES
                (:consumer_id, :workspace_id, :ts, :situation_type, :situation_summary,
                 CAST(:context_snapshot AS jsonb), :user_response, :response_reasoning, :outcome,
                 :outcome_sentiment, :source_event_ids, :confidence, NULL)
            RETURNING id
            """
        ),
        {
            "consumer_id": observation["consumer_id"],
            "workspace_id": observation.get("workspace_id", "default"),
            "ts": now,
            "situation_type": normalized_situation_type,
            "situation_summary": normalized_summary,
            "context_snapshot": json.dumps(observation.get("context_snapshot", {})),
            "user_response": observation["user_response"],
            "response_reasoning": observation.get("response_reasoning"),
            "outcome": observation.get("outcome"),
            "outcome_sentiment": observation.get("outcome_sentiment"),
            "source_event_ids": observation.get("source_event_ids", []),
            "confidence": observation.get("confidence", 1.0),
        },
    )
    observation_id = result.scalar_one()
    _mark_superseded_observations(
        db,
        observation_id=observation_id,
        workspace_id=observation.get("workspace_id", "default"),
        consumer_id=observation["consumer_id"],
        situation_type=normalized_situation_type,
        situation_summary=normalized_summary,
        outcome=observation.get("outcome"),
        outcome_sentiment=observation.get("outcome_sentiment"),
    )
    db.commit()
    return observation_id


def build_session_context_from_state(takeover_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "objective": takeover_context.get("objective", "not set"),
        "turn_count": takeover_context.get("turn_count", 0),
        "turns": takeover_context.get("session_turns", []),
        "unresolved_threads": takeover_context.get("unresolved_threads", []),
    }
