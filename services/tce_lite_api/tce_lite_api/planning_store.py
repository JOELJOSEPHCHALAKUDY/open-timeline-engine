"""Planning jobs for Lite (P2 §5.3, §0.7 S12.4).

The Lite twin of `tce_api.planning_store`. Same seven signatures, same row shape, same
idempotency key — with `conn: sqlite3.Connection` and `?` params in place of a SQLAlchemy
`Session`.

**D2 — the one behavioural difference.** Lite has no worker, no Redis and no model gateway, so a
deferred row would sit in `pending` until a restart. `enqueue_planning_job` therefore runs the
deterministic planner **inline** and writes the row already `state='succeeded'`,
`producer='deterministic'`, `queue_state='inline'`, with `result_json` carrying the steps — all in
the caller's transaction. `scope_json` is still written, so the two backends' `planning_jobs` rows
stay diffable. The wire shape is identical to Full's and `planning_pending` is normally false.

`claim_planning_job` / `complete_planning_job` exist with the same signatures for parity and are
exercised only by `sweep_stale_planning_jobs` and its tests.

Nothing here commits: the caller owns the transaction.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from tce_shared.scope import ResolvedScope
from tce_shared.task_state import (
    PLANNING_JOB_REVIVABLE_STATES,
    PLANNING_PRODUCER_DETERMINISTIC,
    PlanCharter,
    canonical_json,
    charter_to_json,
    deterministic_plan,
    plan_steps_to_json,
    planning_idempotency_key,
)

from .plan_rows import resolved_scope_to_json

_JOB_COLUMNS = (
    "id",
    "workspace_id",
    "owner_id",
    "session_id",
    "task_id",
    "task_state_id",
    "job_kind",
    "idempotency_key",
    "input_revision",
    "contract_revision",
    "objective_hash",
    "objective_text",
    "charter_json",
    "scope_json",
    "state",
    "lease_owner",
    "lease_until",
    "attempts",
    "max_attempts",
    "next_attempt_at",
    "last_error",
    "producer",
    "result_json",
    "queue_state",
    "rq_job_id",
    "cancel_requested",
    "created_at",
    "updated_at",
)

_CLAIM_COLUMNS = (
    "id",
    "workspace_id",
    "owner_id",
    "session_id",
    "task_id",
    "task_state_id",
    "job_kind",
    "input_revision",
    "contract_revision",
    "objective_hash",
    "objective_text",
    "charter_json",
    "scope_json",
    "attempts",
    "max_attempts",
    "cancel_requested",
)


def enqueue_planning_job(
    conn: sqlite3.Connection,
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
) -> tuple[str, bool]:
    """Returns ``(job_id, created)``.

    D2: the deterministic planner runs INLINE and the row is written already succeeded, in the
    caller's transaction. The caller applies ``PLAN_APPROVED`` in the same transaction.

    1. Insert on the idempotency key. A new row -> ``(id, True)``.
    2. On conflict, read the existing row; revive it when its state is in
       ``PLANNING_JOB_REVIVABLE_STATES`` and return ``(id, True)``.
    3. Otherwise ``(id, False)``.

    S4 note: the revive branch can no longer resurrect a job the owner just cancelled on the same
    objective — ``plan_input_revision`` folds in the cancel epoch, so a post-cancel turn computes a
    different ``idempotency_key`` and takes branch 1 with a fresh row.
    """
    idempotency = planning_idempotency_key(
        job_kind=job_kind,
        workspace_id=workspace_id,
        owner_id=owner_id,
        task_id=task_id,
        input_revision=input_revision,
    )
    existing = conn.execute(
        "SELECT id, state FROM planning_jobs WHERE workspace_id = ? AND idempotency_key = ?",
        (workspace_id, idempotency),
    ).fetchone()
    stamp = now.isoformat()
    if existing is not None:
        job_id = str(existing["id"])
        if str(existing["state"]) in PLANNING_JOB_REVIVABLE_STATES:
            cursor = conn.execute(
                """
                UPDATE planning_jobs
                   SET state = 'pending', attempts = 0, cancel_requested = 0,
                       next_attempt_at = NULL, lease_owner = NULL, lease_until = NULL,
                       last_error = NULL, updated_at = ?
                 WHERE id = ? AND state IN ('cancelled', 'failed')
                """,
                (stamp, job_id),
            )
            if cursor.rowcount == 1:
                return job_id, True
        return job_id, False

    steps = deterministic_plan(objective_text, charter=charter)
    job_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO planning_jobs ({cols}) VALUES ({marks})".format(
            cols=", ".join(_JOB_COLUMNS),
            marks=", ".join("?" for _ in _JOB_COLUMNS),
        ),
        (
            job_id,
            workspace_id,
            owner_id,
            session_id,
            task_id,
            task_state_id,
            job_kind,
            idempotency,
            input_revision,
            int(contract_revision),
            objective_hash,
            objective_text,
            canonical_json(charter_to_json(charter)),
            canonical_json(resolved_scope_to_json(scope)),
            "succeeded",
            None,
            None,
            0,
            int(max_attempts),
            None,
            None,
            PLANNING_PRODUCER_DETERMINISTIC,
            canonical_json({"steps": plan_steps_to_json(steps)}),
            "inline",
            None,
            0,
            stamp,
            stamp,
        ),
    )
    return job_id, True


def get_planning_job(
    conn: sqlite3.Connection, *, workspace_id: str, job_id: str
) -> Mapping[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM planning_jobs WHERE workspace_id = ? AND id = ?",
        (workspace_id, job_id),
    ).fetchone()
    return dict(row) if row is not None else None


def latest_planning_job_for_task(
    conn: sqlite3.Connection, *, workspace_id: str, task_id: str, job_kind: str
) -> Mapping[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM planning_jobs
         WHERE workspace_id = ? AND task_id = ? AND job_kind = ?
         ORDER BY created_at DESC
         LIMIT 1
        """,
        (workspace_id, task_id, job_kind),
    ).fetchone()
    return dict(row) if row is not None else None


