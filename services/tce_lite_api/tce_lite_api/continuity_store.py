from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from tce_shared.autonomy_context import SUMMARY_VERSION, summarize_event_record
from tce_shared.continuity import progress_patch, summarize_attempts
from tce_shared.handoff import normalize_objective_text
from tce_shared.redaction import redact_payload, redact_text


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _safe_change_summary(raw: Any) -> tuple[dict[str, dict[str, Any]], bool]:
    if not isinstance(raw, dict):
        return {}, False
    output: dict[str, dict[str, Any]] = {}
    redacted_any = False
    for path, value in list(raw.items())[:40]:
        safe_path, path_redacted = redact_text(str(path or "").strip()[:240])
        if not safe_path:
            continue
        item = value if isinstance(value, dict) else {}
        intent, intent_redacted = redact_text(str(item.get("intent") or "").strip()[:160])
        try:
            added = max(0, int(item.get("added", item.get("added_lines", 0)) or 0))
        except (TypeError, ValueError):
            added = 0
        try:
            removed = max(0, int(item.get("removed", item.get("removed_lines", 0)) or 0))
        except (TypeError, ValueError):
            removed = 0
        output[safe_path] = {"added": added, "removed": removed, "intent": intent}
        redacted_any = redacted_any or bool(path_redacted) or bool(intent_redacted)
    return output, redacted_any


def enqueue_handoff(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    behavior_subject_id: str,
    session_id: str,
    directive_id: uuid.UUID | None,
    completion_key: str,
    terminal_state: str,
    milestone: dict[str, Any],
    source: str,
    redaction_applied: bool,
    now: datetime | None = None,
) -> sqlite3.Row:
    created_at = (now or datetime.now(tz=UTC)).astimezone(UTC)
    conn.execute(
        """
        INSERT OR IGNORE INTO handoff_outbox(
            id, workspace_id, owner_id, behavior_subject_id, session_id, directive_id,
            completion_key, terminal_state, milestone_json, source, status, attempts,
            next_attempt_at, event_id, handoff_record_id, redaction_applied,
            created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            workspace_id,
            owner_id,
            behavior_subject_id,
            session_id,
            str(directive_id) if directive_id else None,
            str(completion_key)[:240],
            terminal_state,
            _dumps(milestone),
            str(source or "native")[:64],
            created_at.isoformat(),
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            1 if redaction_applied else 0,
            created_at.isoformat(),
            created_at.isoformat(),
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM handoff_outbox
        WHERE workspace_id = ? AND owner_id = ? AND completion_key = ?
        """,
        (workspace_id, owner_id, str(completion_key)[:240]),
    ).fetchone()
    if row is None:
        raise RuntimeError("handoff outbox insert failed")
    return cast(sqlite3.Row, row)


