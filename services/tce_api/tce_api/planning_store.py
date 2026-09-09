"""``planning_jobs`` row lifecycle for the Full backend: enqueue, claim, complete, sweep.

Every function takes an explicit ``db: Session`` and is fully annotated — under the repo's
``strict = false`` an untyped parameter makes the whole body unchecked, which is exactly where a
missed call site would hide.

Only :func:`claim_planning_job` commits, and it does so deliberately: the lease must be visible to
other workers before the model call starts. Everything else leaves the transaction to its caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_shared.scope import ResolvedScope
from tce_shared.task_state import (
    PLANNING_JOB_REVIVABLE_STATES,
    TASK_STATE_SCHEMA_VERSION,
    PlanCharter,
    canonical_json,
    charter_to_json,
    planning_idempotency_key,
)

from .plan_rows import resolved_scope_to_json

# Pinned: the worker indexes every one of these by name.
_CLAIM_COLUMNS = """
    j.id, j.workspace_id, j.owner_id, j.session_id, j.task_id, j.task_state_id, j.job_kind,
    j.input_revision, j.contract_revision, j.objective_hash, j.objective_text, j.charter_json,
    j.scope_json, j.attempts, j.max_attempts, j.cancel_requested
"""

_JOB_COLUMNS = """
    id, workspace_id, owner_id, session_id, task_id, task_state_id, job_kind,
    idempotency_key, input_revision, contract_revision, objective_hash, objective_text,
    charter_json, scope_json, state, lease_owner, lease_until, attempts, max_attempts,
    next_attempt_at, last_error, producer, result_json, queue_state, rq_job_id,
    cancel_requested, created_at, updated_at
