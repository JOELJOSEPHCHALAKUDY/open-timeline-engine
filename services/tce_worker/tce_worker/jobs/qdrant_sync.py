from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests
from redis import Redis
from sqlalchemy import text

from ..config import get_settings
from ..db import SessionLocal


def _qdrant_base_url() -> str:
    settings = get_settings()
    explicit = str(getattr(settings, "qdrant_url", "") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    ollama_url = str(getattr(settings, "ollama_url", "") or "").strip().lower()
    if "://ollama:" in ollama_url:
        return "http://qdrant:6333"
    return "http://localhost:6333"


def _cursor_keys(collection: str) -> tuple[str, str]:
    token = collection.strip().lower().replace(" ", "_")
    return (
        f"tce:qdrant_sync:{token}:cursor_ts",
        f"tce:qdrant_sync:{token}:cursor_event_id",
    )


def _parse_vector_text(raw: Any) -> list[float] | None:
    token = str(raw or "").strip()
    if not token:
        return None
    if token.startswith("[") and token.endswith("]"):
        token = token[1:-1]
    if not token:
        return None
    try:
        return [float(item) for item in token.split(",") if item]
    except Exception:
        return None


def _ensure_collection(
    session: requests.Session,
    *,
    base_url: str,
    collection: str,
    vector_size: int,
    timeout: float,
) -> bool:
    try:
        probe = session.get(f"{base_url}/collections/{collection}", timeout=timeout)
        if probe.status_code == 200:
            return True
        if probe.status_code not in {400, 404}:
            return False
        payload = {
            "vectors": {
                "size": int(vector_size),
                "distance": "Cosine",
            }
        }
        create = session.put(
            f"{base_url}/collections/{collection}",
            json=payload,
            timeout=timeout,
        )
        return create.status_code < 300
    except Exception:
        return False


def _parse_cursor_ts(raw: bytes | str | None) -> datetime:
    if not raw:
        return datetime(1970, 1, 1, tzinfo=UTC)
    token = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    token = token.strip()
    if not token:
        return datetime(1970, 1, 1, tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(token)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception:
        return datetime(1970, 1, 1, tzinfo=UTC)


def sync_event_embedding(event_id: str) -> dict[str, Any]:
    settings = get_settings()
    if not bool(getattr(settings, "qdrant_enabled", False)):
        return {"status": "skipped", "reason": "qdrant_disabled", "event_id": event_id}

    collection = str(getattr(settings, "qdrant_collection", "tce_event_embeddings") or "tce_event_embeddings").strip()
    timeout = max(0.2, float(getattr(settings, "qdrant_timeout_seconds", 2.0)))
    base_url = _qdrant_base_url()
    sql = text(
        """
        SELECT
            CAST(ee.event_id AS TEXT) AS event_id,
            ee.embedding::text AS embedding_text,
            EXTRACT(EPOCH FROM e.ts)::bigint AS ts_epoch,
            e.domain AS domain,
            e.task_type AS task_type,
            COALESCE(e.context->>'_tce_workspace', 'personal') AS workspace_id,
            COALESCE(e.context->>'_tce_owner', 'local-user') AS owner_id
        FROM event_embeddings ee
        JOIN events e ON e.id = ee.event_id
        WHERE CAST(ee.event_id AS TEXT) = :event_id
        LIMIT 1
        """
    )

    with SessionLocal() as db:
        row = db.execute(sql, {"event_id": str(event_id)}).mappings().first()
    if not row:
        return {"status": "missing", "event_id": event_id}

    vector = _parse_vector_text(row.get("embedding_text"))
    if not vector:
        return {"status": "invalid_vector", "event_id": event_id}

    point = {
        "id": str(row.get("event_id") or ""),
        "vector": vector,
        "payload": {
            "event_id": str(row.get("event_id") or ""),
            "workspace_id": str(row.get("workspace_id") or ""),
            "owner_id": str(row.get("owner_id") or ""),
            "domain": str(row.get("domain") or ""),
            "task_type": str(row.get("task_type") or ""),
            "ts_epoch": int(row.get("ts_epoch") or 0),
            "longterm": True,
        },
    }

    with requests.Session() as http_session:
        if not _ensure_collection(
            http_session,
            base_url=base_url,
            collection=collection,
            vector_size=len(vector),
            timeout=timeout,
        ):
            return {"status": "failed", "error": "collection_unavailable", "event_id": event_id}
        try:
            upsert = http_session.put(
                f"{base_url}/collections/{collection}/points",
                params={"wait": "true"},
                json={"points": [point]},
                timeout=timeout,
            )
        except Exception as exc:
            return {"status": "failed", "error": f"upsert_error:{exc}", "event_id": event_id}
        if upsert.status_code >= 300:
            return {"status": "failed", "error": f"upsert_http_{upsert.status_code}", "event_id": event_id}
    return {"status": "ok", "event_id": event_id}


def run(max_batches: int | None = None, batch_size: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    if not bool(getattr(settings, "qdrant_enabled", False)):
        return {"status": "skipped", "reason": "qdrant_disabled"}
    if not bool(getattr(settings, "qdrant_sync_enabled", True)):
        return {"status": "skipped", "reason": "qdrant_sync_disabled"}

    collection = str(getattr(settings, "qdrant_collection", "tce_event_embeddings") or "tce_event_embeddings").strip()
    timeout = max(0.2, float(getattr(settings, "qdrant_timeout_seconds", 2.0)))
    max_batches_value = max(1, int(max_batches if max_batches is not None else getattr(settings, "qdrant_sync_max_batches", 10)))
    batch_size_value = max(50, int(batch_size if batch_size is not None else getattr(settings, "qdrant_sync_batch_size", 500)))
    base_url = _qdrant_base_url()

    redis_conn = Redis.from_url(settings.redis_url)
    cursor_ts_key, cursor_event_id_key = _cursor_keys(collection)
    cursor_ts = _parse_cursor_ts(redis_conn.get(cursor_ts_key))
    cursor_event_id_raw = redis_conn.get(cursor_event_id_key)
    cursor_event_id = (cursor_event_id_raw.decode() if isinstance(cursor_event_id_raw, (bytes, bytearray)) else str(cursor_event_id_raw or "")).strip()

    total_rows = 0
    total_points = 0
    batches_done = 0
    collection_ready = False
    last_error: str | None = None

    sql = text(
        """
        SELECT
            CAST(ee.event_id AS TEXT) AS event_id,
            ee.embedding::text AS embedding_text,
            ee.created_at AS embedding_created_at,
            EXTRACT(EPOCH FROM e.ts)::bigint AS ts_epoch,
            e.domain AS domain,
            e.task_type AS task_type,
            COALESCE(e.context->>'_tce_workspace', 'personal') AS workspace_id,
            COALESCE(e.context->>'_tce_owner', 'local-user') AS owner_id
        FROM event_embeddings ee
        JOIN events e ON e.id = ee.event_id
        WHERE (
            ee.created_at > :cursor_ts
            OR (ee.created_at = :cursor_ts AND CAST(ee.event_id AS TEXT) > :cursor_event_id)
        )
        ORDER BY ee.created_at ASC, CAST(ee.event_id AS TEXT) ASC
        LIMIT :row_limit
        """
    )

    with requests.Session() as http_session:
        with SessionLocal() as db:
            for _ in range(max_batches_value):
                rows = db.execute(
                    sql,
                    {
                        "cursor_ts": cursor_ts,
                        "cursor_event_id": cursor_event_id,
                        "row_limit": batch_size_value,
                    },
                ).mappings().all()
                if not rows:
                    break
                batches_done += 1
                total_rows += len(rows)

                points: list[dict[str, Any]] = []
                expected_vector_size: int | None = None
                for row in rows:
                    vector = _parse_vector_text(row.get("embedding_text"))
                    if not vector:
                        continue
                    if expected_vector_size is None:
                        expected_vector_size = len(vector)
                    if len(vector) != expected_vector_size:
                        continue
                    event_id = str(row.get("event_id") or "").strip()
                    if not event_id:
                        continue
                    points.append(
                        {
                            "id": event_id,
                            "vector": vector,
                            "payload": {
                                "event_id": event_id,
                                "workspace_id": str(row.get("workspace_id") or ""),
                                "owner_id": str(row.get("owner_id") or ""),
                                "domain": str(row.get("domain") or ""),
                                "task_type": str(row.get("task_type") or ""),
                                "ts_epoch": int(row.get("ts_epoch") or 0),
                                "longterm": True,
                            },
                        }
                    )

                if points:
                    if not collection_ready:
                        if expected_vector_size is None:
                            last_error = "no_vectors_in_batch"
                        else:
                            collection_ready = _ensure_collection(
                                http_session,
                                base_url=base_url,
                                collection=collection,
                                vector_size=expected_vector_size,
                                timeout=timeout,
                            )
                            if not collection_ready:
                                last_error = "collection_unavailable"
                                break
                    if collection_ready:
                        for start in range(0, len(points), 128):
                            chunk = points[start:start + 128]
                            try:
                                upsert = http_session.put(
                                    f"{base_url}/collections/{collection}/points",
                                    params={"wait": "true"},
                                    json={"points": chunk},
                                    timeout=timeout,
                                )
                            except Exception as exc:
                                last_error = f"upsert_error:{exc}"
                                break
                            if upsert.status_code >= 300:
                                last_error = f"upsert_http_{upsert.status_code}"
                                break
                            total_points += len(chunk)
                        if last_error:
                            break

                last_row = rows[-1]
                last_ts_raw = last_row.get("embedding_created_at")
                if isinstance(last_ts_raw, datetime):
                    cursor_ts = last_ts_raw if last_ts_raw.tzinfo else last_ts_raw.replace(tzinfo=UTC)
                cursor_event_id = str(last_row.get("event_id") or cursor_event_id).strip()
                redis_conn.set(cursor_ts_key, cursor_ts.isoformat())
                redis_conn.set(cursor_event_id_key, cursor_event_id)

    status = "ok" if not last_error else "failed"
    response: dict[str, Any] = {
        "status": status,
        "collection": collection,
        "base_url": base_url,
        "batches": batches_done,
        "rows_scanned": total_rows,
        "points_upserted": total_points,
        "cursor_ts": cursor_ts.isoformat(),
        "cursor_event_id": cursor_event_id,
    }
    if last_error:
        response["error"] = last_error
    return response
