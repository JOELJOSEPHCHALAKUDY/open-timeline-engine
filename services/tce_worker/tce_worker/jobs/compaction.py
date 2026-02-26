from __future__ import annotations

from datetime import UTC, datetime

from redis import Redis
from rq import Queue, Retry
from sqlalchemy import and_, desc, func, select

from ..config import get_settings
from ..db import SessionLocal
from ..models import Event, Pattern


def run(time_start: str, time_end: str) -> dict:
    start = datetime.fromisoformat(time_start)
    end = datetime.fromisoformat(time_end)
    if start >= end:
        return {"status": "invalid_window", "time_start": time_start, "time_end": time_end}

    settings = get_settings()
    queue_name = getattr(settings, "queue_patterns_name", "tce-default")
    queue = Queue(queue_name, connection=Redis.from_url(settings.redis_url))

    with SessionLocal() as db:
        domain_rows = db.execute(
            select(Event.domain, func.count(Event.id), func.max(Event.ts))
            .where(and_(Event.ts >= start, Event.ts <= end, Event.sensitivity <= 2))
            .group_by(Event.domain)
        ).all()

        generated = 0
        updated = 0
        pattern_ids: list[str] = []

        for domain, count, latest_ts in domain_rows:
            evidence_rows = db.execute(
                select(Event.id)
                .where(
                    and_(
                        Event.domain == domain,
                        Event.ts >= start,
                        Event.ts <= end,
                        Event.sensitivity <= 2,
                    )
                )
                .order_by(desc(Event.ts))
                .limit(100)
            ).all()
            evidence_event_ids = [row[0] for row in evidence_rows]

            statement = (
                f"Compaction summary ({start.date().isoformat()} to {end.date().isoformat()}): "
                f"{count} events in {domain}"
            )
            confidence = min(1.0, float(count) / 50.0)
            status = "active" if confidence >= 0.8 else "needs_review"

            existing = db.execute(
                select(Pattern)
                .where(
                    Pattern.domain == domain,
                    Pattern.pattern_type == "summary",
                    Pattern.statement == statement,
                )
                .order_by(desc(Pattern.updated_at))
                .limit(1)
            ).scalar_one_or_none()

            if existing:
                existing.evidence_event_ids = evidence_event_ids
                existing.confidence = max(existing.confidence, confidence)
                existing.status = status
                existing.updated_at = latest_ts or datetime.now(tz=UTC)
                existing.version += 1
                db.flush()
                pattern_ids.append(str(existing.id))
                updated += 1
                continue

            pattern = Pattern(
                domain=domain,
                pattern_type="summary",
                statement=statement,
                evidence_event_ids=evidence_event_ids,
                confidence=confidence,
                updated_at=latest_ts or datetime.now(tz=UTC),
                version=1,
                status=status,
            )
            db.add(pattern)
            db.flush()
            pattern_ids.append(str(pattern.id))
            generated += 1

        db.commit()

    retry = Retry(max=settings.worker_retry_max, interval=settings.retry_intervals)
    for pattern_id in sorted(set(pattern_ids)):
        queue.enqueue("tce_worker.jobs.validation.run", pattern_id, retry=retry)
        queue.enqueue("tce_worker.jobs.workflow.run", pattern_id, retry=retry)

    return {
        "status": "ok",
        "generated": generated,
        "updated": updated,
        "patterns": len(set(pattern_ids)),
        "time_start": time_start,
        "time_end": time_end,
    }