"""


def enqueue_planning_job(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    task_id: str,
    task_state_id: str,
    job_kind: str,
    input_revision: str,
    contract_revision: int,
    objective_hash: str | None,
    objective_text: str,
    charter: PlanCharter,
    scope: ResolvedScope,
    now: datetime,
    max_attempts: int,
    state: str = "pending",
    queue_state: str = "queued",
    producer: str | None = None,
) -> tuple[str, bool]:
    """Returns ``(job_id, created)``.

    The idempotency key folds in ``input_revision``, which folds in the **cancel epoch**, so a
    turn that re-issues an objective after a cancel computes a different key and takes the
    INSERT branch with a fresh row rather than reviving the row the owner just killed. The
    revive branch survives only for its original purpose: a job that failed for a transient
    reason on an unchanged objective.

    This function and its Lite twin are the **only** producers of ``charter_json`` and
    ``scope_json``; the worker reads both back rather than rebuilding them from settings.
    """
    idempotency_key = planning_idempotency_key(
        job_kind=job_kind,
        workspace_id=workspace_id,
        owner_id=owner_id,
        task_id=task_id,
        input_revision=input_revision,
    )
    params: dict[str, Any] = {
        "workspace_id": workspace_id,
        "owner_id": owner_id,
        "session_id": session_id,
        "task_id": task_id,
        "task_state_id": task_state_id,
        "job_kind": job_kind,
        "idempotency_key": idempotency_key,
        "input_revision": input_revision,
        "contract_revision": int(contract_revision),
        "objective_hash": objective_hash,
        "objective_text": objective_text,
        "charter_json": canonical_json(charter_to_json(charter)),
        "scope_json": canonical_json(resolved_scope_to_json(scope)),
        "state": state,
        "queue_state": queue_state,
        "producer": producer,
        "max_attempts": int(max_attempts),
        "now": now,
        # Bound explicitly rather than left to the column default: models.py's `default=` is a
        # Python-side default, so a metadata-built schema has no server default at all.
        "schema_version": TASK_STATE_SCHEMA_VERSION,
    }
    inserted = db.execute(
        text(
            """
            INSERT INTO planning_jobs(
                id, workspace_id, owner_id, session_id, task_id, task_state_id, job_kind,
                idempotency_key, input_revision, contract_revision, objective_hash,
                objective_text, charter_json, scope_json, state, attempts, max_attempts,
                producer, result_json, queue_state, cancel_requested, created_at, updated_at,
                schema_version
            )
            VALUES(
                gen_random_uuid(), :workspace_id, :owner_id, :session_id, :task_id,
                CAST(:task_state_id AS UUID), :job_kind, :idempotency_key, :input_revision,
                :contract_revision, :objective_hash, :objective_text,
                CAST(:charter_json AS JSONB), CAST(:scope_json AS JSONB), :state, 0,
                :max_attempts, :producer, CAST('{}' AS JSONB), :queue_state, false,
                :now, :now, :schema_version
            )
            ON CONFLICT (workspace_id, idempotency_key) DO NOTHING
            RETURNING id
            """
        ),
        params,
    ).scalar()
    if inserted is not None:
        return str(inserted), True

    existing = db.execute(
        text(
            """
            SELECT id, state FROM planning_jobs
             WHERE workspace_id = :workspace_id AND idempotency_key = :idempotency_key
            """
        ),
        {"workspace_id": workspace_id, "idempotency_key": idempotency_key},
    ).mappings().first()
    if existing is None:  # pragma: no cover - only reachable on a concurrent delete
        return "", False
    job_id = str(existing["id"])
    if str(existing["state"]) in PLANNING_JOB_REVIVABLE_STATES:
        db.execute(
            text(
                """
                UPDATE planning_jobs
                   SET state = 'pending', attempts = 0, cancel_requested = false,
                       next_attempt_at = NULL, lease_owner = NULL, lease_until = NULL,
                       last_error = NULL, updated_at = :now
                 WHERE id = :job_id AND state IN ('cancelled', 'failed')
                """
            ),
            {"job_id": existing["id"], "now": now},
        )
        return job_id, True
    return job_id, False


def get_planning_job(
    db: Session, *, workspace_id: str, job_id: str
) -> Mapping[str, Any] | None:
    row = db.execute(
        text(
            f"SELECT {_JOB_COLUMNS} FROM planning_jobs "
            "WHERE workspace_id = :workspace_id AND id = CAST(:job_id AS UUID)"
        ),
        {"workspace_id": workspace_id, "job_id": job_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def latest_planning_job_for_task(
    db: Session, *, workspace_id: str, task_id: str, job_kind: str
) -> Mapping[str, Any] | None:
    row = db.execute(
        text(
            f"SELECT {_JOB_COLUMNS} FROM planning_jobs "
            "WHERE workspace_id = :workspace_id AND task_id = :task_id AND job_kind = :job_kind "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"workspace_id": workspace_id, "task_id": task_id, "job_kind": job_kind},
    ).mappings().first()
    return dict(row) if row is not None else None


def mark_queue_state(
    db: Session, *, job_id: str, queue_state: str, rq_job_id: str | None
) -> None:
    db.execute(
        text(
            """
            UPDATE planning_jobs
               SET queue_state = :queue_state, rq_job_id = :rq_job_id
             WHERE id = CAST(:job_id AS UUID)
            """
        ),
        {"job_id": job_id, "queue_state": queue_state, "rq_job_id": rq_job_id},
    )


def claim_planning_job(
    db: Session,
    *,
    job_id: str | None,
    lease_owner: str,
    lease_seconds: int,
    now: datetime,
    batch_size: int,
) -> list[Mapping[str, Any]]:
    """Lease pending jobs with ``FOR UPDATE SKIP LOCKED`` and commit immediately.

    Committing here is deliberate and matches ``decision_extraction._claim``: the lease must be
    visible to every other worker *before* the model call starts, or two workers plan the same
    objective.
    """
    lease_until_sql = "(:now + make_interval(secs => :lease_seconds))"
    predicate = (
        "id = CAST(:job_id AS UUID)"
        if job_id
        else "state = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= :now)"
    )
    rows = db.execute(
        text(
            f"""
            WITH claimed AS (
                SELECT id FROM planning_jobs
                 WHERE {predicate}
                   AND state IN ('pending')
                   AND cancel_requested = false
                 ORDER BY created_at
                 LIMIT :batch_size
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE planning_jobs AS j
               SET state = 'leased', lease_owner = :lease_owner,
                   lease_until = {lease_until_sql},
                   attempts = j.attempts + 1, updated_at = :now
              FROM claimed
             WHERE j.id = claimed.id
            RETURNING {_CLAIM_COLUMNS}
            """
        ),
        {
            "job_id": job_id,
            "lease_owner": lease_owner,
            "lease_seconds": int(lease_seconds),
            "now": now,
            "batch_size": int(max(1, batch_size)),
        },
    ).mappings().all()
    db.commit()
    return [dict(row) for row in rows]


def complete_planning_job(
    db: Session,
    *,
    job_id: str,
    lease_owner: str,
    state: str,
    producer: str | None,
    result: Mapping[str, Any],
    last_error: str | None,
    next_attempt_at: datetime | None,
    now: datetime,
) -> bool:
    """The fenced completion UPDATE. ``False`` means the lease was lost or the task was
    cancelled while the model ran — the caller must then write nothing."""
    result_row = db.execute(
        text(
            """
            UPDATE planning_jobs
               SET state = :state, producer = COALESCE(:producer, producer),
                   result_json = CAST(:result_json AS JSONB), last_error = :last_error,
                   next_attempt_at = :next_attempt_at, lease_owner = NULL, lease_until = NULL,
                   updated_at = :now
             WHERE id = CAST(:job_id AS UUID)
               AND lease_owner = :lease_owner
               AND state = 'leased'
               AND cancel_requested = false
            """
        ),
        {
            "job_id": job_id,
            "lease_owner": lease_owner,
            "state": state,
            "producer": producer,
            "result_json": canonical_json(dict(result)),
            "last_error": last_error,
            "next_attempt_at": next_attempt_at,
            "now": now,
        },
    )
    return int(getattr(result_row, "rowcount", 0) or 0) == 1


def sweep_stale_planning_jobs(
    db: Session, *, settings: Any, now: datetime
) -> dict[str, int]:
    """Release expired leases and discard jobs that exhausted their attempts."""
    released = db.execute(
        text(
            """
            UPDATE planning_jobs
               SET state = 'pending', lease_owner = NULL, lease_until = NULL, updated_at = :now
             WHERE state = 'leased' AND lease_until IS NOT NULL AND lease_until < :now
               AND attempts < max_attempts
            """
        ),
        {"now": now},
    )
    discarded = db.execute(
        text(
            """
            UPDATE planning_jobs
               SET state = 'discarded', lease_owner = NULL, lease_until = NULL,
                   last_error = COALESCE(last_error, 'max_attempts_exhausted'), updated_at = :now
             WHERE state IN ('pending', 'leased') AND attempts >= max_attempts
            """
        ),
        {"now": now},
    )
    _ = settings
    return {
        "leases_released": int(getattr(released, "rowcount", 0) or 0),
        "orphans_discarded": int(getattr(discarded, "rowcount", 0) or 0),
    }


def pending_directive_ids(
    db: Session, *, workspace_id: str, owner_id: str, session_id: str
) -> list[str]:
    """Pending/in-progress directives for the session.

    Keyed on ``session_id``, never on a task column: under D4 they hold the same value and
    ``session_id`` is the column the mint path actually populates.
    """
    rows = db.execute(
        text(
            """
            SELECT directive_id FROM directive_executions
             WHERE workspace_id = :workspace_id AND user_id = :owner_id
               AND session_id = :session_id
               AND state IN ('pending', 'in_progress')
            """
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id, "session_id": session_id},
    ).all()
    return [str(row[0]) for row in rows]


def pending_planning_job_ids(db: Session, *, workspace_id: str, task_id: str) -> list[str]:
    rows = db.execute(
        text(
            """
            SELECT id FROM planning_jobs
             WHERE workspace_id = :workspace_id AND task_id = :task_id
               AND state IN ('pending', 'leased')
            """
        ),
        {"workspace_id": workspace_id, "task_id": task_id},
    ).all()
    return [str(row[0]) for row in rows]
