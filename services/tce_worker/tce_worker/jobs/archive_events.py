from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

from ..config import get_settings
from ..db import SessionLocal
from .graph_health import run as run_graph_health

_PARTITION_RE = re.compile(r"^events_v2_(\d{6})$")


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def _month_end_from_partition(partition_name: str) -> datetime | None:
    match = _PARTITION_RE.match(partition_name)
    if not match:
        return None
    ym = match.group(1)
    year = int(ym[:4])
    month = int(ym[4:6])
    if month == 12:
        return datetime(year + 1, 1, 1, tzinfo=UTC)
    return datetime(year, month + 1, 1, tzinfo=UTC)


def _write_status(db, status: dict[str, Any]) -> None:
    db.execute(
        text(
            """
            INSERT INTO runtime_settings (key, value, updated_at)
            VALUES ('lifecycle_status', CAST(:status AS jsonb), :updated_at)
            ON CONFLICT (key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """
        ),
        {"status": json.dumps(status), "updated_at": datetime.now(tz=UTC)},
    )


def _ensure_retention_runtime_setting(db, retention_days: int, archive_enabled: bool, archive_path: str) -> None:
    db.execute(
        text(
            """
            INSERT INTO runtime_settings (key, value, updated_at)
            VALUES (
              'event_retention',
              CAST(:value AS jsonb),
              :updated_at
            )
            ON CONFLICT (key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """
        ),
        {
            "value": json.dumps(
                {
                "retention_days": retention_days,
                "handoff_retention_days": int(get_settings().handoff_retention_days),
                "behavior_control_retention_days": int(get_settings().behavior_control_retention_days),
                "archive_enabled": archive_enabled,
                    "archive_path": archive_path,
                }
            ),
            "updated_at": datetime.now(tz=UTC),
        },
    )


def _flush_session_snapshots_before_lifecycle(db, *, max_per_session: int) -> int:
    rows = db.execute(
        text(
            """
            SELECT session_id, workspace_id, user_id, objective_hash, takeover_context, working_set_json,
                   recent_outcomes_json, autonomy_score
            FROM takeover_sessions
            WHERE objective_hash IS NOT NULL
              AND objective_hash <> ''
              AND (
                    active = TRUE
                 OR COALESCE(takeover_context, '{}'::jsonb) <> '{}'::jsonb
                 OR COALESCE(working_set_json, '{}'::jsonb) <> '{}'::jsonb
              )
            ORDER BY updated_at DESC
            """
        )
    ).mappings().all()
    created = 0
    keep_limit = max(1, int(max_per_session))
    now = datetime.now(tz=UTC)
    for row in rows:
        objective_hash = str(row.get("objective_hash") or "").strip()
        if not objective_hash:
            continue
        takeover_context = row.get("takeover_context")
        working_set = row.get("working_set_json")
        if not isinstance(takeover_context, dict):
            takeover_context = {}
        if not isinstance(working_set, dict):
            working_set = {}
        if not takeover_context and not working_set:
            continue
        recent_outcomes = row.get("recent_outcomes_json")
        if not isinstance(recent_outcomes, list):
            recent_outcomes = []
        try:
            autonomy_score = float(row.get("autonomy_score") or 0.5)
        except (TypeError, ValueError):
            autonomy_score = 0.5
        snapshot_payload = {
            "reason": "pre_lifecycle_flush",
            "objective_hash": objective_hash,
            "takeover_context": takeover_context,
            "working_set_json": working_set,
            "recent_outcomes_json": recent_outcomes,
            "autonomy_score": autonomy_score,
            "saved_at": now.isoformat(),
        }
        snapshot_id = str(uuid.uuid4())
        db.execute(
            text(
                """
                INSERT INTO session_memory_snapshots(
                    id, workspace_id, user_id, session_id, objective_hash, snapshot_json, created_at
                )
                VALUES(
                    :id, :workspace_id, :user_id, :session_id, :objective_hash, CAST(:snapshot_json AS jsonb), :created_at
                )
                """
            ),
            {
                "id": snapshot_id,
                "workspace_id": str(row.get("workspace_id")),
                "user_id": str(row.get("user_id")),
                "session_id": str(row.get("session_id")),
                "objective_hash": objective_hash,
                "snapshot_json": json.dumps(snapshot_payload),
                "created_at": now,
            },
        )
        db.execute(
            text(
                """
                DELETE FROM session_memory_snapshots
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY workspace_id, user_id, session_id
                                   ORDER BY created_at DESC
                               ) AS rn
                        FROM session_memory_snapshots
                        WHERE workspace_id = :workspace_id
                          AND user_id = :user_id
                          AND session_id = :session_id
                    ) ranked
                    WHERE rn > :keep_limit
                )
                """
            ),
            {
                "workspace_id": str(row.get("workspace_id")),
                "user_id": str(row.get("user_id")),
                "session_id": str(row.get("session_id")),
                "keep_limit": keep_limit,
            },
        )
        created += 1
    return created


