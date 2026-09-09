"""Dream synthesis: what this person keeps returning to, in their own words.

Noticing a throughline across fifty messages is what a model is for and what counting cannot
do — frequency in a chat log ranks boilerplate first, because boilerplate is the only thing
that repeats word for word. That call used to happen inside the takeover request thread. It
happens here instead, under the same lease, staleness and fence discipline as plan
decomposition.

Unlike planning, this job writes **discovery** rows: no root goal, no dependencies, nothing
mutating. Its only task-state event is ``PLAN_REQUESTED``, for provenance — a dream is a
candidate, not an approved plan.

Off by default (``takeover_dream_llm_enabled``): a model call nobody asked for is a cost
nobody agreed to.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_api.crypto import maybe_decrypt_payload
from tce_api.db import get_session_factory
from tce_api.plan_rows import resolved_scope_from_json, write_dream_rows
from tce_api.planning_store import claim_planning_job, complete_planning_job
from tce_api.task_state_store import apply_task_state_events, load_task_state
from tce_model_gateway.factory import get_gateway
from tce_shared.decision_capture import HUMAN_INPUT_TASK_TYPE
from tce_shared.dreams import DreamSeed
from tce_shared.scope import ResolvedScope
from tce_shared.task_state import (
    PLANNING_JOB_KIND_DREAM,
    PLANNING_LOST_LEASE_REASON,
    PLANNING_PRODUCER_MODEL,
    PLANNING_STALE_REASON,
    TASK_STATE_POLICY_REVISION,
    TaskStateEvent,
    TaskStateEventKind,
    TaskStatePreconditionFailed,
    TaskStateProjection,
    TaskStateRevisionConflict,
    plan_input_revision,
    planning_result_is_stale,
    task_scope_digest,
)

from ..config import get_settings
from .planning import _worker_plan_gateway_settings

LOGGER = logging.getLogger(__name__)

_COUNT_KEYS = ("processed", "succeeded", "failed", "discarded", "cancelled", "skipped")
def _zero_counts() -> dict[str, int]:
    return dict.fromkeys(_COUNT_KEYS, 0)


def _lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


DREAM_PROMPT = """Below are real messages a developer sent to their coding assistant, newest first.

Work out what this person is actually trying to get done. Not what they asked for in any
one message -- what they keep returning to.

Then write it the way THEY would write it. Look at how they type in these messages and
match it. They are one person building alone, at a keyboard, with no team.

Hard rules on wording:
- Write it as they'd say it out loud. Short. Plain. Lower case is fine.
- Name the actual thing. "get the witness engine in front of someone" beats
  "validate market positioning". "stop the CI failing" beats "improve pipeline health".
- Banned words: comprehensive, leverage, stakeholder, framework, roadmap, strategy,
  ecosystem, robust, holistic, real-world, best practice, optimize, streamline.
- No Title Case. No consultant voice. If it reads like a slide, rewrite it.
- It has to be something they could have typed themselves.

Hard rules on content:
- Only name something you can point at specific messages for.
- Ignore interruptions, pasted links, one-word replies and tool output.
- Something said once but clearly counts for more than boilerplate repeated ten times.
- If nothing clear comes through, return an empty list. That is a good answer.
- At most 3.

Messages:
__MESSAGES__

