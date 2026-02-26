from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ENTITY_TOKEN_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9._/-]{2,64}\b")

LLM_ENTITY_PROMPT = (
    "Extract named entities from the following text. "
    "Return a JSON object with key 'entities' containing an array. "
    "Each element: {\"name\": str, \"type\": str, \"confidence\": float 0-1}. "
    "Valid types: person, organization, technology, decision, error_type, concept, path, url, domain. "
    "Text: {text}"
)


def llm_extract_entities(
    text: str,
    gateway: Any,
) -> list[tuple[str, str, float]]:
    """Extract entities via LLM. Returns list of (type, key, confidence) tuples."""
    try:
        if hasattr(gateway, "aextract_structured"):
            result = asyncio.run(
                gateway.aextract_structured(
                    prompt=LLM_ENTITY_PROMPT.replace("{text}", text[:2000]),
                    schema_name="entity_extraction_v1",
                )
            )
        else:
            result = gateway.extract_structured(
                prompt=LLM_ENTITY_PROMPT.replace("{text}", text[:2000]),
                schema_name="entity_extraction_v1",
            )
    except Exception as exc:
        logger.warning("LLM entity extraction failed, falling back to regex-only: %s", exc)
        return []

    entities_raw = result.get("entities", [])
    if not isinstance(entities_raw, list):
        return []

    output: list[tuple[str, str, float]] = []
    for item in entities_raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        entity_type = item.get("type")
        confidence = item.get("confidence", 0.65)
        if not isinstance(name, str) or not isinstance(entity_type, str):
            continue
        name = name.strip().lower()
        entity_type = entity_type.strip().lower()
        if not name or not entity_type:
            continue
        confidence = max(0.0, min(1.0, float(confidence)))
        output.append((entity_type, name, confidence))
    return output


LLM_FACT_PROMPT = (
    "Extract factual assertions from the following text. "
    "Return a JSON object with key 'facts' containing an array. "
    "Each element: {\"subject\": str, \"predicate\": str, \"object\": str, \"confidence\": float 0-1}. "
    "Only include assertions that state something concrete and verifiable. "
    "Text: {text}"
)


def llm_extract_facts(
    text: str,
    gateway: Any,
) -> list[tuple[str, str, float]]:
    """Extract facts via LLM. Returns list of (fact_key, fact_value, confidence) tuples.

    fact_key is '{subject}__{predicate}' to match the existing fact_assertions schema.
    """
    try:
        if hasattr(gateway, "aextract_structured"):
            result = asyncio.run(
                gateway.aextract_structured(
                    prompt=LLM_FACT_PROMPT.replace("{text}", text[:2000]),
                    schema_name="fact_extraction_v1",
                )
            )
        else:
            result = gateway.extract_structured(
                prompt=LLM_FACT_PROMPT.replace("{text}", text[:2000]),
                schema_name="fact_extraction_v1",
            )
    except Exception as exc:
        logger.warning("LLM fact extraction failed: %s", exc)
        return []

    facts_raw = result.get("facts", [])
    if not isinstance(facts_raw, list):
        return []

    output: list[tuple[str, str, float]] = []
    for item in facts_raw:
        if not isinstance(item, dict):
            continue
        subject = item.get("subject")
        predicate = item.get("predicate")
        obj = item.get("object")
        confidence = item.get("confidence", 0.65)
        if not all(isinstance(v, str) for v in (subject, predicate, obj)):
            continue
        subject = subject.strip()
        predicate = predicate.strip()
        obj = obj.strip()
        if not subject or not predicate or not obj:
            continue
        confidence = max(0.0, min(1.0, float(confidence)))
        fact_key = f"{subject}__{predicate}"
        output.append((fact_key, obj, confidence))
    return output


def stamp_context_scope(context: dict[str, Any], workspace_id: str, owner_id: str) -> dict[str, Any]:
    scoped = dict(context or {})
    scoped["_tce_workspace"] = workspace_id
    scoped["_tce_owner"] = owner_id
    return scoped


def _classify_entity(value: str) -> str:
    lower = value.lower()
    if "/" in value and "." in value:
        return "path"
    if lower.startswith("http://") or lower.startswith("https://"):
        return "url"
    if lower in {"coding", "planning", "ops", "research"}:
        return "domain"
    return "concept"


