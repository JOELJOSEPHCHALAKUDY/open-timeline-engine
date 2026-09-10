"""Durable task-state projection for the Full backend: load, rebuild, CAS-write, cancel.

Raw ``text()`` SQL against ``task_states`` / ``task_state_events``. **No ``db.commit()`` and no
``db.rollback()`` inside** — the caller owns the transaction, and the only rollback this module
ever performs is ``ROLLBACK TO SAVEPOINT`` on a savepoint it took itself.

The CAS rule, stated once because it is the whole point of the module: the revision the compare
runs against is **always** the one read by *this* call's ``SELECT ... FOR UPDATE``, inside this
transaction, under the lock. There is no turn-level pinned revision. ``expected_revision=None``
means "no caller precondition"; an integer is a precondition for a caller whose snapshot was
taken outside this transaction (in P2 that is exactly one caller, the worker across its model
call).

Lock order (global): ``task_states`` (1) → ``planning_jobs`` (2) → ``directive_executions`` (3)
→ ``execution_permits`` (4) → ``autonomy_goals`` (5).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_shared.events import TaskCancelResponse
from tce_shared.execution_transitions import SYSTEM_ACTOR, validate_transition
from tce_shared.task_state import (
    CANCEL_REASON_TASK_CANCELLED,
    MAX_TASK_STATE_EVENTS_PER_FOLD,
    PINNED_KIND_VALUES,
    PINNED_KINDS,
    TASK_STATE_SCHEMA_VERSION,
    UNKNOWN_CONTRACT_REVISION,
    ApprovedPlan,
    FoldResult,
    InvalidationPlan,
    PlanState,
    TaskStateEvent,
    TaskStateEventKind,
    TaskStatePreconditionFailed,
    TaskStateProjection,
    TaskStateRevisionConflict,
    TaskStateWrite,
    approved_plan_from_json,
    approved_plan_to_json,
    canonical_json,
    constraints_from_json,
    constraints_to_json,
    decisions_from_json,
    decisions_to_json,
    effects_from_json,
    effects_to_json,
    event_payload_from_json,
    event_payload_to_json,
    fold_task_state,
    prepare_write,
    verification_from_json,
    verification_to_json,
)

logger = logging.getLogger(__name__)

ApplySideEffects = Callable[[Session, InvalidationPlan], None]
Revalidate = Callable[[TaskStateProjection], tuple[bool, str]]

_TAIL_EVENTS = MAX_TASK_STATE_EVENTS_PER_FOLD - len(PINNED_KINDS)

_PROJECTION_COLUMNS = """
    id, workspace_id, owner_id, subject_user_id, session_id, task_id, project_id,
    revision, contract_revision, highest_seq, objective_text, objective_hash,
    objective_set_at, objective_set_seq, status, next_permitted_action,
    plan_state, plan_producer, plan_root_goal_id, planning_job_id,
    constraints_json, open_decisions_json, plan_json, unresolved_effects_json,
    latest_verification_json, citations_json, source_revision,
    cancelled_at, cancelled_seq, last_cancel_seq, cancel_reason, schema_version