def deliver_handoff(conn: sqlite3.Connection, *, outbox_id: str, retention_days: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM handoff_outbox WHERE id = ?", (outbox_id,)).fetchone()
    if row is None:
        raise KeyError(outbox_id)
    if str(row["status"]) == "delivered":
        return cast(sqlite3.Row, row)
    now = datetime.now(tz=UTC)
    created_at = datetime.fromisoformat(str(row["created_at"]))
    milestone_value = _loads(row["milestone_json"], {})
    milestone: dict[str, Any] = milestone_value if isinstance(milestone_value, dict) else {}
    payload_root_value = milestone.get("payload")
    payload_root: dict[str, Any] = payload_root_value if isinstance(payload_root_value, dict) else {}
    outcome_value = milestone.get("outcome")
    outcome: dict[str, Any] = outcome_value if isinstance(outcome_value, dict) else {}
    files = [str(value)[:240] for value in list(payload_root.get("files") or [])[:40]]
    payload = {
        "directive_id": row["directive_id"],
        "files": files,
        "decision": str(milestone.get("decision") or "")[:500],
        "outcome": outcome,
        "git": dict(milestone.get("git") or {}),
        "anchors": list(milestone.get("anchors") or [])[:40],
        "milestone_schema": "v1",
        "workspace_id": row["workspace_id"],
        "user_id": row["owner_id"],
    }
    redacted_payload, payload_redacted = redact_payload(payload)
    payload = redacted_payload if isinstance(redacted_payload, dict) else {}
    title, title_redacted = redact_text(str(milestone.get("title") or "Completion captured")[:160])
    context = {
        "session_id": row["session_id"],
        "_tce_workspace": row["workspace_id"],
        "_tce_owner": row["owner_id"],
        "_tce_behavior_subject": row["behavior_subject_id"],
    }
    event_outcome = {
        "success": row["terminal_state"] == "succeeded",
        "metrics": {"state": row["terminal_state"], "outbox_id": row["id"]},
        "followups": [str(outcome.get("next_step") or "")[:300]],
    }
    summary_l0, summary_l1 = summarize_event_record(
        title=title,
        task_type="completion_handoff",
        domain="continuity",
        payload=payload,
        decision=None,
        outcome=event_outcome,
    )
    canonical = _dumps({"title": title, "payload": payload, "context": context, "ts": row["created_at"]})
    conn.execute(
        """
        INSERT OR IGNORE INTO events(
            id, ts, actor, source, domain, task_type, event_type, title,
            summary_l0, summary_l1_json, summary_version, summary_updated_at,
            payload, context, inputs, steps, decision, outcome, style, links,
            tags, sensitivity, redaction_hints, hash, schema_version,
            source_id, source_seq, vector_clock, idempotency_key, authority_level
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["event_id"],
            row["created_at"],
            row["owner_id"],
            "tce-completion-outbox",
            "continuity",
            "completion_handoff",
            "TASK_DONE" if row["terminal_state"] == "succeeded" else "TASK_DECISION",
            title,
            summary_l0,
            _dumps(summary_l1),
            SUMMARY_VERSION,
            row["created_at"],
            _dumps(payload),
            _dumps(context),
            "{}",
            "[]",
            None,
            _dumps(event_outcome),
            None,
            None,
            _dumps(["handoff", "completion", "milestone-v1"]),
            1,
            "[]",
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            1,
            row["id"],
            1,
            "{}",
            f"completion:{row['completion_key']}",
            "high",
        ),
    )
    decision_text = str(milestone.get("decision") or "")[:500]
    next_step = str(outcome.get("next_step") or "")[:300]
    objective_text, objective_redacted = normalize_objective_text(
        title=title,
        decision=decision_text,
        next_step=next_step,
    )
    change_summary, summary_redacted = _safe_change_summary(
        milestone.get("change_summary_json") or milestone.get("change_summary")
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO handoff_records(
            id, workspace_id, owner_id, session_id, directive_id, ts, title, decision,
            next_step, status, files_json, anchors_json, git_json, change_summary_json,
            objective_text, source, event_id, schema_version, redaction_applied, expires_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1', ?, ?)
        """,
        (
            row["handoff_record_id"],
            row["workspace_id"],
            row["owner_id"],
            row["session_id"],
            row["directive_id"],
            row["created_at"],
            title,
            decision_text,
            next_step,
            row["terminal_state"],
            _dumps(files),
            _dumps(list(milestone.get("anchors") or [])[:40]),
            _dumps(dict(milestone.get("git") or {})),
            _dumps(change_summary),
            objective_text,
            row["source"],
            row["event_id"],
            1
            if bool(row["redaction_applied"] or payload_redacted or title_redacted or objective_redacted or summary_redacted)
            else 0,
            (created_at + timedelta(days=max(1, retention_days))).isoformat(),
        ),
    )
    conn.execute(
        """
        UPDATE capability_grants
        SET completion_outbox_id = ?, completion_recorded_at = ?
        WHERE workspace_id = ? AND owner_id = ? AND session_id = ?
          AND completion_required = 1 AND status = 'consumed' AND completion_recorded_at IS NULL
        """,
        (row["id"], now.isoformat(), row["workspace_id"], row["owner_id"], row["session_id"]),
    )
    conn.execute(
        """
        UPDATE handoff_outbox
        SET status = 'delivered', attempts = attempts + 1, last_error = NULL,
            delivered_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (now.isoformat(), now.isoformat(), row["id"]),
    )
    conn.commit()
    delivered = conn.execute("SELECT * FROM handoff_outbox WHERE id = ?", (outbox_id,)).fetchone()
    if delivered is None:
        raise RuntimeError("delivered outbox disappeared")
    return cast(sqlite3.Row, delivered)


def deliver_handoff_safely(conn: sqlite3.Connection, *, outbox_id: str, retention_days: int) -> sqlite3.Row:
    try:
        return deliver_handoff(conn, outbox_id=outbox_id, retention_days=retention_days)
    except Exception as exc:
        conn.rollback()
        row = conn.execute("SELECT attempts FROM handoff_outbox WHERE id = ?", (outbox_id,)).fetchone()
        if row is None:
            raise
        attempts = int(row["attempts"] or 0) + 1
        now = datetime.now(tz=UTC)
        conn.execute(
            """
            UPDATE handoff_outbox
            SET status = ?, attempts = ?, last_error = ?, next_attempt_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                "dead" if attempts >= 10 else "pending",
                attempts,
                str(exc)[:1000],
                (now + timedelta(seconds=min(3600, 2 ** min(attempts, 10)))).isoformat(),
                now.isoformat(),
                outbox_id,
            ),
        )
        conn.commit()
        result = conn.execute("SELECT * FROM handoff_outbox WHERE id = ?", (outbox_id,)).fetchone()
        if result is None:
            raise
        return cast(sqlite3.Row, result)