def _normalize(value: str) -> str:
    return value.strip().lower()


def _candidate_entities(
    domain: str,
    task_type: str,
    title: str,
    tags: list[str],
    context: dict[str, Any],
    payload: dict[str, Any],
) -> list[tuple[str, str]]:
    values: list[str] = []
    values.extend([domain, task_type])
    values.extend(tags or [])
    values.extend([v for v in [context.get("project"), context.get("repo"), context.get("branch")] if isinstance(v, str)])
    values.extend([v for v in [payload.get("summary"), title] if isinstance(v, str)])
    links = payload.get("links")
    if isinstance(links, dict):
        for key in ("issue", "pr"):
            value = links.get(key)
            if isinstance(value, str) and value:
                values.append(value)
        docs = links.get("docs")
        if isinstance(docs, list):
            values.extend([doc for doc in docs if isinstance(doc, str)])

    entities: dict[tuple[str, str], None] = {}
    for raw in values:
        if not raw:
            continue
        if len(raw) <= 96 and " " not in raw:
            norm = _normalize(raw)
            if len(norm) >= 3:
                entities[(_classify_entity(norm), norm)] = None
        for token in ENTITY_TOKEN_RE.findall(raw):
            norm = _normalize(token)
            if len(norm) >= 3:
                entities[(_classify_entity(norm), norm)] = None
    return list(entities.keys())[:80]


