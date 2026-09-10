"""Model-authored plan decomposition, off the request thread.

This job is the reason the takeover control path can promise a bounded turn: the model call
that turns an objective into ordered steps happens here, under a lease, with a staleness
re-check on both sides of it — not inside ``takeover_step``.

Three properties worth stating, because each one is a class of bug this job is designed not
to have:

* **Stale results cannot dispatch work.** The objective can change while the model is
  running. The result is checked against a fresh projection before the write and again,
  under the row lock, by the ``revalidate`` closure. Either check failing means nothing is
  written at all.
* **A lost lease writes nothing.** The completion UPDATE is fenced on
  ``lease_owner`` and ``state='leased'``; rowcount 0 rolls the whole transaction back.
* **A failed model call is a failure, not a fallback.** The worker never substitutes the
  deterministic plan into a ``producer='model'`` slot — the request thread owns the
  deterministic path, which is what keeps "a mutating plan can only come from an approved
  model plan" true by construction.

Write-back order follows the global lock order: ``task_states`` (lock 1) before
``autonomy_goals`` (lock 5). That is why the root goal id is minted with ``uuid4()`` *before*
the task-state write and handed to ``write_plan_rows`` — the ``PLAN_APPROVED`` payload has to
name a row that does not exist yet, and it should still name the right one.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_api.db import get_session_factory
from tce_api.plan_rows import resolved_scope_from_json, write_plan_rows
from tce_api.planning_store import claim_planning_job, complete_planning_job
from tce_api.task_state_store import apply_task_state_events, load_task_state
from tce_model_gateway.factory import get_gateway
from tce_shared.plan_decomposition import PLAN_DECOMPOSITION_PROMPT

# Private on purpose, and imported on purpose: the fold derives the same plan id when a
# payload omits it, so copying the namespace UUID here would be a second source of truth for
# a value that must never differ.
from tce_shared.task_state import (
    PLANNING_JOB_KIND_DECOMPOSE,
    PLANNING_LOST_LEASE_REASON,
    PLANNING_PRODUCER_MODEL,
    PLANNING_STALE_REASON,
    TASK_STATE_POLICY_REVISION,
    TaskStateEvent,
    TaskStateEventKind,
    TaskStatePreconditionFailed,
    TaskStateProjection,
    TaskStateRevisionConflict,
    approved_plan_id,
    charter_from_json,
    charter_to_json,
    plan_input_revision,
    plan_steps_from_model,
    plan_steps_to_json,
    planning_result_is_stale,
    task_scope_digest,
)

from ..config import get_settings

LOGGER = logging.getLogger(__name__)

_COUNT_KEYS = ("processed", "succeeded", "failed", "discarded", "cancelled", "skipped")


def _zero_counts() -> dict[str, int]:
    return dict.fromkeys(_COUNT_KEYS, 0)


def _lease_owner() -> str:
    """One identity per ``run()`` call — the fence the completion UPDATE checks."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _worker_plan_gateway_settings(settings_obj: Any) -> Any:
    """The clamped settings clone for the worker's plan/dream model call.

    Moved verbatim out of the API layer, where it had no business being: the configured
    advisor timeout is 90 s, which would block a request thread for a minute and a half.
    Carries every field ``create_gateway`` and ``_wrap_with_cache`` read as bare attributes,
    so it can never raise AttributeError on a partially-populated namespace.
    """
    provider = str(
        getattr(settings_obj, "takeover_plan_llm_provider", "")
        or getattr(settings_obj, "model_provider", "ollama")
    ).strip().lower()
    return SimpleNamespace(
        model_provider=provider,
        ollama_url=getattr(settings_obj, "ollama_url", "http://ollama:11434"),
        embed_model=getattr(settings_obj, "embed_model", "mxbai-embed-large"),
        extract_model=getattr(settings_obj, "extract_model", "qwen2.5:3b"),
        openai_api_key=getattr(settings_obj, "openai_api_key", ""),
        openai_embed_model=getattr(settings_obj, "openai_embed_model", "text-embedding-3-small"),
        openai_extract_model=getattr(settings_obj, "openai_extract_model", "gpt-4o-mini"),
        openai_base_url=getattr(settings_obj, "openai_base_url", None),
        anthropic_api_key=getattr(settings_obj, "anthropic_api_key", ""),
        anthropic_extract_model=getattr(
            settings_obj, "anthropic_extract_model", "claude-haiku-4-5-20251001"
        ),
        advisor_timeout_seconds=float(getattr(settings_obj, "takeover_plan_llm_timeout_seconds", 25)),
        advisor_attempt_timeout_ms=0,
        advisor_read_timeout_ms=0,
        redis_url=getattr(settings_obj, "redis_url", ""),
    )


def _reread_cancel_requested(db: Session, job_id: str) -> bool:
    """Re-read the cancel flag under the task_states lock, for the revalidate closure."""
    value = db.execute(
        text("SELECT cancel_requested FROM planning_jobs WHERE id = CAST(:job_id AS UUID)"),
        {"job_id": job_id},
    ).scalar_one_or_none()
    return bool(value)


def _current_input_revision(projection: TaskStateProjection, scope: Any) -> str:
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


def _backoff(settings: Any, attempts: int, now: datetime) -> datetime | None:
    delay = min(
        int(getattr(settings, "planning_job_backoff_cap_seconds", 900)),
        5 * (2 ** max(0, attempts - 1)),
    )
    return now + timedelta(seconds=delay)


