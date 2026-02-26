from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from tce_model_gateway import create_gateway

from ..config import get_settings
from ..db import SessionLocal


def _observation_embed_text(row: dict) -> str:
    parts = [
        str(row.get("situation_summary") or ""),
        str(row.get("user_response") or ""),
        str(row.get("response_reasoning") or ""),
        str(row.get("outcome") or ""),
    ]
    return "\n".join(part for part in parts if part).strip()


def _as_vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in embedding) + "]"


def run(observation_id: str) -> dict:
    settings = get_settings()
    gateway = create_gateway(settings)
    parsed_id = UUID(observation_id)
    with SessionLocal() as db:
        row = db.execute(
            text(
                """
                SELECT id, situation_summary, user_response, response_reasoning, outcome
                FROM decision_observations
                WHERE id = :observation_id
                """
            ),
            {"observation_id": parsed_id},
        ).mappings().first()
        if not row:
            return {"status": "missing", "observation_id": observation_id}

        content = _observation_embed_text(dict(row))
        if not content:
            return {"status": "skipped", "observation_id": observation_id, "reason": "empty_content"}
        embedding = gateway.embed(content)
        db.execute(
            text(
                """
                UPDATE decision_observations
                SET embedding = CAST(:embedding AS vector), ts = COALESCE(ts, :ts_now)
                WHERE id = :observation_id
                """
            ),
            {
                "observation_id": parsed_id,
                "embedding": _as_vector_literal(embedding),
                "ts_now": datetime.now(tz=UTC),
            },
        )
        db.commit()
        return {"status": "ok", "observation_id": observation_id, "dimensions": len(embedding)}
