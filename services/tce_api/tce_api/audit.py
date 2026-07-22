from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_session_factory
from .models import AuditLog

logger = logging.getLogger(__name__)


def _write_audit_sync(
    consumer: str,
    action: str,
    query: dict[str, Any],
    result_event_ids: list[UUID],
    policy_decisions: dict[str, Any],
    latency_ms: int,
    *,
    raise_on_error: bool = False,
) -> None:
    """Write audit log in a background thread with its own DB session."""
    db: Session | None = None
    try:
        db = get_session_factory()()
        log = AuditLog(
            ts=datetime.now(tz=UTC),
            consumer=consumer,
            action=action,
            query=query,
            result_event_ids=result_event_ids,
            policy_decisions=policy_decisions,
            latency_ms=latency_ms,
        )
        db.add(log)
        db.commit()
    except Exception:
        logger.warning("audit log write failed", exc_info=True)
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
        if raise_on_error:
            raise
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def write_audit_log(
    db: Session,
    consumer: str,
    action: str,
    query: dict[str, Any],
    result_event_ids: list[UUID],
    policy_decisions: dict[str, Any],
    latency_ms: int,
) -> None:
    """Write asynchronously by default, or fail closed in durable mode."""
    mode = str(getattr(get_settings(), "audit_write_mode", "async") or "async").strip().lower()
    if mode == "durable":
        _write_audit_sync(
            consumer,
            action,
            query,
            result_event_ids,
            policy_decisions,
            latency_ms,
            raise_on_error=True,
        )
        return
    t = threading.Thread(
        target=_write_audit_sync,
        args=(consumer, action, query, result_event_ids, policy_decisions, latency_ms),
        daemon=True,
    )
    t.start()
