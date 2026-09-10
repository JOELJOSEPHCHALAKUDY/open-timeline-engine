from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from tce_shared.policy_evaluation import episode_key

from ..config import get_settings
from ..db import SessionLocal


def _success_from_outcome(outcome: Any, event_type: str) -> bool:
    if isinstance(outcome, dict):
        value = str(outcome.get("success", "")).strip().lower()
        if value in {"true", "t", "1"}:
            return True
        if value in {"false", "f", "0"}:
            return False
    normalized = str(event_type or "").strip().upper()
    if normalized in {"ERROR"}:
        return False
    return normalized in {"TASK_DONE", "FIX", "REVIEW"}


def _upsert_experience_pattern(
    db: Any,
    *,
    domain: str,
    statement: str,
    event_id: UUID,
    confidence: float,
    now: datetime,
) -> None:
    existing = db.execute(
        text(
            """
            SELECT id, confidence, version, evidence_event_ids
            FROM patterns
            WHERE domain = :domain
              AND pattern_type = 'experience'
              AND statement = :statement
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ),
        {"domain": domain, "statement": statement},
    ).mappings().first()
    if existing:
        evidence_ids = list(dict.fromkeys([*(existing.get("evidence_event_ids") or []), event_id]))[:200]
        merged_conf = max(0.0, min(1.0, (float(existing.get("confidence") or 0.0) * 0.75) + (confidence * 0.25)))
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
                "evidence_event_ids": evidence_ids,
                "status": "active" if merged_conf >= 0.8 else ("needs_review" if merged_conf >= 0.5 else "suppressed"),
                "updated_at": now,
                "version": int(existing.get("version") or 1) + 1,
            },
        )
        return

    db.execute(
        text(
            """
            INSERT INTO patterns (
                id, domain, pattern_type, statement, evidence_event_ids,
                confidence, updated_at, version, status
            )
            VALUES (
                :id, :domain, 'experience', :statement, :evidence_event_ids,
                :confidence, :updated_at, 1, :status
            )
            """
        ),
        {
            "id": uuid.uuid4(),
            "domain": domain,
            "statement": statement,
            "evidence_event_ids": [event_id],
            "confidence": confidence,
            "updated_at": now,
            "status": "active" if confidence >= 0.8 else ("needs_review" if confidence >= 0.5 else "suppressed"),
        },
    )


def _reflection_attribution(
    db: Any,
    *,
    workspace_id: str,
    subject_user_id: str,
    session_id: str,
    context: dict[str, Any],
) -> dict[str, str | None]:
    """The four P4 learning-scope columns for a reflection-derived observation.

    A reflection has no `decision_opportunity`, so `decision_family` is genuinely unknown and stays
    `NULL` — which is also correct rather than merely honest: `policy_store.load_policy_evidence`
    selects on `decision_family`, and a machine-written reflection summary is not adjudicated human
    evidence for any decision family and must never be retrieved as if it were.

    `project_id`, `task_id` and `episode_key` are not unknown. P2's `task_states` row for this
    session carries the project, the task and the two components (`objective_hash`,
    `last_cancel_seq`) that `policy_evaluation.episode_key` needs, so the key computed here is the
    same key the freeze site computes for the same session — which is the whole point of the column:
    a split may not straddle an episode, and a row with a fabricated or absent key silently can.
    """

    row = (
        db.execute(
            text(
                """
                SELECT project_id, task_id, objective_hash, last_cancel_seq
                FROM task_states
                WHERE workspace_id = :workspace_id AND session_id = :session_id
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"workspace_id": workspace_id, "session_id": session_id},
        )
        .mappings()
        .first()
    )
    project_id = str(context.get("project_id") or "").strip() or None
    task_id: str | None = None
    objective_hash: str | None = None
    cancel_epoch = 0
    if row is not None:
        project_id = str(row.get("project_id") or "").strip() or project_id
        task_id = str(row.get("task_id") or "").strip() or None
        objective_hash = str(row.get("objective_hash") or "").strip() or None
        try:
            cancel_epoch = int(row.get("last_cancel_seq") or 0)
        except (TypeError, ValueError):
            cancel_epoch = 0
    return {
        "project_id": project_id,
        "decision_family": None,
        "task_id": task_id,
        "episode_key": episode_key(
            workspace_id=workspace_id,
            subject_user_id=subject_user_id,
            project_id=project_id,
            session_id=session_id,
            objective_hash=objective_hash,
            cancel_epoch=cancel_epoch,
        ),
    }


