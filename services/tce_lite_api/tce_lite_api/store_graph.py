from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


ENTITY_TOKEN_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9._/-]{2,64}\b")


def _scope_match(context: dict[str, Any], workspace_id: str, owner_id: str) -> bool:
    ctx_workspace = context.get("_tce_workspace")
    ctx_owner = context.get("_tce_owner")
    if ctx_workspace and ctx_workspace != workspace_id:
        return False
    if ctx_owner and ctx_owner != owner_id:
        return False
    return True


def _classify_entity(token: str) -> str:
    lower = token.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        return "url"
    if "/" in token and "." in token:
        return "path"
    if lower in {"coding", "planning", "ops", "research"}:
        return "domain"
    return "concept"


def _entity_candidates(
    domain: str,
    task_type: str,
    title: str,
    tags: list[str],
    context: dict[str, Any],
    payload: dict[str, Any],
) -> list[tuple[str, str]]:
    values: list[str] = [domain, task_type, title]
    values.extend([tag for tag in tags if isinstance(tag, str)])
    for key in ("project", "repo", "branch"):
        value = context.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    summary = payload.get("summary")
    if isinstance(summary, str):
        values.append(summary)

    entities: dict[tuple[str, str], None] = {}
    for value in values:
        if len(value) <= 96 and " " not in value:
            norm = value.strip().lower()
            if len(norm) >= 3:
                entities[(_classify_entity(norm), norm)] = None
        for token in ENTITY_TOKEN_RE.findall(value):
            norm = token.strip().lower()
            if len(norm) >= 3:
                entities[(_classify_entity(norm), norm)] = None
    return list(entities.keys())[:80]


def _ensure_owner_membership(conn: sqlite3.Connection, workspace_id: str, owner_id: str) -> None:
    conn.execute(
        """
        INSERT INTO team_memberships(id, workspace_id, user_id, role, added_by, created_at, active)
        VALUES(?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(workspace_id, user_id) DO UPDATE SET active = 1
        """,
        (
            str(uuid.uuid4()),
            workspace_id,
            owner_id,
            "owner",
            owner_id,
            now_utc().isoformat(),
        ),
    )
    conn.commit()


def _record_fact_conflicts(
    conn: sqlite3.Connection,
    event_id: str,
    workspace_id: str,
    owner_id: str,
    domain: str,
    payload: dict[str, Any],
) -> list[str]:
    facts_raw = payload.get("facts")
    facts: list[tuple[str, str]] = []
    if isinstance(facts_raw, dict):
        facts.extend([(str(k), str(v)) for k, v in facts_raw.items() if v is not None])
    elif isinstance(facts_raw, list):
        for item in facts_raw:
            if isinstance(item, dict) and item.get("key") is not None and item.get("value") is not None:
                facts.append((str(item["key"]), str(item["value"])))

    conflict_rel_ids: list[str] = []
    for fact_key, fact_value in facts:
        previous = conn.execute(
            """
            SELECT id, event_id, fact_value
            FROM fact_assertions
            WHERE workspace_id = ?
              AND owner_id = ?
              AND domain = ?
              AND fact_key = ?
              AND active = 1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (workspace_id, owner_id, domain, fact_key),
        ).fetchone()
        supersedes_event_id = None
        if previous and str(previous["fact_value"]) != fact_value:
            supersedes_event_id = previous["event_id"]
            conn.execute("UPDATE fact_assertions SET active = 0 WHERE id = ?", (previous["id"],))
            rel_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO event_relationships(
                    id, workspace_id, owner_id, source_event_id, target_event_id,
                    relationship_type, confidence, relation_meta, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rel_id,
                    workspace_id,
                    owner_id,
                    event_id,
                    supersedes_event_id,
                    "contradicts",
                    0.92,
                    json_dumps({"type": "fact_conflict", "fact_key": fact_key}),
                    now_utc().isoformat(),
                ),
            )
            conflict_rel_ids.append(rel_id)

        conn.execute(
            """
            INSERT INTO fact_assertions(
                id, workspace_id, owner_id, domain, fact_key, fact_value,
                event_id, supersedes_event_id, active, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                str(uuid.uuid4()),
                workspace_id,
                owner_id,
                domain,
                fact_key,
                fact_value,
                event_id,
                supersedes_event_id,
                now_utc().isoformat(),
            ),
        )
    conn.commit()
    return conflict_rel_ids