def drain_pending_handoffs(
    conn: sqlite3.Connection,
    *,
    retention_days: int,
    limit: int = 100,
) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT id FROM handoff_outbox
        WHERE status = 'pending' AND next_attempt_at <= ?
        ORDER BY created_at ASC LIMIT ?
        """,
        (datetime.now(tz=UTC).isoformat(), max(1, min(500, int(limit)))),
    ).fetchall()
    counts = {"processed": 0, "delivered": 0, "pending": 0, "dead": 0}
    for row in rows:
        result = deliver_handoff_safely(conn, outbox_id=str(row["id"]), retention_days=retention_days)
        status = str(result["status"])
        counts["processed"] += 1
        if status in counts:
            counts[status] += 1
    return counts


def record_resume_attempt(
    conn: sqlite3.Connection,
    *,
    packet_id: uuid.UUID,
    workspace_id: str,
    requesting_owner_id: str,
    target_owner_id: str,
    session_id: str,
    selected_record_id: str,
    query_text: str,
    top_file: str | None,
    recommended_files: list[str],
    requested_at: datetime,
    returned_at: datetime,
    handoff_ts: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO continuity_resume_attempts(
            id, packet_id, workspace_id, requesting_owner_id, target_owner_id,
            session_id, selected_record_id, query_text, top_file, requested_at, returned_at,
            latency_ms, time_since_handoff_ms, recommended_files_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            str(packet_id),
            workspace_id,
            requesting_owner_id,
            target_owner_id,
            str(session_id or "default")[:160],
            selected_record_id,
            query_text[:500],
            (top_file or "")[:240] or None,
            requested_at.isoformat(),
            returned_at.isoformat(),
            max(0, int((returned_at - requested_at).total_seconds() * 1000)),
            max(0, int((requested_at - handoff_ts).total_seconds() * 1000)),
            _dumps([str(value)[:240] for value in recommended_files[:40]]),
        ),
    )
    conn.commit()


def record_resume_progress(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    requesting_owner_id: str,
    packet_id: str | None = None,
    session_id: str | None = None,
    phase: str,
    opened_file: str | None = None,
    correct_file: bool | None = None,
    correct_anchor: bool | None = None,
    opened_file_rank: int | None = None,
    correction_required: bool | None = None,
    correction_reason: str | None = None,
    archaeology_tool_calls: int | None = None,
    archaeology_tokens: int | None = None,
    outcome_status: str | None = None,
    progress_source: str = "manual",
    max_age_hours: int = 24,
) -> tuple[sqlite3.Row, str] | None:
    clauses = ["workspace_id = ?", "requesting_owner_id = ?", "requested_at >= ?"]
    params: list[Any] = [
        workspace_id,
        requesting_owner_id,
        (datetime.now(tz=UTC) - timedelta(hours=max(1, max_age_hours))).isoformat(),
    ]
    if packet_id:
        clauses.append("packet_id = ?")
        params.append(str(packet_id))
    if session_id:
        clauses.append("session_id = ?")
        params.append(str(session_id)[:160])
    row = conn.execute(
        f"""
        SELECT * FROM continuity_resume_attempts
        WHERE {" AND ".join(clauses)}
        ORDER BY requested_at DESC LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        return None
    safe_file, _ = redact_text(str(opened_file or "")[:240])
    safe_reason, _ = redact_text(str(correction_reason or "")[:500])
    patch = progress_patch(
        {
            "recommended_files_json": row["recommended_files_json"],
            "first_file_opened_at": row["first_file_opened_at"],
            "productive_at": row["productive_at"],
            "completed_at": row["completed_at"],
        },
        phase=phase,
        now=datetime.now(tz=UTC),
        opened_file=safe_file or None,
        correct_file=correct_file,
        correct_anchor=correct_anchor,
        opened_file_rank=opened_file_rank,
        correction_required=correction_required,
        correction_reason=safe_reason if correction_reason is not None else None,
        archaeology_tool_calls=archaeology_tool_calls,
        archaeology_tokens=archaeology_tokens,
        outcome_status=outcome_status,
        progress_source=progress_source,
    )
    normalized_phase = str(patch.pop("phase"))
    bool_fields = {"correct_file", "correct_anchor", "correction_required"}
    datetime_fields = {"first_file_opened_at", "productive_at", "completed_at", "feedback_at"}
    assignments: list[str] = []
    values: list[Any] = []
    for key, value in patch.items():
        assignments.append(f"{key} = ?")
        if key in bool_fields and value is not None:
            values.append(1 if value else 0)
        elif key in datetime_fields and isinstance(value, datetime):
            values.append(value.isoformat())
        else:
            values.append(value)
    values.append(str(row["id"]))
    conn.execute(
        f"UPDATE continuity_resume_attempts SET {', '.join(assignments)} WHERE id = ?",
        values,
    )
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM continuity_resume_attempts WHERE id = ?",
        (str(row["id"]),),
    ).fetchone()
    assert updated is not None
    return updated, normalized_phase


