from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from ..config import get_settings
from ..db import SessionLocal


def _compute_scope_status(
    db,
    *,
    workspace_id: str,
    owner_id: str,
    window_minutes: int,
    now: datetime,
) -> dict[str, Any]:
    window_start = now - timedelta(minutes=window_minutes)
    entity_total = int(
        db.execute(
            text(
                """
                SELECT COUNT(1)
                FROM entity_nodes
                WHERE workspace_id = :workspace_id
                  AND owner_id = :owner_id
                """
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id},
        ).scalar()
        or 0
    )
    link_total = int(
        db.execute(
            text(
                """
                SELECT COUNT(1)
                FROM event_entity_links
                WHERE workspace_id = :workspace_id
                  AND owner_id = :owner_id
                """
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id},
        ).scalar()
        or 0
    )
    searchable_entity_count = int(
        db.execute(
            text(
                """
                SELECT COUNT(DISTINCT en.id)
                FROM entity_nodes en
                JOIN event_entity_links eel ON eel.entity_id = en.id
                WHERE en.workspace_id = :workspace_id
                  AND en.owner_id = :owner_id
                  AND eel.workspace_id = :workspace_id
                  AND eel.owner_id = :owner_id
                """
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id},
        ).scalar()
        or 0
    )
    entity_created_window = int(
        db.execute(
            text(
                """
                SELECT COUNT(1)
                FROM entity_nodes
                WHERE workspace_id = :workspace_id
                  AND owner_id = :owner_id
                  AND created_at >= :window_start
                """
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id, "window_start": window_start},
        ).scalar()
        or 0
    )
    link_created_window = int(
        db.execute(
            text(
                """
                SELECT COUNT(1)
                FROM event_entity_links
                WHERE workspace_id = :workspace_id
                  AND owner_id = :owner_id
                  AND created_at >= :window_start
                """
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id, "window_start": window_start},
        ).scalar()
        or 0
    )
    setting_key = f"graph_health_status:{workspace_id}:{owner_id}"
    previous = db.execute(
        text("SELECT value FROM runtime_settings WHERE key = :key LIMIT 1"),
        {"key": setting_key},
    ).scalar()
    previous_payload = previous if isinstance(previous, dict) else {}
    if isinstance(previous, str):
        try:
            decoded = json.loads(previous)
            previous_payload = decoded if isinstance(decoded, dict) else {}
        except Exception:
            previous_payload = {}
    zero_since: str | None
    if searchable_entity_count <= 0:
        prior_zero_since = str(previous_payload.get("zero_entities_since") or "").strip()
        zero_since = prior_zero_since or now.isoformat()
    else:
        zero_since = None
    zero_minutes = 0.0
    if zero_since:
        try:
            zero_minutes = max(0.0, (now - datetime.fromisoformat(zero_since)).total_seconds() / 60.0)
        except Exception:
            zero_minutes = 0.0
    alert = bool(searchable_entity_count <= 0 and zero_minutes >= float(window_minutes))
    status = {
        "workspace_id": workspace_id,
        "owner_id": owner_id,
        "window_minutes": window_minutes,
        "entity_total": entity_total,
        "link_total": link_total,
        "searchable_entity_count": searchable_entity_count,
        "entity_creation_rate_per_min": round(float(entity_created_window) / float(window_minutes), 4),
        "link_creation_rate_per_min": round(float(link_created_window) / float(window_minutes), 4),
        "entity_created_in_window": entity_created_window,
        "link_created_in_window": link_created_window,
        "zero_entities_since": zero_since,
        "zero_entities_minutes": round(zero_minutes, 2),
        "alert": alert,
        "generated_at": now.isoformat(),
    }
    db.execute(
        text(
            """
            INSERT INTO runtime_settings(key, value, updated_at)
            VALUES (:key, CAST(:value AS jsonb), :updated_at)
            ON CONFLICT (key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """
        ),
        {"key": setting_key, "value": json.dumps(status), "updated_at": now},
    )
    return status


def run() -> dict[str, Any]:
    settings = get_settings()
    now = datetime.now(tz=UTC)
    window_minutes = max(5, int(getattr(settings, "graph_health_alert_threshold_zero_entities_minutes", 30)))
    summary: dict[str, Any] = {
        "status": "ok",
        "ran_at": now.isoformat(),
        "window_minutes": window_minutes,
        "scopes_scanned": 0,
        "alerts": 0,
        "updated_keys": [],
    }
    with SessionLocal() as db:
        scopes = db.execute(
            text(
                """
                SELECT DISTINCT workspace_id, owner_id FROM entity_nodes
                UNION
                SELECT DISTINCT workspace_id, user_id AS owner_id FROM takeover_sessions
                """
            )
        ).mappings().all()
        for scope in scopes:
            workspace_id = str(scope.get("workspace_id") or "").strip()
            owner_id = str(scope.get("owner_id") or "").strip()
            if not workspace_id or not owner_id:
                continue
            status = _compute_scope_status(
                db,
                workspace_id=workspace_id,
                owner_id=owner_id,
                window_minutes=window_minutes,
                now=now,
            )
            summary["scopes_scanned"] = int(summary["scopes_scanned"]) + 1
            if bool(status.get("alert")):
                summary["alerts"] = int(summary["alerts"]) + 1
            summary["updated_keys"].append(f"graph_health_status:{workspace_id}:{owner_id}")
        db.commit()
    summary["completed_at"] = datetime.now(tz=UTC).isoformat()
    return summary