def _index_graph(
    conn: sqlite3.Connection,
    event_id: str,
    workspace_id: str,
    owner_id: str,
    domain: str,
    task_type: str,
    title: str,
    tags: list[str],
    context: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    entities = _entity_candidates(domain, task_type, title, tags, context, payload)
    count = 0
    for entity_type, entity_key in entities:
        row = conn.execute(
            """
            SELECT id FROM entity_nodes
            WHERE workspace_id = ? AND owner_id = ? AND entity_type = ? AND entity_key = ?
            """,
            (workspace_id, owner_id, entity_type, entity_key),
        ).fetchone()
        if row:
            entity_id = row["id"]
            conn.execute(
                "UPDATE entity_nodes SET updated_at = ? WHERE id = ?",
                (now_utc().isoformat(), entity_id),
            )
        else:
            entity_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO entity_nodes(
                    id, workspace_id, owner_id, entity_type, entity_key, display_name, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_id,
                    workspace_id,
                    owner_id,
                    entity_type,
                    entity_key,
                    entity_key,
                    now_utc().isoformat(),
                    now_utc().isoformat(),
                ),
            )

        conn.execute(
            """
            INSERT INTO event_entity_links(
                id, workspace_id, owner_id, event_id, entity_id, role, confidence, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, event_id, entity_id, role) DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                workspace_id,
                owner_id,
                event_id,
                entity_id,
                "mentioned",
                0.65,
                now_utc().isoformat(),
            ),
        )
        count += 1

    prev = conn.execute(
        """
        SELECT id FROM events
        WHERE id <> ?
          AND domain = ?
          AND task_type = ?
          AND context LIKE ?
          AND context LIKE ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (event_id, domain, task_type, f'%\"_tce_workspace\":\"{workspace_id}\"%', f'%\"_tce_owner\":\"{owner_id}\"%'),
    ).fetchone()
    sequence_rel = None
    if prev:
        sequence_rel = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO event_relationships(
                id, workspace_id, owner_id, source_event_id, target_event_id,
                relationship_type, confidence, relation_meta, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence_rel,
                workspace_id,
                owner_id,
                event_id,
                prev["id"],
                "follows",
                0.7,
                json_dumps({"type": "sequence"}),
                now_utc().isoformat(),
            ),
        )

    conflict_relationships = _record_fact_conflicts(
        conn=conn,
        event_id=event_id,
        workspace_id=workspace_id,
        owner_id=owner_id,
        domain=domain,
        payload=payload,
    )
    conn.commit()
    return {
        "entities_indexed": count,
        "sequence_relationship": sequence_rel,
        "conflict_relationships": conflict_relationships,
    }


