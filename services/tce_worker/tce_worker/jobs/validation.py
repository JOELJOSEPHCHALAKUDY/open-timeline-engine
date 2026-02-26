from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from ..confidence import recency_decay
from ..db import SessionLocal
from ..models import Pattern


def run(pattern_id: str) -> dict:
    parsed_id = UUID(pattern_id)
    with SessionLocal() as db:
        pattern = db.execute(select(Pattern).where(Pattern.id == parsed_id)).scalar_one_or_none()
        if pattern is None:
            return {"status": "missing", "pattern_id": pattern_id}

        decay = recency_decay(pattern.updated_at, half_life_days=90)
        pattern.confidence = max(0.0, min(1.0, pattern.confidence * decay))

        if pattern.confidence < 0.5:
            pattern.status = "suppressed"
        elif pattern.confidence < 0.8:
            pattern.status = "needs_review"
        else:
            pattern.status = "active"

        pattern.updated_at = datetime.now(tz=UTC)
        db.commit()
    return {"status": "ok", "pattern_id": pattern_id, "pattern_status": pattern.status}