def pilot_metrics(conn: sqlite3.Connection, *, workspace_id: str, days: int) -> dict[str, Any]:
    since = datetime.now(tz=UTC) - timedelta(days=max(1, days))
    coverage = conn.execute(
        """
        WITH eligible AS (
            SELECT 'directive:' || directive_id AS completion_key
            FROM directive_executions
            WHERE workspace_id = ? AND updated_at >= ?
              AND state IN ('succeeded', 'failed', 'blocked', 'abandoned')
            UNION
            SELECT 'directive:' || directive_id AS completion_key
            FROM capability_grants
            WHERE workspace_id = ? AND consumed_at >= ?
              AND completion_required = 1 AND status = 'consumed' AND directive_id IS NOT NULL
            UNION
            SELECT 'standalone:' || id AS completion_key
            FROM handoff_outbox
            WHERE workspace_id = ? AND created_at >= ? AND directive_id IS NULL
        ), captured AS (
            SELECT DISTINCT CASE WHEN directive_id IS NULL THEN 'standalone:' || id
                                 ELSE 'directive:' || directive_id END AS completion_key
            FROM handoff_outbox
            WHERE workspace_id = ? AND created_at >= ? AND status = 'delivered'
        )
        SELECT COUNT(*) AS eligible,
               SUM(CASE WHEN captured.completion_key IS NOT NULL THEN 1 ELSE 0 END) AS captured
        FROM eligible LEFT JOIN captured USING (completion_key)
        """,
        (
            workspace_id,
            since.isoformat(),
            workspace_id,
            since.isoformat(),
            workspace_id,
            since.isoformat(),
            workspace_id,
            since.isoformat(),
        ),
    ).fetchone()
    outbox = conn.execute(
        """
        SELECT SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN status = 'dead' THEN 1 ELSE 0 END) AS dead
        FROM handoff_outbox WHERE workspace_id = ? AND created_at >= ?
        """,
        (workspace_id, since.isoformat()),
    ).fetchone()
    attempts = conn.execute(
        """
        SELECT requested_at, latency_ms, time_since_handoff_ms, first_file_opened_at,
               productive_at, completed_at, opened_file_rank, correct_file, correct_anchor,
               correction_required, archaeology_tool_calls, archaeology_tokens, feedback_at
        FROM continuity_resume_attempts WHERE workspace_id = ? AND requested_at >= ?
        """,
        (workspace_id, since.isoformat()),
    ).fetchall()
    eligible = int(coverage["eligible"] or 0) if coverage else 0
    captured = int(coverage["captured"] or 0) if coverage else 0
    attempt_metrics = summarize_attempts(dict(row) for row in attempts)
    return {
        "eligible_completion_count": eligible,
        "captured_completion_count": captured,
        "handoff_capture_coverage": float(captured / eligible) if eligible else 0.0,
        "outbox_pending_count": int(outbox["pending"] or 0) if outbox else 0,
        "outbox_dead_count": int(outbox["dead"] or 0) if outbox else 0,
        **attempt_metrics,
    }