def run(retention_days: int | None = None, dry_run: bool | None = None) -> dict[str, Any]:
    settings = get_settings()
    now = datetime.now(tz=UTC)
    retention = int(retention_days if retention_days is not None else settings.event_retention_days)
    effective_dry_run = bool(settings.lifecycle_dry_run if dry_run is None else dry_run)
    archive_enabled = bool(settings.archive_enabled)
    archive_path = Path(settings.archive_path)
    cutoff = now - timedelta(days=retention)
    cutoff_iso = cutoff.isoformat()

    summary: dict[str, Any] = {
        "status": "ok",
        "ran_at": now.isoformat(),
        "retention_days": retention,
        "handoff_retention_days": int(settings.handoff_retention_days),
        "behavior_control_retention_days": int(settings.behavior_control_retention_days),
        "dry_run": effective_dry_run,
        "archive_enabled": archive_enabled,
        "archive_path": str(archive_path),
        "cutoff": cutoff_iso,
        "archived_partitions": [],
        "dropped_partitions": [],
        "default_rows_deleted": 0,
        "handoff_rows_deleted": 0,
        "behavior_control_rows_deleted": 0,
        "audit_rows_deleted": 0,
        "interaction_rows_deleted": 0,
        "patterns_pruned": 0,
        "session_snapshots_created": 0,
        "graph_health_scopes_updated": 0,
        "graph_health_alerts": 0,
    }

    with SessionLocal() as db:
        _ensure_retention_runtime_setting(db, retention, archive_enabled, str(archive_path))
        if not effective_dry_run:
            summary["session_snapshots_created"] = _flush_session_snapshots_before_lifecycle(
                db,
                max_per_session=max(1, int(settings.snapshot_max_per_session)),
            )
        partition_rows = db.execute(
            text(
                """
                SELECT c.relname AS partition_name
                FROM pg_inherits i
                JOIN pg_class c ON c.oid = i.inhrelid
                JOIN pg_class p ON p.oid = i.inhparent
                WHERE p.relname = 'events'
                ORDER BY c.relname
                """
            )
        ).mappings().all()

        candidates: list[str] = []
        for row in partition_rows:
            partition_name = str(row["partition_name"])
            month_end = _month_end_from_partition(partition_name)
            if month_end is None or month_end >= cutoff:
                continue
            candidates.append(partition_name)
        candidates = candidates[: max(0, int(settings.lifecycle_max_partitions_per_run))]

        if archive_enabled and not effective_dry_run:
            archive_path.mkdir(parents=True, exist_ok=True)

        for partition_name in candidates:
            if not _PARTITION_RE.match(partition_name):
                continue
            count = int(
                db.execute(text(f'SELECT COUNT(1) FROM "{partition_name}"')).scalar() or 0
            )
            archive_file = archive_path / f"{partition_name}.jsonl"
            if archive_enabled and count > 0:
                if effective_dry_run:
                    summary["archived_partitions"].append(
                        {"partition": partition_name, "rows": count, "path": str(archive_file), "dry_run": True}
                    )
                else:
                    json_rows = db.execute(text(f'SELECT row_to_json(t) FROM "{partition_name}" t')).fetchall()
                    with archive_file.open("w", encoding="utf-8") as handle:
                        for item in json_rows:
                            handle.write(json.dumps(item[0], sort_keys=True, default=str))
                            handle.write("\n")
                    summary["archived_partitions"].append(
                        {"partition": partition_name, "rows": count, "path": str(archive_file)}
                    )

            if not effective_dry_run:
                db.execute(text(f'DROP TABLE IF EXISTS "{partition_name}" CASCADE'))
            summary["dropped_partitions"].append(partition_name)

        if not effective_dry_run:
            max_rows = max(1, int(settings.lifecycle_max_delete_rows_per_run))
            has_events_default = bool(
                db.execute(text("SELECT to_regclass('public.events_default')")).scalar()
            )
            if has_events_default:
                deleted_default = db.execute(
                    text(
                        """
                        WITH doomed AS (
                          SELECT id, ts
                          FROM events_default
                          WHERE ts < :cutoff
                          ORDER BY ts ASC
                          LIMIT :max_rows
                        )
                        DELETE FROM events_default e
                        USING doomed d
                        WHERE e.id = d.id AND e.ts = d.ts
                        """
                    ),
                    {"cutoff": cutoff, "max_rows": max_rows},
                )
                summary["default_rows_deleted"] = _rowcount(deleted_default)
            else:
                summary["default_rows_deleted"] = 0
            deleted_audit = db.execute(
                text("DELETE FROM audit_log WHERE ts < :cutoff"),
                {"cutoff": now - timedelta(days=int(settings.audit_retention_days))},
            )
            summary["audit_rows_deleted"] = _rowcount(deleted_audit)
            deleted_interactions = db.execute(
                text("DELETE FROM agent_interactions WHERE ts < :cutoff"),
                {"cutoff": now - timedelta(days=int(settings.interaction_retention_days))},
            )
            summary["interaction_rows_deleted"] = _rowcount(deleted_interactions)
            handoff_cutoff = now - timedelta(days=max(1, int(getattr(settings, "handoff_retention_days", 90))))
            deleted_handoff = db.execute(
                text(
                    """
                    DELETE FROM handoff_records
                    WHERE expires_at < :now
                       OR ts < :handoff_cutoff
                    """
                ),
                {"now": now, "handoff_cutoff": handoff_cutoff},
            )
            summary["handoff_rows_deleted"] = _rowcount(deleted_handoff)
            behavior_cutoff = now - timedelta(
                days=max(1, int(getattr(settings, "behavior_control_retention_days", 365)))
            )
            behavior_deleted = 0
            for statement in (
                "DELETE FROM capability_grants WHERE created_at < :cutoff",
                "DELETE FROM behavior_shadow_predictions WHERE created_at < :cutoff",
                "DELETE FROM behavior_memory_reviews WHERE resolved_at IS NOT NULL AND resolved_at < :cutoff",
                "DELETE FROM behavior_counterfactuals WHERE resolved_at IS NOT NULL AND resolved_at < :cutoff",
                "DELETE FROM behavior_process_models WHERE status = 'rejected' AND updated_at < :cutoff",
                "DELETE FROM behavior_projection_pilot_assignments WHERE assigned_at < :cutoff",
            ):
                deleted = db.execute(text(statement), {"cutoff": behavior_cutoff})
                behavior_deleted += int(getattr(deleted, "rowcount", 0) or 0)
            summary["behavior_control_rows_deleted"] = behavior_deleted

            pattern_rows = db.execute(text("SELECT id, evidence_event_ids FROM patterns")).fetchall()
            pruned = 0
            for pattern_row in pattern_rows:
                pattern_id = pattern_row[0]
                evidence_ids = pattern_row[1] or []
                if not evidence_ids:
                    continue
                existing = {
                    row[0]
                    for row in db.execute(
                        text("SELECT id FROM events WHERE id = ANY(:ids)"),
                        {"ids": evidence_ids},
                    ).fetchall()
                }
                filtered = [event_id for event_id in evidence_ids if event_id in existing]
                if len(filtered) != len(evidence_ids):
                    db.execute(
                        text("UPDATE patterns SET evidence_event_ids = :ids, updated_at = :updated_at WHERE id = :id"),
                        {"ids": filtered, "updated_at": now, "id": pattern_id},
                    )
                    pruned += 1
            summary["patterns_pruned"] = pruned

        try:
            graph_summary = run_graph_health()
            summary["graph_health_scopes_updated"] = int(graph_summary.get("scopes_scanned") or 0)
            summary["graph_health_alerts"] = int(graph_summary.get("alerts") or 0)
        except Exception as exc:
            summary["graph_health_error"] = str(exc)

        summary["completed_at"] = datetime.now(tz=UTC).isoformat()
        _write_status(db, summary)
        db.commit()
    return summary