def graph_snapshot_for_events(
    conn: sqlite3.Connection,
    workspace_id: str,
    owner_id: str,
    event_ids: list[UUID],
    max_entities: int = 20,
    max_relationships: int = 20,
) -> dict[str, Any]:
    if not event_ids:
        return {"entities": [], "relationships": [], "facts": []}
    id_values = [str(event_id) for event_id in event_ids]
    placeholders = ",".join(["?"] * len(id_values))

    entity_rows = conn.execute(
        f"""
        SELECT DISTINCT
            en.id, en.entity_type, en.entity_key, en.display_name
        FROM entity_nodes en
        JOIN event_entity_links eel ON eel.entity_id = en.id
        WHERE eel.workspace_id = ?
          AND en.owner_id = ?
          AND eel.event_id IN ({placeholders})
        ORDER BY en.updated_at DESC
        LIMIT ?
        """,
        [workspace_id, owner_id, *id_values, max_entities],
    ).fetchall()
    relationship_rows = conn.execute(
        f"""
        SELECT id, source_event_id, target_event_id, relationship_type, confidence, relation_meta
        FROM event_relationships
        WHERE workspace_id = ?
          AND owner_id = ?
          AND source_event_id IN ({placeholders})
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [workspace_id, owner_id, *id_values, max_relationships],
    ).fetchall()
    fact_rows = conn.execute(
        f"""
        SELECT fact_key, fact_value, active, event_id, supersedes_event_id, created_at
        FROM fact_assertions
        WHERE workspace_id = ?
          AND owner_id = ?
          AND event_id IN ({placeholders})
        ORDER BY created_at DESC
        LIMIT 20
        """,
        [workspace_id, owner_id, *id_values],
    ).fetchall()
    return {
        "entities": [
            {
                "id": row["id"],
                "type": row["entity_type"],
                "key": row["entity_key"],
                "display_name": row["display_name"],
            }
            for row in entity_rows
        ],
        "relationships": [
            {
                "id": row["id"],
                "source_event_id": row["source_event_id"],
                "target_event_id": row["target_event_id"],
                "type": row["relationship_type"],
                "confidence": float(row["confidence"]),
                "meta": json_loads(row["relation_meta"], {}),
            }
            for row in relationship_rows
        ],
        "facts": [
            {
                "key": row["fact_key"],
                "value": row["fact_value"],
                "active": bool(row["active"]),
                "event_id": row["event_id"],
                "supersedes_event_id": row["supersedes_event_id"],
                "created_at": row["created_at"],
            }
            for row in fact_rows
        ],
    }


def search_entities(
    conn: sqlite3.Connection,
    workspace_id: str,
    owner_id: str,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
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
         AND eel.workspace_id = ?
        WHERE en.workspace_id = ?
          AND en.owner_id = ?
          AND (
            en.entity_key LIKE ?
            OR en.display_name LIKE ?
          )
        GROUP BY en.id, en.entity_type, en.entity_key, en.display_name, en.updated_at
        ORDER BY event_count DESC, en.updated_at DESC
        LIMIT ?
        """,
        (workspace_id, workspace_id, owner_id, f"%{query.strip().lower()}%", f"%{query.strip().lower()}%", limit),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "type": row["entity_type"],
            "key": row["entity_key"],
            "display_name": row["display_name"],
            "event_count": int(row["event_count"] or 0),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def graph_for_event(
    conn: sqlite3.Connection, workspace_id: str, owner_id: str, event_id: UUID
) -> dict[str, Any]:
    return graph_snapshot_for_events(
        conn=conn,
        workspace_id=workspace_id,
        owner_id=owner_id,
        event_ids=[event_id],
        max_entities=30,
        max_relationships=30,
    )


def list_team_memberships(conn: sqlite3.Connection, workspace_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT user_id, role, added_by, created_at, active
        FROM team_memberships
        WHERE workspace_id = ?
        ORDER BY created_at ASC
        """,
        (workspace_id,),
    ).fetchall()
    return [
        {
            "workspace_id": workspace_id,
            "user_id": row["user_id"],
            "role": row["role"],
            "added_by": row["added_by"],
            "created_at": row["created_at"],
            "active": bool(row["active"]),
        }
        for row in rows
    ]


def workspace_access_allowed(
    conn: sqlite3.Connection,
    workspace_id: str,
    user_id: str,
    *,
    require_membership: bool = False,
) -> bool:
    count_row = conn.execute(
        """
        SELECT COUNT(1) AS c
        FROM team_memberships
        WHERE workspace_id = ?
          AND active = 1
        """,
        (workspace_id,),
    ).fetchone()
    count = int(count_row["c"]) if count_row else 0
    if count == 0:
        return not require_membership
    member = conn.execute(
        """
        SELECT 1
        FROM team_memberships
        WHERE workspace_id = ?
          AND user_id = ?
          AND active = 1
        LIMIT 1
        """,
        (workspace_id, user_id),
    ).fetchone()
    return member is not None


def upsert_team_membership(
    conn: sqlite3.Connection,
    workspace_id: str,
    user_id: str,
    role: str,
    added_by: str,
    active: bool,
) -> dict[str, Any]:
    conn.execute(
        """
        INSERT INTO team_memberships(id, workspace_id, user_id, role, added_by, created_at, active)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(workspace_id, user_id)
        DO UPDATE SET
          role = excluded.role,
          added_by = excluded.added_by,
          active = excluded.active
        """,
        (
            str(uuid.uuid4()),
            workspace_id,
            user_id,
            role,
            added_by,
            now_utc().isoformat(),
            1 if active else 0,
        ),
    )
    conn.commit()
    return {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "role": role,
        "added_by": added_by,
        "active": active,
    }