"""


def load_task_state(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    task_id: str,
    for_update: bool = False,
) -> tuple[TaskStateProjection, int, str] | None:
    """``(projection, highest_seq, source_revision)``, or ``None`` when there is no row.

    ``for_update=True`` appends ``FOR UPDATE`` — lock 1 in the global order. The third element
    is the stored ``task_states.source_revision`` column, returned here so the read path does
    not need a second query for ``TaskStateSummary.source_revision``.
    """
    sql = f"SELECT {_PROJECTION_COLUMNS} FROM task_states WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND task_id = :task_id"
    if for_update:
        sql += " FOR UPDATE"
    row = db.execute(
        text(sql),
        {"workspace_id": workspace_id, "owner_id": owner_id, "task_id": task_id},
    ).mappings().first()
    if row is None:
        return None
    return (
        _projection_from_row(row),
        int(row["highest_seq"] or 0),
        str(row["source_revision"] or ""),
    )


def load_task_state_events(
    db: Session,
    *,
    task_state_id: str,
    max_events: int = MAX_TASK_STATE_EVENTS_PER_FOLD,
) -> list[TaskStateEvent]:
    """The same pinning the fold applies, expressed in SQL.

    ``UNION ALL``, not ``UNION``: ``UNION`` needs an equality operator per column and the fold
    already dedups on ``(seq, kind, payload_digest)``. The tail size is the constant
    ``max_events - len(PINNED_KINDS)`` in both the SQL and the fold, so the reconstruction and
    the stored row provably agree — SQL cannot know how many pinned events exist without a
    second round trip, and a constant is the only formula both sides can evaluate identically.
    """
    tail = max(1, int(max_events) - len(PINNED_KINDS))
    rows = db.execute(
        text(
            """
            (SELECT DISTINCT ON (kind)
                    seq, kind, contract_revision, payload_json, occurred_at, actor,
                    source_event_id, directive_id, goal_id
               FROM task_state_events
              WHERE task_state_id = :task_state_id AND kind = ANY(:pinned)
              ORDER BY kind, seq DESC)
            UNION ALL
            (SELECT seq, kind, contract_revision, payload_json, occurred_at, actor,
                    source_event_id, directive_id, goal_id
               FROM task_state_events
              WHERE task_state_id = :task_state_id
              ORDER BY seq DESC
              LIMIT :tail)
            """
        ),
        {
            "task_state_id": uuid.UUID(str(task_state_id)),
            "pinned": list(PINNED_KIND_VALUES),
            "tail": tail,
        },
    ).mappings().all()
    return [_event_from_row(row) for row in rows]


def rebuild_task_state(
    db: Session, *, workspace_id: str, owner_id: str, task_id: str
) -> FoldResult | None:
    """SELECT the row, SELECT its events, fold. The deterministic reconstruction path."""
    row = db.execute(
        text(
            f"SELECT {_PROJECTION_COLUMNS} FROM task_states "
            "WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND task_id = :task_id"
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id, "task_id": task_id},
    ).mappings().first()
    if row is None:
        return None
    events = load_task_state_events(db, task_state_id=str(row["id"]))
    return fold_task_state(
        events,
        task_id=str(row["task_id"]),
        workspace_id=str(row["workspace_id"]),
        owner_id=str(row["owner_id"]),
        session_id=str(row["session_id"] or ""),
        revision=int(row["revision"] or 0),
        now=datetime.now(tz=UTC),
    )


def ensure_task_state(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    subject_user_id: str,
    session_id: str,
    task_id: str,
    project_id: str | None,
    now: datetime,
) -> tuple[str, TaskStateProjection, int, str]:
    """INSERT ... ON CONFLICT DO NOTHING, then SELECT. **Never bumps the revision.**"""
    db.execute(
        text(
            """
            INSERT INTO task_states(
                id, workspace_id, owner_id, subject_user_id, session_id, task_id, project_id,
                revision, contract_revision, highest_seq, objective_text, objective_hash,
                objective_set_seq, status, next_permitted_action, plan_state,
                constraints_json, open_decisions_json, plan_json, unresolved_effects_json,
                latest_verification_json, citations_json, source_revision,
                cancelled_seq, last_cancel_seq, cancel_reason,
                created_at, updated_at, schema_version
            )
            VALUES(
                :id, :workspace_id, :owner_id, :subject_user_id, :session_id, :task_id,
                :project_id, 0, 0, 0, '', NULL, 0, :status, :next_permitted_action, :plan_state,
                CAST('[]' AS JSONB), CAST('[]' AS JSONB), NULL, CAST('[]' AS JSONB),
                NULL, CAST('[]' AS JSONB), '', 0, 0, '', :now, :now, :schema_version
            )
            ON CONFLICT (workspace_id, owner_id, task_id) DO NOTHING
            """
        ),
        {
            "id": uuid.uuid4(),
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "subject_user_id": subject_user_id,
            "session_id": session_id,
            "task_id": task_id,
            "project_id": project_id,
            "status": "awaiting_objective",
            "next_permitted_action": "await_owner_objective",
            "plan_state": PlanState.ABSENT.value,
            "now": now,
            "schema_version": TASK_STATE_SCHEMA_VERSION,
        },
    )
    row = db.execute(
        text(
            f"SELECT {_PROJECTION_COLUMNS} FROM task_states "
            "WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND task_id = :task_id"
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id, "task_id": task_id},
    ).mappings().first()
    if row is None:  # pragma: no cover - the INSERT above guarantees a row
        raise TaskStateRevisionConflict(task_id=task_id, expected_revision=0, actual_revision=-1)
    return (
        str(row["id"]),
        _projection_from_row(row),
        int(row["highest_seq"] or 0),
        str(row["source_revision"] or ""),
    )


def apply_task_state_events(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    task_id: str,
    new_events: Sequence[TaskStateEvent],
    now: datetime,
    expected_revision: int | None = None,
    invalidation: InvalidationPlan | None = None,
    apply_side_effects: ApplySideEffects | None = None,
    revalidate: Revalidate | None = None,
    retry_once: bool = False,
) -> TaskStateWrite:
    """Compare-and-swap write of the projection plus its new events.

    ``expected_revision=None`` (what both in-turn call sites pass) means "no caller
    precondition": the CAS uses the revision read under this call's own lock, so a second write
    in the same turn cannot conflict with the first. An integer is a caller precondition and,
    when it disagrees with the locked revision, raises before any UPDATE is issued.
    """
    savepoint = db.begin_nested()
    locked = db.execute(
        text(
            """
            SELECT id, revision, highest_seq
              FROM task_states
             WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND task_id = :task_id
             FOR UPDATE
            """
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id, "task_id": task_id},
    ).mappings().first()
    if locked is None:
        savepoint.rollback()
        raise TaskStateRevisionConflict(
            task_id=task_id,
            expected_revision=int(expected_revision or 0),
            actual_revision=-1,
        )

    task_state_id = str(locked["id"])
    locked_revision = int(locked["revision"] or 0)
    highest_seq = int(locked["highest_seq"] or 0)

    if expected_revision is not None and int(expected_revision) != locked_revision:
        savepoint.rollback()
        if not retry_once:
            raise TaskStateRevisionConflict(
                task_id=task_id,
                expected_revision=int(expected_revision),
                actual_revision=locked_revision,
            )
        return _retry_after_conflict(
            db,
            workspace_id=workspace_id,
            owner_id=owner_id,
            session_id=session_id,
            task_id=task_id,
            new_events=new_events,
            now=now,
            invalidation=invalidation,
            apply_side_effects=apply_side_effects,
            revalidate=revalidate,
        )

    prior_events = load_task_state_events(db, task_state_id=task_state_id)

    if invalidation is not None and apply_side_effects is not None:
        apply_side_effects(db, invalidation)

    write = prepare_write(
        prior_events,
        new_events,
        task_id=task_id,
        workspace_id=workspace_id,
        owner_id=owner_id,
        session_id=session_id,
        expected_revision=locked_revision,
        highest_seq=highest_seq,
        now=now,
    )

    for event in write.events:
        _insert_event(
            db,
            task_state_id=task_state_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
            task_id=task_id,
            event=event,
        )

    rowcount = _cas_update(
        db,
        task_state_id=task_state_id,
        locked_revision=locked_revision,
        write=write,
        now=now,
    )
    if rowcount == 1:
        savepoint.commit()
        return write

    savepoint.rollback()
    if not retry_once:
        raise TaskStateRevisionConflict(
            task_id=task_id,
            expected_revision=locked_revision,
            actual_revision=-1,
        )
    return _retry_after_conflict(
        db,
        workspace_id=workspace_id,
        owner_id=owner_id,
        session_id=session_id,
        task_id=task_id,
        new_events=new_events,
        now=now,
        invalidation=invalidation,
        apply_side_effects=apply_side_effects,
        revalidate=revalidate,
    )


def _retry_after_conflict(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    task_id: str,
    new_events: Sequence[TaskStateEvent],
    now: datetime,
    invalidation: InvalidationPlan | None,
    apply_side_effects: ApplySideEffects | None,
    revalidate: Revalidate | None,
) -> TaskStateWrite:
    """Re-read UNDER LOCK, ask the caller whether the write is still valid, then retry once.

    ``for_update=True`` is load-bearing, not defensive. Rolling back to the savepoint released
    every row lock the aborted subtransaction held, so an unlocked re-read opens a window
    between "revalidate said yes" and the recursive CAS — and the recursion runs with
    ``expected_revision=None``, i.e. with no precondition of its own. An objective change
    committed inside that window would therefore never be re-checked: the write would land at
    the superseded contract revision and author orphan ``autonomy_goals`` rows against a plan
    the owner had already replaced. Taking ``FOR UPDATE`` here holds lock 1 continuously from
    the revalidation through the recursive CAS, which is what makes the check binding.

    The lock is taken in the OUTER transaction (no savepoint of its own), so it survives until
    the caller commits — the recursion's own savepoint cannot release it.

    ``revalidate`` is carried into the recursion as well. With the lock held it cannot fire —
    the recursion's CAS cannot lose — but a future caller that passes ``retry_once=True`` down
    this path must not silently inherit an unvalidated write.
    """
    fresh = load_task_state(
        db, workspace_id=workspace_id, owner_id=owner_id, task_id=task_id, for_update=True
    )
    if fresh is None:
        raise TaskStatePreconditionFailed(task_id=task_id, reason="task_state_missing")
    if revalidate is not None:
        still_valid, reason = revalidate(fresh[0])
        if not still_valid:
            raise TaskStatePreconditionFailed(task_id=task_id, reason=reason)
    return apply_task_state_events(
        db,
        workspace_id=workspace_id,
        owner_id=owner_id,
        session_id=session_id,
        task_id=task_id,
        new_events=new_events,
        now=now,
        expected_revision=None,
        invalidation=invalidation,
        apply_side_effects=apply_side_effects,
        revalidate=revalidate,
        retry_once=False,
    )


def _insert_event(
    db: Session,
    *,
    task_state_id: str,
    workspace_id: str,
    owner_id: str,
    task_id: str,
    event: TaskStateEvent,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO task_state_events(
                id, task_state_id, workspace_id, owner_id, task_id, seq, kind,
                contract_revision, payload_json, source_event_id, directive_id, goal_id,
                actor, occurred_at, schema_version
            )
            VALUES(
                :id, :task_state_id, :workspace_id, :owner_id, :task_id, :seq, :kind,
                :contract_revision, CAST(:payload_json AS JSONB), :source_event_id,
                :directive_id, :goal_id, :actor, :occurred_at, :schema_version
            )
            ON CONFLICT (task_state_id, seq) DO NOTHING
            """
        ),
        {
            "id": uuid.uuid4(),
            "task_state_id": uuid.UUID(str(task_state_id)),
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "task_id": task_id,
            "seq": int(event.seq),
            "kind": str(event.kind),
            "contract_revision": int(event.contract_revision),
            "payload_json": canonical_json(event_payload_to_json(event.payload)),
            "source_event_id": event.source_event_id,
            "directive_id": event.directive_id,
            "goal_id": event.goal_id,
            "actor": event.actor,
            "occurred_at": event.occurred_at,
            "schema_version": TASK_STATE_SCHEMA_VERSION,
        },
    )


