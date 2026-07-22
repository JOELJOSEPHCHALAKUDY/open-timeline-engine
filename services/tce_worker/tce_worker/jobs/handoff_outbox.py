from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from tce_api.continuity_store import deliver_handoff_safely
from tce_api.db import get_session_factory

from ..config import get_settings


def run(outbox_id: str | None = None, batch_size: int | None = None) -> dict[str, int]:
    settings = get_settings()
    session_factory = get_session_factory()
    delivered = 0
    pending = 0
    dead = 0
    with session_factory() as db:
        if outbox_id:
            ids = [UUID(str(outbox_id))]
        else:
            rows = db.execute(
                text(
                    """
                    SELECT id FROM handoff_outbox
                    WHERE status = 'pending' AND next_attempt_at <= :now
                    ORDER BY created_at ASC
                    LIMIT :batch_size
                    """
                ),
                {
                    "now": datetime.now(tz=UTC),
                    "batch_size": max(1, min(500, int(batch_size or settings.handoff_outbox_batch_size))),
                },
            ).scalars().all()
            ids = [UUID(str(value)) for value in rows]
    for item_id in ids:
        with session_factory() as db:
            row = deliver_handoff_safely(
                db,
                outbox_id=item_id,
                retention_days=int(settings.handoff_retention_days),
            )
            if row.status == "delivered":
                delivered += 1
            elif row.status == "dead":
                dead += 1
            else:
                pending += 1
    return {"processed": len(ids), "delivered": delivered, "pending": pending, "dead": dead}
