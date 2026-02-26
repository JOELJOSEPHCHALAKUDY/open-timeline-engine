from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from redis import Redis
from rq import Queue, Retry
from sqlalchemy import and_, desc, func, select, text
from tce_model_gateway import create_gateway

from ..confidence import confidence_status, recency_decay, weighted_confidence
from ..config import get_settings
from ..db import SessionLocal
from ..models import Event, Pattern

logger = logging.getLogger(__name__)


def _pattern_feedback_signal(db, *, event_ids: list) -> float:
    if not event_ids:
        return 0.5
    event_id_values = [str(value) for value in event_ids if value is not None]
    if not event_id_values:
        return 0.5
    try:
        feedback_row = db.execute(
            text(
                """
                SELECT
                  SUM(
                    CASE
                      WHEN LOWER(COALESCE(cf.feedback_type, '')) IN ('helpful', 'positive', 'correct') THEN 1
                      ELSE 0
                    END
                  ) AS positive_feedback,
                  SUM(
                    CASE
                      WHEN LOWER(COALESCE(cf.feedback_type, '')) IN ('unhelpful', 'negative', 'wrong') THEN 1
                      ELSE 0
                    END
                  ) AS negative_feedback
                FROM decision_observations dobs
                JOIN clone_feedback cf ON cf.observation_id = dobs.id
                CROSS JOIN LATERAL unnest(dobs.source_event_ids) AS source_event_id
                WHERE source_event_id = ANY(CAST(:event_ids AS UUID[]))
                """
            ),
            {"event_ids": event_id_values},
        ).mappings().first()
        outcome_row = db.execute(
            text(
                """
                SELECT
                  SUM(
                    CASE
                      WHEN LOWER(COALESCE(dobs.outcome, '')) IN ('success', 'succeeded', 'completed', 'done') THEN 1
                      ELSE 0
                    END
                  ) AS positive_outcome,
                  SUM(
                    CASE
                      WHEN LOWER(COALESCE(dobs.outcome, '')) IN ('failure', 'failed', 'blocked', 'aborted', 'error', 'timeout') THEN 1
                      ELSE 0
                    END
                  ) AS negative_outcome
                FROM decision_observations dobs
                CROSS JOIN LATERAL unnest(dobs.source_event_ids) AS source_event_id
                WHERE source_event_id = ANY(CAST(:event_ids AS UUID[]))
                """
            ),
            {"event_ids": event_id_values},
        ).mappings().first()
    except Exception:
        return 0.5

    positive_feedback = float((feedback_row or {}).get("positive_feedback") or 0.0)
    negative_feedback = float((feedback_row or {}).get("negative_feedback") or 0.0)
    positive_outcome = float((outcome_row or {}).get("positive_outcome") or 0.0)
    negative_outcome = float((outcome_row or {}).get("negative_outcome") or 0.0)

    weighted_positive = positive_feedback + (0.6 * positive_outcome)
    weighted_negative = negative_feedback + (0.8 * negative_outcome)
    total = weighted_positive + weighted_negative
    if total <= 0:
        return 0.5
    raw = (weighted_positive - weighted_negative) / total
    return float(max(0.0, min(1.0, (raw + 1.0) / 2.0)))


def run(domain: str, window_days: int = 30) -> dict:
    now = datetime.now(tz=UTC)
    start = now - timedelta(days=window_days)
    settings = get_settings()
    gateway = create_gateway(settings)
    queue_name = getattr(settings, "queue_patterns_name", "tce-default")
    queue = Queue(queue_name, connection=Redis.from_url(settings.redis_url))

    with SessionLocal() as db:
        rows = db.execute(
            select(Event.task_type, func.array_agg(Event.id), func.count(Event.id), func.max(Event.ts))
            .where(and_(Event.domain == domain, Event.ts >= start, Event.ts <= now, Event.sensitivity <= 2))
            .group_by(Event.task_type)
            .order_by(desc(func.count(Event.id)))
            .limit(20)
        ).all()

        generated = 0
        updated = 0
        skipped_low_support = 0
        skipped_low_confidence = 0
        created_pattern_ids: list[str] = []
        feedback_cache: dict[str, float] = {}

        for task_type, event_ids, frequency_count, latest_ts in rows:
            if int(frequency_count) < settings.pattern_min_frequency:
                skipped_low_support += 1
                continue

            frequency = min(1.0, float(frequency_count) / 20.0)
            recency = recency_decay(latest_ts)
            consistency = min(1.0, 0.35 + (float(frequency_count) / 20.0))
            cache_key = ",".join(sorted(str(value) for value in (event_ids or [])))
            feedback_signal = feedback_cache.get(cache_key)
            if feedback_signal is None:
                feedback_signal = _pattern_feedback_signal(db, event_ids=list(event_ids or []))
                feedback_cache[cache_key] = feedback_signal
            confidence = weighted_confidence(
                frequency,
                recency,
                consistency,
                feedback_signal,
                weights=settings.confidence_weights,
            )
            if confidence < settings.pattern_min_confidence:
                skipped_low_confidence += 1
                continue

            status = confidence_status(confidence)
            if status == "suppressed":
                skipped_low_confidence += 1
                continue

            statement = f"In {domain}, recurring workflow starts with {task_type}"
            try:
                extraction = gateway.extract_structured(
                    prompt=(
                        "Generate a concise user workflow pattern statement. "
                        f"domain={domain} task_type={task_type} count={frequency_count}"
                    ),
                    schema_name="pattern_statement_v1",
                )
                if isinstance(extraction, dict) and extraction.get("statement"):
                    statement = str(extraction["statement"]).strip() or statement
            except Exception as exc:
                logger.warning("pattern extraction fallback to deterministic statement: %s", exc)

            existing = db.execute(
                select(Pattern)
                .where(
                    Pattern.domain == domain,
                    Pattern.pattern_type == "workflow",
                    Pattern.statement == statement,
                )
                .order_by(desc(Pattern.updated_at))
                .limit(1)
            ).scalar_one_or_none()

            if existing:
                merged_ids = list(dict.fromkeys([*existing.evidence_event_ids, *event_ids]))[:200]
                existing.evidence_event_ids = merged_ids
                existing.confidence = max(confidence, (existing.confidence * 0.6) + (confidence * 0.4))
                existing.updated_at = now
                existing.status = "active" if existing.confidence >= 0.8 else "needs_review"
                db.flush()
                created_pattern_ids.append(str(existing.id))
                updated += 1
                continue

            pattern = Pattern(
                domain=domain,
                pattern_type="workflow",
                statement=statement,
                evidence_event_ids=event_ids,
                confidence=confidence,
                updated_at=now,
                version=1,
                status="active" if status == "active" else "needs_review",
            )
            db.add(pattern)
            db.flush()
            created_pattern_ids.append(str(pattern.id))
            generated += 1

        db.commit()

    for pattern_id in sorted(set(created_pattern_ids)):
        retry = Retry(max=settings.worker_retry_max, interval=settings.retry_intervals)
        queue.enqueue("tce_worker.jobs.validation.run", pattern_id, retry=retry)
        queue.enqueue("tce_worker.jobs.workflow.run", pattern_id, retry=retry)

    return {
        "status": "ok",
        "domain": domain,
        "generated": generated,
        "updated": updated,
        "skipped_low_support": skipped_low_support,
        "skipped_low_confidence": skipped_low_confidence,
    }