def _cas_update(
    db: Session,
    *,
    task_state_id: str,
    locked_revision: int,
    write: TaskStateWrite,
    now: datetime,
) -> int:
    projection = write.projection
    plan: ApprovedPlan | None = projection.plan
    result = db.execute(
        text(
            """
            UPDATE task_states
               SET revision = :next_revision,
                   contract_revision = :contract_revision,
                   highest_seq = :highest_seq,
                   objective_text = :objective_text,
                   objective_hash = :objective_hash,
                   objective_set_at = :objective_set_at,
                   objective_set_seq = :objective_set_seq,
                   status = :status,
                   next_permitted_action = :next_permitted_action,
                   plan_state = :plan_state,
                   plan_producer = :plan_producer,
                   plan_root_goal_id = :plan_root_goal_id,
                   constraints_json = CAST(:constraints_json AS JSONB),
                   open_decisions_json = CAST(:open_decisions_json AS JSONB),
                   plan_json = CAST(:plan_json AS JSONB),
                   unresolved_effects_json = CAST(:unresolved_effects_json AS JSONB),
                   latest_verification_json = CAST(:latest_verification_json AS JSONB),
                   citations_json = CAST(:citations_json AS JSONB),
                   source_revision = :source_revision,
                   cancelled_at = :cancelled_at,
                   cancelled_seq = :cancelled_seq,
                   last_cancel_seq = :last_cancel_seq,
                   cancel_reason = :cancel_reason,
                   updated_at = :now
             WHERE id = :task_state_id AND revision = :locked_revision
            """
        ),
        {
            "next_revision": int(write.next_revision),
            "contract_revision": int(projection.contract_revision),
            "highest_seq": _highest_seq(write),
            "objective_text": projection.objective_text,
            "objective_hash": projection.objective_hash,
            "objective_set_at": projection.objective_set_at,
            "objective_set_seq": int(projection.objective_set_seq),
            "status": str(projection.status),
            "next_permitted_action": str(projection.next_permitted_action),
            "plan_state": str(plan.state) if plan is not None else PlanState.ABSENT.value,
            "plan_producer": plan.producer if plan is not None else None,
            "plan_root_goal_id": plan.root_goal_id if plan is not None else None,
            "constraints_json": canonical_json(constraints_to_json(projection.constraints)),
            "open_decisions_json": canonical_json(decisions_to_json(projection.open_decisions)),
            "plan_json": canonical_json(approved_plan_to_json(plan)),
            "unresolved_effects_json": canonical_json(
                effects_to_json(projection.unresolved_effects)
            ),
            "latest_verification_json": canonical_json(
                verification_to_json(projection.latest_verification)
            ),
            "citations_json": canonical_json(list(projection.citations)),
            "source_revision": write.source_revision,
            "cancelled_at": projection.cancelled_at,
            "cancelled_seq": int(projection.cancelled_seq),
            "last_cancel_seq": int(projection.last_cancel_seq),
            "cancel_reason": projection.cancel_reason,
            "now": now,
            "task_state_id": uuid.UUID(str(task_state_id)),
            "locked_revision": int(locked_revision),
        },
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _highest_seq(write: TaskStateWrite) -> int:
    if write.events:
        return max(int(event.seq) for event in write.events)
    return int(write.next_seq_start) - 1


def cancel_task(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    task_id: str,
    reason: str,
    actor: str,
    now: datetime,
) -> TaskCancelResponse:
    """Cancel a task, in global lock order, with every downstream write fenced.

    The directive query keys on ``session_id`` rather than a task column: under D4 they hold the
    same value, and ``directive_executions.session_id`` is the column that is actually
    populated. That is what makes ``cancelled_directives`` a real count.
    """
    # 1. task_states (lock 1)
    db.execute(
        text(
            """
            SELECT id FROM task_states
             WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND task_id = :task_id
             FOR UPDATE
            """
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id, "task_id": task_id},
    ).mappings().first()

    # 2. planning_jobs (lock 2)
    job_rows = db.execute(
        text(
            """
            UPDATE planning_jobs
               SET cancel_requested = true, state = 'cancelled', updated_at = :now
             WHERE workspace_id = :workspace_id AND task_id = :task_id
               AND state IN ('pending', 'leased')
            RETURNING id, rq_job_id
            """
        ),
        {"workspace_id": workspace_id, "task_id": task_id, "now": now},
    ).mappings().all()

    # 3. directive_executions (lock 3)
    cancelled_directives = _cancel_pending_directives(
        db,
        workspace_id=workspace_id,
        owner_id=owner_id,
        session_id=session_id,
        now=now,
    )

    # 4. execution_permits (lock 4)
    permit_result = db.execute(
        text(
            """
            UPDATE execution_permits SET expires_at = :now
             WHERE workspace_id = :workspace_id AND session_id = :session_id
               AND (expires_at IS NULL OR expires_at > :now)
            """
        ),
        {"workspace_id": workspace_id, "session_id": session_id, "now": now},
    )
    expired_permits = int(getattr(permit_result, "rowcount", 0) or 0)

    # 5. the projection event itself
    write = apply_task_state_events(
        db,
        workspace_id=workspace_id,
        owner_id=owner_id,
        session_id=session_id,
        task_id=task_id,
        new_events=[
            TaskStateEvent(
                seq=0,
                kind=TaskStateEventKind.CANCELLATION_REQUESTED,
                contract_revision=0,
                payload={"reason": reason, "actor": actor},
                occurred_at=now,
                actor=actor,
            )
        ],
        now=now,
        expected_revision=None,
        retry_once=False,
    )

    # 6. best-effort queue cancellation, never fatal
    cancelled_rq_jobs = 0
    for job_row in job_rows:
        rq_job_id = job_row.get("rq_job_id")
        if not rq_job_id:
            continue
        try:
            from rq.job import Job  # noqa: PLC0415 - optional, best-effort

            from .cache_clients import get_redis_client  # noqa: PLC0415
            from .config import get_settings  # noqa: PLC0415

            redis_conn = get_redis_client(get_settings().redis_url)
            if redis_conn is not None:
                Job.fetch(str(rq_job_id), connection=redis_conn).cancel()
                cancelled_rq_jobs += 1
        except Exception:  # noqa: BLE001 - a queue that is down must not fail a cancel
            logger.warning("rq cancel failed for planning job %s", rq_job_id, exc_info=True)

    return TaskCancelResponse(
        task_id=task_id,
        revision=int(write.next_revision),
        cancelled_planning_jobs=len(job_rows),
        cancelled_directives=cancelled_directives,
        revoked_constraints=len(write.projection.constraints),
        expired_permits=expired_permits,
        cancelled_rq_jobs=cancelled_rq_jobs,
    )


def _cancel_pending_directives(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    now: datetime,
) -> int:
    rows = db.execute(
        text(
            """
            SELECT directive_id, state, claimed_by, claimed_executor, lease_generation
              FROM directive_executions
             WHERE workspace_id = :workspace_id AND user_id = :owner_id
               AND session_id = :session_id
               AND state IN ('pending', 'in_progress')
            """
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id, "session_id": session_id},
    ).mappings().all()
    cancelled = 0
    for row in rows:
        current_lease = int(row["lease_generation"] or 0)
        decision = validate_transition(
            current_state=str(row["state"]),
            claimed_by=row["claimed_by"],
            lease_generation=current_lease,
            requested="cancelled",
            actor=SYSTEM_ACTOR,
            actor_lease=None,
        )
        if not decision.allowed:
            continue
        result = db.execute(
            text(
                """
                UPDATE directive_executions
                   SET state = 'cancelled', cancel_reason = :cancel_reason,
                       cancelled_at = :now, finished_at = :now, updated_at = :now,
                       lease_generation = :next_lease
                 WHERE directive_id = :directive_id
                   AND state IN ('pending', 'in_progress')
                   AND lease_generation = :lease_generation
                """
            ),
            {
                "cancel_reason": CANCEL_REASON_TASK_CANCELLED,
                "now": now,
                "next_lease": int(decision.next_lease),
                "directive_id": row["directive_id"],
                "lease_generation": current_lease,
            },
        )
        cancelled += int(getattr(result, "rowcount", 0) or 0)
    return cancelled


def _projection_from_row(row: Any) -> TaskStateProjection:
    return TaskStateProjection(
        task_id=str(row["task_id"]),
        workspace_id=str(row["workspace_id"]),
        owner_id=str(row["owner_id"]),
        session_id=str(row["session_id"] or ""),
        revision=int(row["revision"] or 0),
        contract_revision=int(row["contract_revision"] or 0),
        objective_text=str(row["objective_text"] or ""),
        objective_hash=(str(row["objective_hash"]) if row["objective_hash"] else None),
        objective_set_at=_as_datetime(row["objective_set_at"]),
        objective_set_seq=int(row["objective_set_seq"] or 0),
        status=_as_status(row["status"]),
        next_permitted_action=_as_action(row["next_permitted_action"]),
        constraints=constraints_from_json(_as_json(row["constraints_json"])),
        open_decisions=decisions_from_json(_as_json(row["open_decisions_json"])),
        plan=approved_plan_from_json(_as_json(row["plan_json"])),
        unresolved_effects=effects_from_json(_as_json(row["unresolved_effects_json"])),
        latest_verification=verification_from_json(_as_json(row["latest_verification_json"])),
        citations=tuple(str(item) for item in (_as_json(row["citations_json"]) or [])),
        cancelled_at=_as_datetime(row["cancelled_at"]),
        cancelled_seq=int(row["cancelled_seq"] or 0),
        last_cancel_seq=int(row["last_cancel_seq"] or 0),
        cancel_reason=str(row["cancel_reason"] or ""),
    )


def _event_from_row(row: Any) -> TaskStateEvent:
    return TaskStateEvent(
        seq=int(row["seq"] or 0),
        kind=_as_kind(row["kind"]),
        contract_revision=int(row["contract_revision"] or 0),
        payload=event_payload_from_json(_as_json(row["payload_json"])),
        occurred_at=_as_datetime(row["occurred_at"]) or datetime.now(tz=UTC),
        actor=str(row["actor"] or ""),
        source_event_id=(str(row["source_event_id"]) if row["source_event_id"] else None),
        directive_id=(str(row["directive_id"]) if row["directive_id"] else None),
        goal_id=(str(row["goal_id"]) if row["goal_id"] else None),
    )


def _as_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return None


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_kind(value: Any) -> TaskStateEventKind | str:
    """An unknown kind stays a plain ``str`` — the fold counts it, it never raises."""
    try:
        return TaskStateEventKind(str(value))
    except ValueError:
        return str(value)


def _as_status(value: Any) -> Any:
    from tce_shared.task_state import TaskStatus  # noqa: PLC0415 - avoids a wide import surface

    try:
        return TaskStatus(str(value))
    except ValueError:
        return TaskStatus.AWAITING_VERIFICATION


def _as_action(value: Any) -> Any:
    from tce_shared.task_state import NextPermittedAction  # noqa: PLC0415

    try:
        return NextPermittedAction(str(value))
    except ValueError:
        return NextPermittedAction.NONE


# ------------------------------------------------------------------ verification provenance


def directive_contract_revision(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    task_id: str,
    directive_id: str,
) -> int:
    """The contract revision the DIRECTIVE's work happened under, read from its own history.

    ``task_state_events`` stamps every row with the ``contract_revision`` in force when it was
    appended and with the ``directive_id`` that caused it, so the earliest such row for a
    directive is the contract the work was done for.  This is the fact a verification's stamp has
    to come from: read from the projection at grading time instead, the stamp says only "the
    contract as of now", which is true of every verification and therefore excludes none.

    A directive with no rows at all -- one cancelled by an objective-change invalidation before it
    ever touched the projection, or reported while task state was off -- returns
    :data:`UNKNOWN_CONTRACT_REVISION`, which no real revision equals.  Guessing "current" there
    would restore exactly the hole this function exists to close.
    """
    row = db.execute(
        text(
            """
            SELECT MIN(e.contract_revision) AS contract_revision
            FROM task_state_events e
            JOIN task_states t ON t.id = e.task_state_id
            WHERE t.workspace_id = :workspace_id
              AND t.owner_id = :owner_id
              AND t.task_id = :task_id
              AND e.directive_id = CAST(:directive_id AS UUID)
            """
        ),
        {
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "task_id": task_id,
            "directive_id": str(directive_id),
        },
    ).mappings().first()
    if row is None or row.get("contract_revision") is None:
        return UNKNOWN_CONTRACT_REVISION
    return int(row["contract_revision"])


def insert_task_verification(
    db: Session,
    *,
    workspace_id: str,
    owner_id: str,
    task_id: str,
    ref: dict[str, Any],
    recorded_by: str,
    now: datetime,
) -> None:
    """Persist a stamped verification row. Callers stamp first; this only writes."""
    db.execute(
        text(
            """
            INSERT INTO task_verifications(
                id, workspace_id, owner_id, task_id, directive_id, state, method, summary,
                evidence_event_ids_json, recorded_by, recorded_at, schema_version,
                contract_revision, plan_id
            )
            VALUES(
                gen_random_uuid(), :workspace_id, :owner_id, :task_id,
                CAST(:directive_id AS UUID), :state, :method, :summary,
                CAST(:evidence_event_ids_json AS JSONB), :recorded_by, :recorded_at, 'v1',
                :contract_revision, :plan_id
            )
            """
        ),
        {
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "task_id": task_id,
            "directive_id": ref.get("directive_id"),
            "state": ref.get("state"),
            "method": ref.get("method"),
            "summary": ref.get("summary"),
            "evidence_event_ids_json": json.dumps(list(ref.get("evidence_event_ids") or [])),
            "recorded_by": recorded_by,
            "recorded_at": now,
            "contract_revision": int(ref.get("contract_revision") or 0),
            "plan_id": ref.get("plan_id"),
        },
    )