def _record_fact_assertions(
    db: Session,
    event_id: UUID,
    workspace_id: str,
    owner_id: str,
    domain: str,
    payload: dict[str, Any],
    derived_facts: list[tuple[str, str, float]] | None = None,
) -> list[UUID]:
    facts_raw = payload.get("facts")
    facts: list[tuple[str, str]] = []
    if isinstance(facts_raw, dict):
        for key, value in facts_raw.items():
            if value is None:
                continue
            facts.append((str(key), str(value)))
    elif isinstance(facts_raw, list):
        for item in facts_raw:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            value = item.get("value")
            if key is None or value is None:
                continue
            facts.append((str(key), str(value)))

    # Merge LLM-derived facts (with confidence)
    fact_confidences: dict[str, float] = {}
    if derived_facts:
        for fact_key, fact_value, confidence in derived_facts:
            if confidence > 0.5:
                facts.append((str(fact_key), str(fact_value)))
                fact_confidences[str(fact_key)] = confidence

    conflict_rel_ids: list[UUID] = []
    for fact_key, fact_value in facts:
        previous = db.execute(
            text(
                """
                SELECT id, event_id, fact_value
                FROM fact_assertions
                WHERE workspace_id = :workspace_id
                  AND owner_id = :owner_id
                  AND domain = :domain
                  AND fact_key = :fact_key
                  AND active = true
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "domain": domain,
                "fact_key": fact_key,
            },
        ).mappings().first()
        supersedes: UUID | None = None
        if previous and str(previous["fact_value"]) != fact_value:
            supersedes = previous["event_id"]
            db.execute(
                text("UPDATE fact_assertions SET active = false WHERE id = :id"),
                {"id": previous["id"]},
            )
            relation_id = uuid.uuid4()
            db.execute(
                text(
                    """
                    INSERT INTO event_relationships (
                      id, workspace_id, owner_id, source_event_id, target_event_id,
                      relationship_type, confidence, relation_meta, created_at
                    )
                    VALUES (
                      :id, :workspace_id, :owner_id, :source_event_id, :target_event_id,
                      'contradicts', :confidence, :relation_meta::jsonb, :created_at
                    )
                    """
                ),
                {
                    "id": relation_id,
                    "workspace_id": workspace_id,
                    "owner_id": owner_id,
                    "source_event_id": event_id,
                    "target_event_id": supersedes,
                    "confidence": fact_confidences.get(fact_key, 0.92),
                    "relation_meta": '{"type":"fact_conflict"}',
                    "created_at": datetime.now(tz=UTC),
                },
            )
            conflict_rel_ids.append(relation_id)

        db.execute(
            text(
                """
                INSERT INTO fact_assertions (
                  id, workspace_id, owner_id, domain, fact_key, fact_value,
                  event_id, supersedes_event_id, active, created_at
                )
                VALUES (
                  :id, :workspace_id, :owner_id, :domain, :fact_key, :fact_value,
                  :event_id, :supersedes_event_id, true, :created_at
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "domain": domain,
                "fact_key": fact_key,
                "fact_value": fact_value,
                "event_id": event_id,
                "supersedes_event_id": supersedes,
                "created_at": datetime.now(tz=UTC),
            },
        )
    return conflict_rel_ids


def _event_fingerprint_hash(
    *,
    domain: str,
    task_type: str,
    title: str,
    context: dict[str, Any],
    tags: list[str],
) -> str:
    source = {
        "domain": domain,
        "task_type": task_type,
        "title": title[:200].lower(),
        "repo": str(context.get("repo", "")).lower() if isinstance(context, dict) else "",
        "project": str(context.get("project", "")).lower() if isinstance(context, dict) else "",
        "tags": sorted(str(tag).lower() for tag in (tags or [])[:20]),
    }
    return hashlib.sha256(str(source).encode("utf-8")).hexdigest()


def index_event_graph(
    db: Session,
    *,
    event_id: UUID,
    workspace_id: str,
    owner_id: str,
    domain: str,
    task_type: str,
    title: str,
    tags: list[str],
    context: dict[str, Any],
    payload: dict[str, Any],
    gateway: Any | None = None,
    llm_extraction: bool = False,
) -> dict[str, Any]:
    db.execute(
        text(
            """
            INSERT INTO team_memberships (id, workspace_id, user_id, role, added_by, created_at, active)
            VALUES (:id, :workspace_id, :user_id, 'owner', :added_by, :created_at, true)
            ON CONFLICT (workspace_id, user_id)
            DO UPDATE SET active = true
            """
        ),
        {
            "id": uuid.uuid4(),
            "workspace_id": workspace_id,
            "user_id": owner_id,
            "added_by": owner_id,
            "created_at": datetime.now(tz=UTC),
        },
    )

    entity_pairs = _candidate_entities(domain, task_type, title, tags, context, payload)
    entity_count = 0
    canonical_entity_ids: dict[tuple[str, str], Any] = {}
    for entity_type, entity_key in entity_pairs:
        row = db.execute(
            text(
                """
                INSERT INTO entity_nodes (
                  id, workspace_id, owner_id, entity_type, entity_key, display_name, created_at, updated_at
                )
                VALUES (
                  :id, :workspace_id, :owner_id, :entity_type, :entity_key, :display_name, :ts, :ts
                )
                ON CONFLICT (workspace_id, owner_id, entity_type, entity_key)
                DO UPDATE SET updated_at = excluded.updated_at
                RETURNING id
                """
            ),
            {
                "id": uuid.uuid4(),
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "entity_type": entity_type,
                "entity_key": entity_key,
                "display_name": entity_key,
                "ts": datetime.now(tz=UTC),
            },
        ).scalar_one()
        canonical_entity_ids[(entity_type, entity_key)] = row
        db.execute(
            text(
                """
                INSERT INTO event_entity_links (
                  id, workspace_id, owner_id, event_id, entity_id, role, confidence, created_at
                )
                VALUES (
                  :id, :workspace_id, :owner_id, :event_id, :entity_id, :role, :confidence, :created_at
                )
                ON CONFLICT (workspace_id, event_id, entity_id, role) DO NOTHING
                """
            ),
            {
                "id": uuid.uuid4(),
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "event_id": event_id,
                "entity_id": row,
                "role": "mentioned",
                "confidence": 0.65,
                "created_at": datetime.now(tz=UTC),
            },
        )
        db.execute(
            text(
                """
                INSERT INTO entity_aliases (
                  id, workspace_id, owner_id, canonical_entity_id, alias_key, confidence, evidence_json, created_at, updated_at
                )
                VALUES (
                  :id, :workspace_id, :owner_id, :canonical_entity_id, :alias_key, :confidence, CAST(:evidence AS jsonb), :ts, :ts
                )
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "id": uuid.uuid4(),
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "canonical_entity_id": row,
                "alias_key": entity_key,
                "confidence": 0.65,
                "evidence": '{"source":"heuristic"}',
                "ts": datetime.now(tz=UTC),
            },
        )
        entity_count += 1

    # LLM-assisted entity extraction (opt-in)
    llm_entity_count = 0
    if llm_extraction and gateway is not None:
        text_for_llm = f"{title}\n{payload.get('summary', '')}"
        llm_entities = llm_extract_entities(text_for_llm, gateway)
        existing_keys = {(etype, ekey) for etype, ekey in entity_pairs}
        for entity_type, entity_key, confidence in llm_entities:
            if (entity_type, entity_key) in existing_keys:
                continue
            existing_keys.add((entity_type, entity_key))
            row = db.execute(
                text(
                    """
                    INSERT INTO entity_nodes (
                      id, workspace_id, owner_id, entity_type, entity_key, display_name, created_at, updated_at
                    )
                    VALUES (
                      :id, :workspace_id, :owner_id, :entity_type, :entity_key, :display_name, :ts, :ts
                    )
                    ON CONFLICT (workspace_id, owner_id, entity_type, entity_key)
                    DO UPDATE SET updated_at = excluded.updated_at
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "workspace_id": workspace_id,
                    "owner_id": owner_id,
                    "entity_type": entity_type,
                    "entity_key": entity_key,
                    "display_name": entity_key,
                    "ts": datetime.now(tz=UTC),
                },
            ).scalar_one()
            canonical_entity_ids[(entity_type, entity_key)] = row
            db.execute(
                text(
                    """
                    INSERT INTO event_entity_links (
                      id, workspace_id, owner_id, event_id, entity_id, role, confidence, created_at
                    )
                    VALUES (
                      :id, :workspace_id, :owner_id, :event_id, :entity_id, :role, :confidence, :created_at
                    )
                    ON CONFLICT (workspace_id, event_id, entity_id, role) DO NOTHING
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "workspace_id": workspace_id,
                    "owner_id": owner_id,
                    "event_id": event_id,
                    "entity_id": row,
                    "role": "mentioned",
                    "confidence": confidence,
                    "created_at": datetime.now(tz=UTC),
                },
            )
            db.execute(
                text(
                    """
                    INSERT INTO entity_aliases (
                      id, workspace_id, owner_id, canonical_entity_id, alias_key, confidence, evidence_json, created_at, updated_at
                    )
                    VALUES (
                      :id, :workspace_id, :owner_id, :canonical_entity_id, :alias_key, :confidence, CAST(:evidence AS jsonb), :ts, :ts
                    )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "workspace_id": workspace_id,
                    "owner_id": owner_id,
                    "canonical_entity_id": row,
                    "alias_key": entity_key,
                    "confidence": confidence,
                    "evidence": '{"source":"llm"}',
                    "ts": datetime.now(tz=UTC),
                },
            )
            llm_entity_count += 1

    previous = db.execute(
        text(
            """
            SELECT id
            FROM events
            WHERE id <> :event_id
              AND (context->>'_tce_workspace') = :workspace_id
              AND (context->>'_tce_owner') = :owner_id
              AND domain = :domain
              AND task_type = :task_type
            ORDER BY ts DESC
            LIMIT 1
            """
        ),
        {
            "event_id": event_id,
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "domain": domain,
            "task_type": task_type,
        },
    ).mappings().first()
    sequence_relation_id: UUID | None = None
    if previous:
        sequence_relation_id = uuid.uuid4()
        db.execute(
            text(
                """
                INSERT INTO event_relationships (
                  id, workspace_id, owner_id, source_event_id, target_event_id,
                  relationship_type, confidence, relation_meta, created_at
                )
                VALUES (
                  :id, :workspace_id, :owner_id, :source_event_id, :target_event_id,
                  'follows', :confidence, :relation_meta::jsonb, :created_at
                )
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "id": sequence_relation_id,
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "source_event_id": event_id,
                "target_event_id": previous["id"],
                "confidence": 0.7,
                "relation_meta": '{"type":"sequence"}',
                "created_at": datetime.now(tz=UTC),
            },
        )

    # LLM-derived fact extraction (opt-in)
    derived_facts: list[tuple[str, str, float]] | None = None
    if llm_extraction and gateway is not None:
        fact_text = f"{title}\n{payload.get('summary', '')}"
        derived_facts = llm_extract_facts(fact_text, gateway)

    conflict_ids = _record_fact_assertions(
        db=db,
        event_id=event_id,
        workspace_id=workspace_id,
        owner_id=owner_id,
        domain=domain,
        payload=payload,
        derived_facts=derived_facts,
    )
    fingerprint_hash = _event_fingerprint_hash(
        domain=domain,
        task_type=task_type,
        title=title,
        context=context,
        tags=tags,
    )
    db.execute(
        text(
            """
            INSERT INTO event_fingerprints (id, workspace_id, owner_id, event_id, fingerprint_hash, fingerprint_payload, created_at)
            VALUES (:id, :workspace_id, :owner_id, :event_id, :fingerprint_hash, CAST(:payload AS jsonb), :created_at)
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "id": uuid.uuid4(),
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "event_id": event_id,
            "fingerprint_hash": fingerprint_hash,
            "payload": '{"version":"v8"}',
            "created_at": datetime.now(tz=UTC),
        },
    )
    db.commit()
    return {
        "entities_indexed": entity_count,
        "llm_entities_indexed": llm_entity_count,
        "sequence_relationship": str(sequence_relation_id) if sequence_relation_id else None,
        "conflict_relationships": [str(value) for value in conflict_ids],
        "fingerprint_hash": fingerprint_hash,
    }


def graph_snapshot_for_events(
    db: Session,
    workspace_id: str,
    owner_id: str,
    event_ids: list[UUID],
    max_entities: int = 20,
    max_relationships: int = 20,
) -> dict[str, Any]:
    if not event_ids:
        return {"entities": [], "relationships": [], "facts": []}
    try:
        ids_list = list(event_ids)
        entity_rows = db.execute(
            text(
                """
                SELECT DISTINCT
                  en.id,
                  en.entity_type,
                  en.entity_key,
                  en.display_name
                FROM entity_nodes en
                JOIN event_entity_links eel ON eel.entity_id = en.id
                WHERE eel.workspace_id = :workspace_id
                  AND en.owner_id = :owner_id
                  AND eel.event_id = ANY(:event_ids)
                ORDER BY en.updated_at DESC
                LIMIT :max_entities
                """
            ),
            {
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "event_ids": ids_list,
                "max_entities": max_entities,
            },
        ).mappings().all()
        relationship_rows = db.execute(
            text(
                """
                SELECT
                  id, source_event_id, target_event_id, relationship_type, confidence, relation_meta
                FROM event_relationships
                WHERE workspace_id = :workspace_id
                  AND owner_id = :owner_id
                  AND source_event_id = ANY(:event_ids)
                ORDER BY created_at DESC
                LIMIT :max_relationships
                """
            ),
            {
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "event_ids": ids_list,
                "max_relationships": max_relationships,
            },
        ).mappings().all()
        fact_rows = db.execute(
            text(
                """
                SELECT fact_key, fact_value, active, event_id, supersedes_event_id, created_at
                FROM fact_assertions
                WHERE workspace_id = :workspace_id
                  AND owner_id = :owner_id
                  AND event_id = ANY(:event_ids)
                ORDER BY created_at DESC
                LIMIT 20
                """
            ),
            {"workspace_id": workspace_id, "owner_id": owner_id, "event_ids": ids_list},
        ).mappings().all()
    except Exception:
        return {"entities": [], "relationships": [], "facts": []}

    return {
        "entities": [
            {
                "id": str(row["id"]),
                "type": row["entity_type"],
                "key": row["entity_key"],
                "display_name": row["display_name"],
            }
            for row in entity_rows
        ],
        "relationships": [
            {
                "id": str(row["id"]),
                "source_event_id": str(row["source_event_id"]),
                "target_event_id": str(row["target_event_id"]),
                "type": row["relationship_type"],
                "confidence": float(row["confidence"]),
                "meta": row["relation_meta"] or {},
            }
            for row in relationship_rows
        ],
        "facts": [
            {
                "key": row["fact_key"],
                "value": row["fact_value"],
                "active": bool(row["active"]),
                "event_id": str(row["event_id"]),
                "supersedes_event_id": str(row["supersedes_event_id"]) if row["supersedes_event_id"] else None,
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in fact_rows
        ],
    }


def search_entities(
    db: Session,
    workspace_id: str,
    owner_id: str,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT
              en.id,
              en.entity_type,
              en.entity_key,
              en.display_name,
              en.updated_at,
              COUNT(eel.event_id) AS event_count
            FROM entity_nodes en
            LEFT JOIN event_entity_links eel
              ON eel.entity_id = en.id
             AND eel.workspace_id = :workspace_id
            WHERE en.workspace_id = :workspace_id
              AND en.owner_id = :owner_id
              AND (
                en.entity_key ILIKE :q
                OR en.display_name ILIKE :q
              )
            GROUP BY en.id, en.entity_type, en.entity_key, en.display_name, en.updated_at
            ORDER BY event_count DESC, en.updated_at DESC
            LIMIT :limit
            """
        ),
        {
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "q": f"%{query.strip()}%",
            "limit": max(1, min(limit, 100)),
        },
    ).mappings().all()
    return [
        {
            "id": str(row["id"]),
            "type": row["entity_type"],
            "key": row["entity_key"],
            "display_name": row["display_name"],
            "event_count": int(row["event_count"] or 0),
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
        for row in rows
    ]


def graph_for_event(
    db: Session,
    workspace_id: str,
    owner_id: str,
    event_id: UUID,
) -> dict[str, Any]:
    return graph_snapshot_for_events(
        db=db,
        workspace_id=workspace_id,
        owner_id=owner_id,
        event_ids=[event_id],
        max_entities=30,
        max_relationships=30,
    )


def list_team_memberships(db: Session, workspace_id: str) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT user_id, role, added_by, created_at, active
            FROM team_memberships
            WHERE workspace_id = :workspace_id
            ORDER BY created_at ASC
            """
        ),
        {"workspace_id": workspace_id},
    ).mappings().all()
    return [
        {
            "workspace_id": workspace_id,
            "user_id": row["user_id"],
            "role": row["role"],
            "added_by": row["added_by"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "active": bool(row["active"]),
        }
        for row in rows
    ]


_workspace_access_cache: dict[str, tuple[bool, float]] = {}
_WORKSPACE_ACCESS_TTL = 300.0  # 5 minutes


def workspace_access_allowed(db: Session, workspace_id: str, user_id: str) -> bool:
    import time

    cache_key = f"{workspace_id}:{user_id}"
    cached = _workspace_access_cache.get(cache_key)
    if cached is not None:
        allowed, ts = cached
        if time.monotonic() - ts < _WORKSPACE_ACCESS_TTL:
            return allowed

    workspace_rows = db.execute(
        text(
            """
            SELECT COUNT(1) AS c
            FROM team_memberships
            WHERE workspace_id = :workspace_id
              AND active = true
            """
        ),
        {"workspace_id": workspace_id},
    ).mappings().first()
    count = int(workspace_rows["c"]) if workspace_rows else 0
    if count == 0:
        _workspace_access_cache[cache_key] = (True, time.monotonic())
        return True
    member = db.execute(
        text(
            """
            SELECT 1
            FROM team_memberships
            WHERE workspace_id = :workspace_id
              AND user_id = :user_id
              AND active = true
            LIMIT 1
            """
        ),
        {"workspace_id": workspace_id, "user_id": user_id},
    ).first()
    result = member is not None
    _workspace_access_cache[cache_key] = (result, time.monotonic())
    return result


def upsert_team_membership(
    db: Session,
    workspace_id: str,
    user_id: str,
    role: str,
    added_by: str,
    active: bool,
) -> dict[str, Any]:
    db.execute(
        text(
            """
            INSERT INTO team_memberships (id, workspace_id, user_id, role, added_by, created_at, active)
            VALUES (:id, :workspace_id, :user_id, :role, :added_by, :created_at, :active)
            ON CONFLICT (workspace_id, user_id)
            DO UPDATE SET
              role = excluded.role,
              added_by = excluded.added_by,
              active = excluded.active
            """
        ),
        {
            "id": uuid.uuid4(),
            "workspace_id": workspace_id,
            "user_id": user_id,
            "role": role,
            "added_by": added_by,
            "created_at": datetime.now(tz=UTC),
            "active": active,
        },
    )
    db.commit()
    return {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "role": role,
        "added_by": added_by,
        "active": active,
    }