def _json_field(value: Any) -> Any:
    """A JSONB column arrives as a dict from psycopg and as a str from some drivers."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value


def run(job_id: str | None = None, batch_size: int | None = None) -> dict[str, Any]:
    """Plan one job (``job_id``) or sweep a batch of claimable jobs.

    Never raises; per-job failures are recorded on the row with exponential backoff.
    """
    settings = get_settings()
    counts = _zero_counts()
    if not bool(getattr(settings, "planning_enabled", True)):
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
        LOGGER.warning("planning claim failed", exc_info=True)
        return {"status": "ok", "reason": "claim_failed", **counts}

    if not rows:
        return {"status": "skipped", **counts}

    last_status = "ok"
    last_reason = ""
    for row in rows:
        if str(row.get("job_kind") or "") != PLANNING_JOB_KIND_DECOMPOSE:
            counts["skipped"] += 1
            continue
        try:
            outcome = _process_job(dict(row), settings=settings, lease_owner=lease_owner)
        except Exception:
            LOGGER.warning("planning job crashed for %s", row.get("id"), exc_info=True)
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
        current = _current_input_revision(projection, scope)

        stale, reason = planning_result_is_stale(
            job_input_revision=str(row.get("input_revision") or ""),
            job_contract_revision=int(row.get("contract_revision") or 0),
            current_input_revision=current,
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

        # The charter is read from the JOB ROW, never rebuilt from settings: otherwise the
        # charter that was enqueued and the charter that executes can silently differ.
        charter = charter_from_json(_json_field(row.get("charter_json")) or {})

        try:
            if not bool(getattr(settings, "takeover_plan_llm_enabled", False)):
                raise RuntimeError("takeover_plan_llm_disabled")
            gateway = get_gateway(_worker_plan_gateway_settings(settings))
            # `.replace`, not `.format`: the prompt contains a literal JSON example, so
            # str.format raises KeyError on it.
            payload = gateway.extract_structured(
                PLAN_DECOMPOSITION_PROMPT.replace("{objective}", str(row.get("objective_text") or "")[:2000]),
                "plan_decomposition_v1",
            )
        except Exception as exc:
            return _fail(
                db,
                row=row,
                job_id=job_id,
                lease_owner=lease_owner,
                settings=settings,
                now=now,
                last_error=str(exc)[:400] or "model_call_failed",
            )

        steps = plan_steps_from_model(payload, charter=charter)
        if not steps:
            return _fail(
                db,
                row=row,
                job_id=job_id,
                lease_owner=lease_owner,
                settings=settings,
                now=now,
                last_error="unparseable_plan",
            )

        root_goal_id = str(uuid.uuid4())

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
            # Lock 1 FIRST. The worker's snapshot predates its model call and may be
            # arbitrarily old, so this is the one call site in P2 that supplies an integer
            # expected_revision: a stale write must be refused, not silently applied.
            apply_task_state_events(
                db,
                workspace_id=str(row["workspace_id"]),
                owner_id=str(row["owner_id"]),
                session_id=str(row["session_id"]),
                task_id=str(row["task_id"]),
                new_events=[
                    TaskStateEvent(
                        seq=0,
                        kind=TaskStateEventKind.PLAN_APPROVED,
                        contract_revision=projection.contract_revision,
                        payload={
                            "producer": PLANNING_PRODUCER_MODEL,
                            "plan_id": approved_plan_id(
                                str(row["task_id"]), projection.contract_revision
                            ),
                            "root_goal_id": root_goal_id,
                            "steps": plan_steps_to_json(steps),
                            "charter": charter_to_json(charter),
                            "descriptive_sources": {},
                        },
                        occurred_at=now,
                        actor="worker:planning",
                    )
                ],
                now=now,
                expected_revision=projection.revision,
                retry_once=True,
                revalidate=_revalidate,
            )
            # Lock 5, last.
            write_plan_rows(
                db,
                scope=scope,
                session_id=str(row["session_id"]),
                task_id=str(row["task_id"]),
                steps=steps,
                producer=PLANNING_PRODUCER_MODEL,
                contract_revision=projection.contract_revision,
                now=now,
                root_goal_id=root_goal_id,
                objective_text=str(row.get("objective_text") or ""),
            )
            fenced = complete_planning_job(
                db,
                job_id=job_id,
                lease_owner=lease_owner,
                state="succeeded",
                producer=PLANNING_PRODUCER_MODEL,
                result={"steps": len(steps), "root_goal_id": root_goal_id},
                last_error=None,
                next_attempt_at=None,
                now=now,
            )
            if not fenced:
                # The lease was lost or the task was cancelled while the model ran. Steps 1
                # and 2 share this transaction, so a rollback persists nothing.
                db.rollback()
                return {"status": PLANNING_LOST_LEASE_REASON}
            db.commit()
        except (TaskStatePreconditionFailed, TaskStateRevisionConflict):
            # Belt and braces: because lock 1 is taken first, no goal row exists yet.
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


def _fail(
    db: Session,
    *,
    row: dict[str, Any],
    job_id: str,
    lease_owner: str,
    settings: Any,
    now: datetime,
    last_error: str,
) -> dict[str, Any]:
    """Record a per-job failure with backoff. Terminal once attempts reach max_attempts."""
    attempts = int(row.get("attempts") or 0)
    max_attempts = int(row.get("max_attempts") or 3)
    next_attempt_at = None if attempts >= max_attempts else _backoff(settings, attempts, now)
    _complete(
        db,
        job_id=job_id,
        lease_owner=lease_owner,
        state="failed",
        producer=None,
        result={"reason": last_error},
        last_error=last_error,
        next_attempt_at=next_attempt_at,
        now=now,
    )
    return {"status": "failed", "reason": last_error}


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