def mark_queue_state(
    conn: sqlite3.Connection, *, job_id: str, queue_state: str, rq_job_id: str | None
) -> None:
    conn.execute(
        "UPDATE planning_jobs SET queue_state = ?, rq_job_id = ? WHERE id = ?",
        (queue_state, rq_job_id, job_id),
    )


def claim_planning_job(
    conn: sqlite3.Connection,
    *,
    job_id: str | None,
    lease_owner: str,
    lease_seconds: int,
    now: datetime,
    batch_size: int,
) -> list[Mapping[str, Any]]:
    """The §8.3 claim, without ``FOR UPDATE SKIP LOCKED`` (SQLite has no row locks; the
    ``BEGIN IMMEDIATE`` write lock serialises claimants instead).

    The ``state='leased' AND lease_until < now`` disjunct is mandatory: without it a crashed
    claimant leaves the row leased forever and ``planning_pending`` never clears.
    """
    stamp = now.isoformat()
    lease_until = (now + timedelta(seconds=int(lease_seconds))).isoformat()
    candidates = conn.execute(
        """
        SELECT id FROM planning_jobs
         WHERE cancel_requested = 0
           AND attempts < max_attempts
           AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
           AND (
                 state IN ('pending', 'failed')
              OR (state = 'leased' AND lease_until IS NOT NULL AND lease_until < ?)
               )
           AND (? IS NULL OR id = ?)
         ORDER BY created_at ASC
         LIMIT ?
        """,
        (stamp, stamp, job_id, job_id, int(batch_size)),
    ).fetchall()
    claimed: list[Mapping[str, Any]] = []
    for candidate in candidates:
        cursor = conn.execute(
            """
            UPDATE planning_jobs
               SET state = 'leased', lease_owner = ?, lease_until = ?,
                   attempts = attempts + 1, updated_at = ?
             WHERE id = ?
            """,
            (lease_owner, lease_until, stamp, str(candidate["id"])),
        )
        if cursor.rowcount != 1:
            continue
        row = conn.execute(
            f"SELECT {', '.join(_CLAIM_COLUMNS)} FROM planning_jobs WHERE id = ?",
            (str(candidate["id"]),),
        ).fetchone()
        if row is not None:
            claimed.append(dict(row))
    return claimed