def run(event_id: str, workspace_id: str | None = None, user_id: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    if not settings.reflection_enabled:
        return {"status": "disabled"}

    parsed_id = UUID(event_id)
    now = datetime.now(tz=UTC)
    with SessionLocal() as db:
        row = db.execute(
            text(
                """
                SELECT id, domain, task_type, event_type, title, outcome, context
                FROM events
                WHERE id = :event_id
                """
            ),
            {"event_id": parsed_id},
        ).mappings().first()
        if not row:
            return {"status": "missing", "event_id": event_id}

        context_raw = row.get("context")
        context: dict[str, Any] = context_raw if isinstance(context_raw, dict) else {}
        scoped_workspace = workspace_id or str(context.get("_tce_workspace") or "")
        scoped_user = user_id or str(context.get("_tce_owner") or "")
        if not scoped_workspace or not scoped_user:
            return {"status": "skipped", "event_id": event_id, "reason": "missing_scope"}

        session_id = str(context.get("session_id") or "default")
        success = _success_from_outcome(row.get("outcome"), str(row.get("event_type") or ""))
        domain = str(row.get("domain") or "general")[:64]
        task_type = str(row.get("task_type") or "general")[:64]
        title = str(row.get("title") or "")[:180]

        episode_row = db.execute(
            text(
                """
                SELECT id, confidence, stability_score
                FROM episodes
                WHERE workspace_id = :workspace_id
                  AND user_id = :user_id
                  AND session_id = :session_id
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"workspace_id": scoped_workspace, "user_id": scoped_user, "session_id": session_id},
        ).mappings().first()

        if episode_row is None:
            episode_id = uuid.uuid4()
            db.execute(
                text(
                    """
                    INSERT INTO episodes (
                        id, workspace_id, user_id, session_id, goal, context, outcome, confidence,
                        status, authority_score, stability_score, created_at, updated_at
                    )
                    VALUES (
                        :id, :workspace_id, :user_id, :session_id, :goal, :context, :outcome, :confidence,
                        'in_progress', :authority_score, :stability_score, :created_at, :updated_at
                    )
                    """
                ),
                {
                    "id": episode_id,
                    "workspace_id": scoped_workspace,
                    "user_id": scoped_user,
                    "session_id": session_id,
                    "goal": title or f"{domain}/{task_type}",
                    "context": "",
                    "outcome": "success" if success else "failure",
                    "confidence": 0.6 if success else 0.4,
                    "authority_score": 0.5,
                    "stability_score": 0.3,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        else:
            episode_id = UUID(str(episode_row["id"]))
            prior_conf = float(episode_row.get("confidence") or 0.5)
            prior_stability = float(episode_row.get("stability_score") or 0.3)
            next_conf = max(0.0, min(1.0, prior_conf + (0.06 if success else -0.08)))
            next_stability = max(0.0, min(1.0, prior_stability + (0.05 if success else -0.06)))
            db.execute(
                text(
                    """
                    UPDATE episodes
                    SET confidence = :confidence,
                        stability_score = :stability_score,
                        outcome = :outcome,
                        updated_at = :updated_at
                    WHERE id = :episode_id
                    """
                ),
                {
                    "episode_id": episode_id,
                    "confidence": next_conf,
                    "stability_score": next_stability,
                    "outcome": "success" if success else "failure",
                    "updated_at": now,
                },
            )

        do_more = (
            ["Capture decisions before mutation", "Validate with focused checks", "Promote successful workflow as reusable skill"]
            if success
            else ["Narrow scope before retry", "Gather more evidence before action", "Try alternate path with lower risk"]
        )
        do_less = [] if success else ["Large unverified changes", "Repeating same failed strategy"]
        avoid = [] if success else ["Proceeding without permit/claim discipline", "Continuing after repeated failure without diagnosis"]

        db.execute(
            text(
                """
                INSERT INTO episode_lessons (episode_id, do_more_json, do_less_json, avoid_json, updated_at)
                VALUES (
                    :episode_id,
                    CAST(:do_more_json AS jsonb),
                    CAST(:do_less_json AS jsonb),
                    CAST(:avoid_json AS jsonb),
                    :updated_at
                )
                ON CONFLICT (episode_id) DO UPDATE SET
                    do_more_json = excluded.do_more_json,
                    do_less_json = excluded.do_less_json,
                    avoid_json = excluded.avoid_json,
                    updated_at = excluded.updated_at
                """
            ),
            {
                "episode_id": episode_id,
                "do_more_json": json.dumps(do_more),
                "do_less_json": json.dumps(do_less),
                "avoid_json": json.dumps(avoid),
                "updated_at": now,
            },
        )

        db.execute(
            text(
                """
                INSERT INTO episode_event_links (id, episode_id, event_id, created_at)
                VALUES (:id, :episode_id, :event_id, :created_at)
                ON CONFLICT DO NOTHING
                """
            ),
            {"id": uuid.uuid4(), "episode_id": episode_id, "event_id": parsed_id, "created_at": now},
        )

        reflection_statement = (
            f"Reflection: {domain}/{task_type} on '{title}' ended with {'success' if success else 'failure'}; "
            f"{'repeat strategy with guardrails' if success else 'adjust strategy and reduce risk'}."
        )
        _upsert_experience_pattern(
            db,
            domain=domain,
            statement=reflection_statement,
            event_id=parsed_id,
            confidence=0.72 if success else 0.48,
            now=now,
        )

        db.execute(
            text(
                """
                INSERT INTO decision_observations (
                    id, consumer_id, workspace_id, ts, situation_type, situation_summary,
                    context_snapshot, user_response, response_reasoning, outcome,
                    outcome_sentiment, source_event_ids, confidence,
                    project_id, decision_family, task_id, episode_key
                )
                VALUES (
                    :id, :consumer_id, :workspace_id, :ts, :situation_type, :situation_summary,
                    CAST(:context_snapshot AS jsonb), :user_response, :response_reasoning, :outcome,
                    :outcome_sentiment, :source_event_ids, :confidence,
                    :project_id, :decision_family, :task_id, :episode_key
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "consumer_id": scoped_user,
                "workspace_id": scoped_workspace,
                "ts": now,
                "situation_type": "routine_task" if success else "error_occurred",
                "situation_summary": f"Reflection for {domain}/{task_type} on '{title}'",
                "context_snapshot": json.dumps({"session_id": session_id, "event_id": str(parsed_id)}),
                "user_response": "Reinforce successful behavior." if success else "Adjust strategy and retry safely.",
                "response_reasoning": reflection_statement[:500],
                "outcome": "success" if success else "failure",
                "outcome_sentiment": "positive" if success else "negative",
                "source_event_ids": [parsed_id],
                "confidence": 0.72 if success else 0.62,
                **_reflection_attribution(
                    db,
                    workspace_id=scoped_workspace,
                    subject_user_id=scoped_user,
                    session_id=session_id,
                    context=context,
                ),
            },
        )

        fp_row = db.execute(
            text(
                """
                SELECT fingerprint, observation_count
                FROM behavioral_fingerprints
                WHERE consumer_id = :consumer_id
                  AND workspace_id = :workspace_id
                LIMIT 1
                """
            ),
            {"consumer_id": scoped_user, "workspace_id": scoped_workspace},
        ).mappings().first()
        if fp_row and isinstance(fp_row.get("fingerprint"), dict):
            fingerprint = fp_row["fingerprint"]
            emotional = fingerprint.setdefault("emotional_patterns", {})
            frustration = emotional.setdefault("frustration_triggers", [])
            satisfaction = emotional.setdefault("satisfaction_signals", [])
            stress = emotional.setdefault("stress_indicators", [])
            token = f"{domain}:{task_type}"
            if success:
                if token not in satisfaction:
                    satisfaction.append(token)
                if token in frustration:
                    frustration = [item for item in frustration if item != token]
                    emotional["frustration_triggers"] = frustration
            else:
                if token not in frustration:
                    frustration.append(token)
                if token not in stress:
                    stress.append(token)
            db.execute(
                text(
                    """
                    UPDATE behavioral_fingerprints
                    SET fingerprint = CAST(:fingerprint AS jsonb),
                        observation_count = :observation_count,
                        last_updated_at = :updated_at
                    WHERE consumer_id = :consumer_id
                      AND workspace_id = :workspace_id
                    """
                ),
                {
                    "fingerprint": json.dumps(fingerprint),
                    "observation_count": int(fp_row.get("observation_count") or 0) + 1,
                    "updated_at": now,
                    "consumer_id": scoped_user,
                    "workspace_id": scoped_workspace,
                },
            )

        db.commit()
        return {
            "status": "ok",
            "event_id": event_id,
            "episode_id": str(episode_id),
            "session_id": session_id,
            "success": success,
        }
