from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from tce_model_gateway import create_gateway

from ..config import get_settings
from ..db import SessionLocal
from ..models import Event, EventEmbedding
from .qdrant_sync import sync_event_embedding


def _embed_text(event: Event) -> str:
    parts = [event.title]
    parts.append(str(event.payload.get("summary", "")))
    if event.decision:
        parts.append(str(event.decision.get("rationale", "")))
    for step in event.steps[:5]:
        if isinstance(step, dict) and step.get("description"):
            parts.append(step["description"])
    return "\n".join(part for part in parts if part)


def run(event_id: str) -> dict:
    settings = get_settings()
    gateway = create_gateway(settings)

    parsed_id = UUID(event_id)
    with SessionLocal() as db:
        event = db.execute(select(Event).where(Event.id == parsed_id)).scalar_one_or_none()
        if event is None:
            return {"status": "missing", "event_id": event_id}

        embedding = gateway.embed(_embed_text(event))
        existing = db.execute(
            select(EventEmbedding).where(EventEmbedding.event_id == parsed_id)
        ).scalar_one_or_none()
        if existing:
            existing.embedding = embedding
            existing.model = settings.embed_model
            existing.created_at = datetime.now(tz=UTC)
        else:
            db.add(
                EventEmbedding(
                    event_id=parsed_id,
                    embedding=embedding,
                    model=settings.embed_model,
                    created_at=datetime.now(tz=UTC),
                )
            )
        db.commit()
    try:
        sync_event_embedding(str(parsed_id))
    except Exception:
        pass
    return {"status": "ok", "event_id": event_id, "dimensions": len(embedding)}