Reply with JSON only:
{"dreams": [{"title": "the goal, in their words", "why": "what makes you say that",
"message_numbers": [1, 4, 9]}]}
"""


def _recent_messages_for_dreaming(
    db: Session, *, scope: ResolvedScope, limit: int = 60, subject_user_id: str | None = None
) -> list[tuple[str, str]]:
    """Return (event_id, the real message text) for recent human messages.

    Moved verbatim out of the API layer. Reads the encrypted payload rather than the title:

    `title` caps at 150 characters and a quarter of rows sit at that ceiling, so every
    earlier attempt at this was reading sentence fragments — and the longest, most
    considered messages, the ones most likely to say what someone wants, were exactly the
    ones cut off.
    """
    settings = get_settings()
    # Scoped to the caller's workspace/owner (and bound project): never read another workspace's words.
    message_params: dict[str, Any] = {
        "max_sensitivity": settings.block_sensitivity - 1,
        "limit": limit,
        "workspace_id": scope.workspace_id,
        "owner_id": scope.owner_id,
        "subject_user_id": subject_user_id or scope.owner_id,
    }
    project_clause = ""
    if scope.is_bound() and scope.project_id:
        project_clause = "AND context->>'project_id' = :project_id"
        message_params["project_id"] = scope.project_id
    # Live receipts (task_type='human_input') are accepted only when a trusted receipt row binds the
    # event to this behavior subject; historical backfills keep the owner-scoped rule.
    rows = db.execute(
        text(
            f"""
            SELECT id, payload
            FROM events
            WHERE task_type IN ('human_input_backfill', '{HUMAN_INPUT_TASK_TYPE}')
              AND sensitivity <= :max_sensitivity
              AND context->>'_tce_workspace' = :workspace_id
              AND (
                    (task_type = 'human_input_backfill' AND context->>'_tce_owner' = :owner_id)
                 OR (task_type = '{HUMAN_INPUT_TASK_TYPE}' AND EXISTS (
                        SELECT 1 FROM trusted_input_receipts r
                        WHERE r.event_id = events.id AND r.subject_user_id = :subject_user_id
                    ))
              )
              {project_clause}
            ORDER BY ts DESC
            LIMIT :limit
            """
        ),
        message_params,
    ).mappings().all()
    out: list[tuple[str, str]] = []
    for row in rows:
        try:
            payload = maybe_decrypt_payload(row["payload"] or {})
        except Exception:
            continue
        body = str(payload.get("input_excerpt") or "").strip()
        if len(body) < 25:
            continue  # acknowledgements, not intentions
        out.append((str(row["id"]), body[:1200]))
    return out


def _dream_seeds_from_payload(
    payload: Any, *, messages: Sequence[tuple[str, str]], project_id: str = ""
) -> list[DreamSeed]:
    """The parsing half of the deleted API-side dream function, moved verbatim.

    Every seed must cite messages that actually exist: no citation, no dream. That rule is
    what stops the model inventing an aspiration and attributing it to the person.
    """
    raw = payload.get("dreams") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    seeds: list[DreamSeed] = []
    for item in raw[:3]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        why = str(item.get("why") or "").strip()
        numbers = item.get("message_numbers")
        if not title or not isinstance(numbers, list) or not numbers:
            continue  # no citation, no dream
        cited = [
            messages[int(n) - 1][0]
            for n in numbers[:10]
            if str(n).lstrip("-").isdigit() and 0 <= int(n) - 1 < len(messages)
        ]
        if not cited:
            continue  # cited nothing that exists
        seeds.append(
            DreamSeed(
                title=title[:140],
                description=(why or title)[:240],
                rationale=f"From {len(cited)} of your own messages. {why}"[:400],
                # Confidence follows how much of the person's own writing backs it.
                weight=min(0.95, 0.55 + (0.08 * len(cited))),
                evidence_event_ids=tuple(cited),
                project_id=project_id,
            )
        )
    return seeds




def _reread_cancel_requested(db: Session, job_id: str) -> bool:
    value = db.execute(
        text("SELECT cancel_requested FROM planning_jobs WHERE id = CAST(:job_id AS UUID)"),
        {"job_id": job_id},
    ).scalar_one_or_none()
    return bool(value)


def _current_input_revision(projection: TaskStateProjection, scope: ResolvedScope) -> str:
    return plan_input_revision(
        objective_hash=projection.objective_hash,
        contract_revision=projection.contract_revision,
        policy_revision=TASK_STATE_POLICY_REVISION,
        scope_digest=task_scope_digest(
            workspace_id=scope.workspace_id,
            executor_id=scope.executor_id,
            owner_ids=scope.sql_owner_ids(),
            subject_user_id=scope.subject_user_id,
            project_id=scope.project_id,
            project_binding=scope.project_binding,
        ),
        cancel_epoch=projection.last_cancel_seq,
    )


def _json_field(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value


def _backoff(settings: Any, attempts: int, now: datetime) -> datetime | None:
    delay = min(
        int(getattr(settings, "planning_job_backoff_cap_seconds", 900)),
        5 * (2 ** max(0, attempts - 1)),
    )
    return now + timedelta(seconds=delay)


def _complete(
    db: Session,
    *,
    job_id: str,
    lease_owner: str,
    state: str,
    producer: str | None,
    result: dict[str, Any],
    last_error: str | None,
    next_attempt_at: datetime | None,
    now: datetime,
) -> bool:
    fenced = complete_planning_job(
        db,
        job_id=job_id,
        lease_owner=lease_owner,
        state=state,
        producer=producer,
        result=result,
        last_error=last_error,
        next_attempt_at=next_attempt_at,
        now=now,
    )
    db.commit()
    return fenced


def run(job_id: str | None = None, batch_size: int | None = None) -> dict[str, Any]:
    """Synthesise dreams for one job (``job_id``) or a batch. Never raises.

    Identical signature and lifecycle to ``planning.run``; the differences are the job kind,
    the prompt, and that it writes discovery rows rather than plan step rows.
    """
    settings = get_settings()
    counts = _zero_counts()
    if not bool(getattr(settings, "planning_enabled", True)):
        return {"status": "disabled", **counts}
    if not bool(getattr(settings, "takeover_dream_llm_enabled", False)):
        return {"status": "disabled", **counts}

    now = datetime.now(tz=UTC)
    lease_owner = _lease_owner()
    session_factory = get_session_factory()

    try:
        with session_factory() as db:
            rows = claim_planning_job(
                db,
                job_id=job_id,
                lease_owner=lease_owner,
                lease_seconds=int(getattr(settings, "planning_job_lease_seconds", 120)),
                now=now,
                batch_size=int(batch_size or 0) or int(getattr(settings, "planning_job_batch_size", 20) or 20),
            )
    except Exception:
        LOGGER.warning("dream synthesis claim failed", exc_info=True)
        return {"status": "ok", "reason": "claim_failed", **counts}

    if not rows:
        return {"status": "skipped", **counts}

    last_status = "ok"
    last_reason = ""
    for row in rows:
        if str(row.get("job_kind") or "") != PLANNING_JOB_KIND_DREAM:
            counts["skipped"] += 1
            continue
        try:
            outcome = _process_job(dict(row), settings=settings, lease_owner=lease_owner)
        except Exception:
            LOGGER.warning("dream synthesis crashed for %s", row.get("id"), exc_info=True)
            outcome = {"status": "failed"}
        last_status = str(outcome.get("status") or "ok")
        last_reason = str(outcome.get("reason") or "")
        counts["processed"] += 1
        if last_status in counts:
            counts[last_status] += 1

    if job_id:
        # The single-job form is what tests and the probe call: they need to know WHY, not
        # just that something was discarded.
        result: dict[str, Any] = {"status": last_status, **counts}
        if last_reason:
            result["reason"] = last_reason
        return result
    return {"status": "ok", **counts}


def _process_job(row: dict[str, Any], *, settings: Any, lease_owner: str) -> dict[str, Any]:
    job_id = str(row["id"])
    now = datetime.now(tz=UTC)
    session_factory = get_session_factory()

    with session_factory() as db:
        loaded = load_task_state(
            db,
            workspace_id=str(row["workspace_id"]),
            owner_id=str(row["owner_id"]),
            task_id=str(row["task_id"]),
        )
        if loaded is None:
            _complete(
                db,
                job_id=job_id,
                lease_owner=lease_owner,
                state="discarded",
                producer=None,
                result={"reason": "task_state_missing"},
                last_error="task_state_missing",
                next_attempt_at=None,
                now=now,
            )
            return {"status": "discarded", "reason": "task_state_missing"}

        projection, _highest_seq, _source_revision = loaded
        scope = resolved_scope_from_json(_json_field(row.get("scope_json")) or {})
        stale, reason = planning_result_is_stale(
            job_input_revision=str(row.get("input_revision") or ""),
            job_contract_revision=int(row.get("contract_revision") or 0),
            current_input_revision=_current_input_revision(projection, scope),
            current_contract_revision=projection.contract_revision,
            cancel_requested=bool(row.get("cancel_requested")),
        )
        if stale:
            state = "cancelled" if reason == "cancelled" else "discarded"
            _complete(
                db,
                job_id=job_id,
                lease_owner=lease_owner,
                state=state,
                producer=None,
                result={"reason": reason},
                last_error=None if state == "cancelled" else PLANNING_STALE_REASON,
                next_attempt_at=None,
                now=now,
            )
            return {"status": state, "reason": reason}

        try:
            messages = _recent_messages_for_dreaming(db, scope=scope, subject_user_id=scope.subject_user_id)
            if len(messages) < 10:
                # Not enough of the person's own writing to say anything honest about it.
                _complete(
                    db,
                    job_id=job_id,
                    lease_owner=lease_owner,
                    state="succeeded",
                    producer=PLANNING_PRODUCER_MODEL,
                    result={"dreams": 0, "reason": "insufficient_messages"},
                    last_error=None,
                    next_attempt_at=None,
                    now=now,
                )
                return {"status": "succeeded", "reason": "insufficient_messages"}
            numbered = "\n\n".join(f"{i}. {body}" for i, (_, body) in enumerate(messages, start=1))
            gateway = get_gateway(_worker_plan_gateway_settings(settings))
            payload = gateway.extract_structured(
                DREAM_PROMPT.replace("__MESSAGES__", numbered[:24000]), "dream_formation_v1"
            )
        except Exception as exc:
            attempts = int(row.get("attempts") or 0)
            max_attempts = int(row.get("max_attempts") or 3)
            _complete(
                db,
                job_id=job_id,
                lease_owner=lease_owner,
                state="failed",
                producer=None,
                result={"reason": "dream_call_failed"},
                last_error=str(exc)[:400] or "dream_call_failed",
                next_attempt_at=None if attempts >= max_attempts else _backoff(settings, attempts, now),
                now=now,
            )
            return {"status": "failed", "reason": "dream_call_failed"}

        seeds = _dream_seeds_from_payload(
            payload, messages=messages, project_id=str(scope.project_id or "")
        )

        def _revalidate(fresh: TaskStateProjection) -> tuple[bool, str]:
            fresh_stale, fresh_reason = planning_result_is_stale(
                job_input_revision=str(row.get("input_revision") or ""),
                job_contract_revision=int(row.get("contract_revision") or 0),
                current_input_revision=_current_input_revision(fresh, scope),
                current_contract_revision=fresh.contract_revision,
                cancel_requested=_reread_cancel_requested(db, job_id),
            )
            return (not fresh_stale), fresh_reason

        try:
            # Lock 1 first, exactly as in planning. PLAN_REQUESTED only: a dream is a
            # candidate, never an approved plan.
            apply_task_state_events(
                db,
                workspace_id=str(row["workspace_id"]),
                owner_id=str(row["owner_id"]),
                session_id=str(row["session_id"]),
                task_id=str(row["task_id"]),
                new_events=[
                    TaskStateEvent(
                        seq=0,
                        kind=TaskStateEventKind.PLAN_REQUESTED,
                        contract_revision=projection.contract_revision,
                        payload={"producer": PLANNING_PRODUCER_MODEL, "job_kind": PLANNING_JOB_KIND_DREAM},
                        occurred_at=now,
                        actor="worker:dream_synthesis",
                    )
                ],
                now=now,
                expected_revision=projection.revision,
                retry_once=True,
                revalidate=_revalidate,
            )
            goal_ids = write_dream_rows(
                db,
                scope=scope,
                session_id=str(row["session_id"]),
                seeds=seeds,
                contract_revision=projection.contract_revision,
                now=now,
            )
            fenced = complete_planning_job(
                db,
                job_id=job_id,
                lease_owner=lease_owner,
                state="succeeded",
                producer=PLANNING_PRODUCER_MODEL,
                result={"dreams": len(goal_ids)},
                last_error=None,
                next_attempt_at=None,
                now=now,
            )
            if not fenced:
                db.rollback()
                return {"status": PLANNING_LOST_LEASE_REASON}
            db.commit()
        except (TaskStatePreconditionFailed, TaskStateRevisionConflict):
            db.rollback()
            _complete(
                db,
                job_id=job_id,
                lease_owner=lease_owner,
                state="discarded",
                producer=None,
                result={"reason": PLANNING_STALE_REASON},
                last_error=PLANNING_STALE_REASON,
                next_attempt_at=None,
                now=now,
            )
            return {"status": "discarded", "reason": PLANNING_STALE_REASON}

    return {"status": "succeeded"}