def complete_planning_job(
    conn: sqlite3.Connection,
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
    """The §8.5 step-3 fenced UPDATE. ``False`` => the lease was lost or the task was cancelled."""
    cursor = conn.execute(
        """
        UPDATE planning_jobs
           SET state = ?, producer = ?, result_json = ?, last_error = ?,
               next_attempt_at = ?, lease_owner = NULL, lease_until = NULL, updated_at = ?
         WHERE id = ? AND lease_owner = ? AND state = 'leased' AND cancel_requested = 0
        """,
        (
            state,
            producer,
            canonical_json(dict(result)),
            last_error,
            next_attempt_at.isoformat() if next_attempt_at is not None else None,
            now.isoformat(),
            job_id,
            lease_owner,
        ),
    )
    return bool(cursor.rowcount == 1)


def sweep_stale_planning_jobs(
    conn: sqlite3.Connection, *, settings: Any, now: datetime
) -> dict[str, int]:
    """Release expired leases, then discard jobs whose task no longer exists.

    Lite's only background trigger for planning-job hygiene: it is called from `_lifespan` and
    from the operator lifecycle endpoint. Full never calls it in-process.
    """
    del settings  # the sweep needs no configuration; the parameter keeps the twins diffable
    stamp = now.isoformat()
    released = conn.execute(
        """
        UPDATE planning_jobs
           SET state = 'pending', lease_owner = NULL, lease_until = NULL, updated_at = ?
         WHERE state = 'leased' AND lease_until IS NOT NULL AND lease_until < ?
           AND attempts < max_attempts
        """,
        (stamp, stamp),
    ).rowcount
    discarded = conn.execute(
        """
        UPDATE planning_jobs
           SET state = 'discarded', last_error = 'task_state_missing', updated_at = ?
         WHERE state IN ('pending', 'leased')
           AND NOT EXISTS (
                 SELECT 1 FROM task_states t
                  WHERE t.workspace_id = planning_jobs.workspace_id
                    AND t.task_id = planning_jobs.task_id
               )
        """,
        (stamp,),
    ).rowcount
    return {"leases_released": int(max(0, released)), "orphans_discarded": int(max(0, discarded))}


def pending_directive_ids(
    conn: sqlite3.Connection, *, workspace_id: str, owner_id: str, session_id: str
) -> list[str]:
    """S1: keyed on ``session_id``, never ``task_id`` — ``task_id`` has no producer on this table."""
    rows = conn.execute(
        """
        SELECT directive_id FROM directive_executions
         WHERE workspace_id = ? AND user_id = ? AND session_id = ?
           AND state IN ('pending', 'in_progress')
        """,
        (workspace_id, owner_id, session_id),
    ).fetchall()
    return [str(row["directive_id"]) for row in rows if row["directive_id"] is not None]


def pending_planning_job_ids(
    conn: sqlite3.Connection, *, workspace_id: str, task_id: str
) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM planning_jobs WHERE workspace_id = ? AND task_id = ? AND state IN ('pending', 'leased')",
        (workspace_id, task_id),
    ).fetchall()
    return [str(row["id"]) for row in rows]


__all__ = [
    "claim_planning_job",
    "complete_planning_job",
    "enqueue_planning_job",
    "get_planning_job",
    "latest_planning_job_for_task",
    "mark_queue_state",
    "pending_directive_ids",
    "pending_planning_job_ids",
    "sweep_stale_planning_jobs",
]
