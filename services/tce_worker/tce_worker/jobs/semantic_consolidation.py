from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from redis import Redis
from rq import Queue, Retry
from sqlalchemy import text

from ..config import get_settings
from ..db import SessionLocal


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _pattern_status(confidence: float) -> str:
    if confidence >= 0.80:
        return "active"
    if confidence >= 0.50:
        return "needs_review"
    return "suppressed"


def _domain_from_scope(scope_raw: Any) -> str:
    if isinstance(scope_raw, dict):
        domain = scope_raw.get("domain")
        if isinstance(domain, str) and domain.strip():
            return domain.strip().lower()[:64]
        task = scope_raw.get("task_type")
        if isinstance(task, str) and task.strip():
            return task.strip().lower()[:64]
    if isinstance(scope_raw, str) and scope_raw.strip():
        try:
            parsed = json.loads(scope_raw)
            if isinstance(parsed, dict):
                return _domain_from_scope(parsed)
        except Exception:
            pass
    return "general"


def _to_uuid_list(values: list[Any]) -> list[UUID]:
    out: list[UUID] = []
    for value in values:
        try:
            out.append(UUID(str(value)))
        except Exception:
            continue
    return out


def _upsert_pattern(
    db: Any,
    *,
    domain: str,
    pattern_type: str,
    statement: str,
    evidence_event_ids: list[UUID],
    target_confidence: float,
    support_count: int,
    contradiction_count: int,
    now: datetime,
) -> str | None:
    if not statement.strip():
        return None
    existing = db.execute(
        text(
            """
            SELECT id, confidence, version, evidence_event_ids
            FROM patterns
            WHERE domain = :domain
              AND pattern_type = :pattern_type
              AND statement = :statement
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ),
        {
            "domain": domain,
            "pattern_type": pattern_type,
            "statement": statement,
        },
    ).mappings().first()

    support_ratio = min(1.0, float(max(0, support_count)) / 12.0)
    contradiction_ratio = min(1.0, float(max(0, contradiction_count)) / max(1.0, float(max(0, support_count))))
    target = _clamp(target_confidence + (0.10 * support_ratio) - (0.15 * contradiction_ratio))

    if existing:
        merged_conf = _clamp((float(existing["confidence"] or 0.0) * 0.70) + (target * 0.30))
        merged_evidence = _to_uuid_list(
            list(dict.fromkeys([*(existing.get("evidence_event_ids") or []), *evidence_event_ids]))[:200]
        )
        db.execute(
            text(
                """
                UPDATE patterns
                SET confidence = :confidence,
                    evidence_event_ids = :evidence_event_ids,
                    status = :status,
                    updated_at = :updated_at,
                    version = :version
                WHERE id = :pattern_id
                """
            ),
            {
                "pattern_id": existing["id"],
                "confidence": merged_conf,
                "evidence_event_ids": merged_evidence,
                "status": _pattern_status(merged_conf),
                "updated_at": now,
                "version": int(existing.get("version") or 1) + 1,
            },
        )
        return str(existing["id"])

    pattern_id = uuid.uuid4()
    db.execute(
        text(
            """
            INSERT INTO patterns (
                id, domain, pattern_type, statement, evidence_event_ids,
                confidence, updated_at, version, status
            )
            VALUES (
                :id, :domain, :pattern_type, :statement, :evidence_event_ids,
                :confidence, :updated_at, 1, :status
            )
            """
        ),
        {
            "id": pattern_id,
            "domain": domain,
            "pattern_type": pattern_type,
            "statement": statement,
            "evidence_event_ids": evidence_event_ids[:200],
            "confidence": target,
            "updated_at": now,
            "status": _pattern_status(target),
        },
    )
    return str(pattern_id)


def run(workspace_id: str | None = None, user_id: str | None = None, lookback_days: int = 21) -> dict[str, Any]:
    settings = get_settings()
    if not settings.semantic_consolidation_enabled:
        return {"status": "disabled"}

    now = datetime.now(tz=UTC)
    start_ts = now - timedelta(days=max(1, int(lookback_days or settings.semantic_consolidation_lookback_days)))
    queue_name = getattr(settings, "queue_patterns_name", "tce-default")
    queue = Queue(queue_name, connection=Redis.from_url(settings.redis_url))
    retry = Retry(max=settings.worker_retry_max, interval=settings.retry_intervals)

    where_parts = ["ts >= :start_ts", "sensitivity <= 2"]
    params: dict[str, Any] = {"start_ts": start_ts}
    if workspace_id:
        where_parts.append("(context->>'_tce_workspace') = :workspace_id")
        params["workspace_id"] = workspace_id
    if user_id:
        where_parts.append("(context->>'_tce_owner') = :user_id")
        params["user_id"] = user_id
    where_sql = " AND ".join(where_parts)

    created_or_updated: list[str] = []
    summary_counts = {"facts": 0, "opinions": 0, "experiences": 0, "skills": 0}

    with SessionLocal() as db:
        rows = db.execute(
            text(
                f"""
                SELECT domain, task_type,
                       COUNT(*) AS support_count,
                       SUM(CASE WHEN lower(COALESCE(outcome->>'success', '')) IN ('true','t','1') THEN 1 ELSE 0 END) AS success_count,
                       MAX(ts) AS latest_ts
                FROM events
                WHERE {where_sql}
                GROUP BY domain, task_type
                HAVING COUNT(*) >= 3
                ORDER BY support_count DESC
                LIMIT 120
                """
            ),
            params,
        ).mappings().all()

        for row in rows:
            domain = str(row["domain"] or "general")[:64]
            task_type = str(row["task_type"] or "general")[:64]
            support_count = int(row.get("support_count") or 0)
            success_count = int(row.get("success_count") or 0)
            contradiction_count = max(0, support_count - success_count)
            success_ratio = 0.0 if support_count <= 0 else (float(success_count) / float(support_count))
            evidence_rows = db.execute(
                text(
                    f"""
                    SELECT id
                    FROM events
                    WHERE {where_sql}
                      AND domain = :domain
                      AND task_type = :task_type
                    ORDER BY ts DESC
                    LIMIT 80
                    """
                ),
                {**params, "domain": domain, "task_type": task_type},
            ).mappings().all()
            evidence_ids = _to_uuid_list([item["id"] for item in evidence_rows])
            if not evidence_ids:
                continue

            base_fact = _clamp(0.35 + min(0.45, support_count / 20.0) + (0.20 * success_ratio))
            fact_statement = (
                f"Semantic memory: in {domain}/{task_type}, evidence-backed iterative execution yields reliable outcomes."
            )
            fact_id = _upsert_pattern(
                db,
                domain=domain,
                pattern_type="semantic_fact",
                statement=fact_statement,
                evidence_event_ids=evidence_ids,
                target_confidence=base_fact,
                support_count=support_count,
                contradiction_count=contradiction_count,
                now=now,
            )
            if fact_id:
                created_or_updated.append(fact_id)
                summary_counts["facts"] += 1

            skill_statement = (
                f"Skill: for {domain}/{task_type}, follow diagnose -> narrow scope -> apply minimal change -> validate."
            )
            skill_id = _upsert_pattern(
                db,
                domain=domain,
                pattern_type="skill",
                statement=skill_statement,
                evidence_event_ids=evidence_ids,
                target_confidence=_clamp(0.30 + min(0.40, support_count / 25.0) + (0.25 * success_ratio)),
                support_count=support_count,
                contradiction_count=contradiction_count,
                now=now,
            )
            if skill_id:
                created_or_updated.append(skill_id)
                summary_counts["skills"] += 1

            experience_statement = (
                f"Experience: recent {domain}/{task_type} execution shows success_ratio={success_ratio:.2f} over {support_count} events."
            )
            exp_id = _upsert_pattern(
                db,
                domain=domain,
                pattern_type="experience",
                statement=experience_statement,
                evidence_event_ids=evidence_ids,
                target_confidence=_clamp(0.25 + min(0.35, support_count / 30.0) + (0.20 * success_ratio)),
                support_count=support_count,
                contradiction_count=contradiction_count,
                now=now,
            )
            if exp_id:
                created_or_updated.append(exp_id)
                summary_counts["experiences"] += 1

        rule_where = ["active = true"]
        rule_params: dict[str, Any] = {}
        if workspace_id:
            rule_where.append("workspace_id = :workspace_id")
            rule_params["workspace_id"] = workspace_id
        if user_id:
            rule_where.append("user_id = :user_id")
            rule_params["user_id"] = user_id
        rule_rows = db.execute(
            text(
                f"""
                SELECT statement, priority, scope
                FROM memory_rules
                WHERE {' AND '.join(rule_where)}
                ORDER BY priority ASC, updated_at DESC
                LIMIT 120
                """
            ),
            rule_params,
        ).mappings().all()
        for row in rule_rows:
            statement = str(row.get("statement") or "").strip()
            if not statement:
                continue
            priority = int(row.get("priority") or 2)
            domain = _domain_from_scope(row.get("scope"))
            confidence = _clamp(0.85 - (priority * 0.10))
            pat_id = _upsert_pattern(
                db,
                domain=domain,
                pattern_type="opinion",
                statement=f"Preference: {statement}",
                evidence_event_ids=[],
                target_confidence=confidence,
                support_count=max(1, 8 - priority),
                contradiction_count=0,
                now=now,
            )
            if pat_id:
                created_or_updated.append(pat_id)
                summary_counts["opinions"] += 1

        db.commit()

    for pattern_id in sorted(set(created_or_updated)):
        queue.enqueue("tce_worker.jobs.validation.run", pattern_id, retry=retry)
        queue.enqueue("tce_worker.jobs.workflow.run", pattern_id, retry=retry)

    return {
        "status": "ok",
        "workspace_id": workspace_id,
        "user_id": user_id,
        "lookback_days": int(lookback_days),
        "pattern_updates": len(set(created_or_updated)),
        "typed_counts": summary_counts,
    }

