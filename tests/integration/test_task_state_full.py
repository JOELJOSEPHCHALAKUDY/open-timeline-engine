"""Durable task state against the real Postgres (Builder B, design §10.2).

Skipped unless ``TCE_DATABASE_URL`` is set, and skipped with an explicit message when
migration ``20260909_0037`` has not been applied — a silent pass on a missing table would be a
gate that tests nothing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_api.db import get_session_factory
from tce_api.plan_rows import write_plan_rows
from tce_api.planning_store import enqueue_planning_job
from tce_api.task_state_store import (
    apply_task_state_events,
    cancel_task,
    ensure_task_state,
    load_task_state,
    load_task_state_events,
    rebuild_task_state,
)
from tce_shared.scope import PROJECT_UNBOUND, ResolvedScope
from tce_shared.task_state import (
    MAX_TASK_STATE_EVENTS_PER_FOLD,
    PINNED_KINDS,
    PLANNING_JOB_KIND_DECOMPOSE,
    PLANNING_PRODUCER_DETERMINISTIC,
    TASK_STATE_POLICY_REVISION,
    TaskStateEvent,
    TaskStateEventKind,
    TaskStatus,
    charter_for_task,
    deterministic_plan,
    fold_task_state,
    plan_input_revision,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TCE_DATABASE_URL"),
    reason="Full-backend integration tests need TCE_DATABASE_URL",
)

_WORKSPACE = "p2-task-state-full"
_OWNER = "codex-executor"
_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


@pytest.fixture()
def db() -> Iterator[Session]:
    session = get_session_factory()()
    present = session.execute(text("SELECT to_regclass('task_states')")).scalar()
    if present is None:
        session.rollback()
        session.close()
        pytest.skip("alembic revision 20260909_0037 has not been applied to this database")
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _scope(task_id: str) -> ResolvedScope:
    return ResolvedScope(
        workspace_id=_WORKSPACE,
        executor_id=_OWNER,
        owner_id=_OWNER,
        subject_user_id=_OWNER,
        project_id=None,
        project_binding=PROJECT_UNBOUND,
        task_id=task_id,
        owner_ids=frozenset({_OWNER}),
    )


def _fresh_task(db: Session) -> tuple[str, str]:
    """A brand new task id plus its ``task_states.id``. Cleans up after itself."""
    task_id = f"task-{uuid.uuid4()}"
    state_id, _projection, _seq, _rev = ensure_task_state(
        db,
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        subject_user_id=_OWNER,
        session_id=task_id,
        task_id=task_id,
        project_id=None,
        now=_NOW,
    )
    return task_id, state_id


def _objective(task_id: str, db: Session, text_value: str, contract_revision: int = 1) -> None:
    apply_task_state_events(
        db,
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        session_id=task_id,
        task_id=task_id,
        new_events=[
            TaskStateEvent(
                seq=0,
                kind=TaskStateEventKind.OBJECTIVE_SET,
                contract_revision=contract_revision,
                payload={"objective_text": text_value, "objective_hash": f"h-{text_value}"},
                occurred_at=_NOW,
                actor="tester",
            )
        ],
        now=_NOW,
        expected_revision=None,
    )


def test_ensure_is_idempotent_and_never_bumps_the_revision(db: Session) -> None:
    task_id, state_id = _fresh_task(db)
    again_id, projection, _seq, _rev = ensure_task_state(
        db,
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        subject_user_id=_OWNER,
        session_id=task_id,
        task_id=task_id,
        project_id=None,
        now=_NOW + timedelta(minutes=1),
    )
    assert again_id == state_id
    assert projection.revision == 0, "ensure must never bump the revision"


def test_two_writes_in_one_transaction_both_succeed(db: Session) -> None:
    """The CAS re-reads under its own lock, so a turn's second write cannot conflict with its
    first. Pinning the revision at the turn level made every first activation 409."""
    task_id, _state_id = _fresh_task(db)
    _objective(task_id, db, "first")
    write = apply_task_state_events(
        db,
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        session_id=task_id,
        task_id=task_id,
        new_events=[
            TaskStateEvent(
                seq=0,
                kind=TaskStateEventKind.PLAN_REQUESTED,
                contract_revision=1,
                payload={},
                occurred_at=_NOW,
                actor="tester",
            )
        ],
        now=_NOW,
        expected_revision=None,
    )
    assert write.next_revision == 2
    loaded = load_task_state(db, workspace_id=_WORKSPACE, owner_id=_OWNER, task_id=task_id)
    assert loaded is not None
    assert loaded[0].revision == 2
    assert loaded[0].objective_text == "first"


def test_deterministic_plan_writes_non_mutating_goal_rows(db: Session) -> None:
    """G4 Full. The value is predetermined; what this guards is the WRITE PATH — that
    ``write_plan_rows`` binds ``mutating`` from the step rather than from the column default,
    and that the migration's default is false."""
    task_id, _state_id = _fresh_task(db)
    steps = deterministic_plan("clean up the deploy script", charter=charter_for_task(max_steps=8))
    assert steps and all(step.mutating is False for step in steps)
    root_id = write_plan_rows(
        db,
        scope=_scope(task_id),
        session_id=task_id,
        task_id=task_id,
        steps=steps,
        producer=PLANNING_PRODUCER_DETERMINISTIC,
        contract_revision=1,
        now=_NOW,
    )
    rows = db.execute(
        text(
            """
            SELECT step_index, mutating, plan_contract_revision, depends_on_json, attempt_count
              FROM autonomy_goals
             WHERE session_id = :session_id AND workspace_id = :workspace_id
             ORDER BY step_index
            """
        ),
        {"session_id": task_id, "workspace_id": _WORKSPACE},
    ).mappings().all()
    assert len(rows) == len(steps) + 1, "one root row plus one row per step"
    assert all(row["mutating"] is False for row in rows)
    assert all(row["plan_contract_revision"] == 1 for row in rows)
    assert rows[0]["step_index"] == 0
    assert uuid.UUID(root_id)


def test_inline_planning_job_row_carries_scope_and_charter(db: Session) -> None:
    """The enqueue site is the ONLY producer of ``scope_json``; the worker rehydrates a
    ResolvedScope from it because it has no request and therefore no AuthContext."""
    task_id, state_id = _fresh_task(db)
    job_id, created = enqueue_planning_job(
        db,
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        session_id=task_id,
        task_id=task_id,
        task_state_id=state_id,
        job_kind=PLANNING_JOB_KIND_DECOMPOSE,
        input_revision=plan_input_revision(
            objective_hash="h1",
            contract_revision=1,
            policy_revision=TASK_STATE_POLICY_REVISION,
            scope_digest="digest",
            cancel_epoch=0,
        ),
        contract_revision=1,
        objective_hash="h1",
        objective_text="clean up the deploy script",
        charter=charter_for_task(max_steps=8),
        scope=_scope(task_id),
        now=_NOW,
        max_attempts=3,
        state="succeeded",
        queue_state="inline",
        producer=PLANNING_PRODUCER_DETERMINISTIC,
    )
    assert created is True
    row = db.execute(
        text(
            "SELECT state, queue_state, producer, scope_json, charter_json "
            "FROM planning_jobs WHERE id = CAST(:job_id AS UUID)"
        ),
        {"job_id": job_id},
    ).mappings().first()
    assert row is not None
    assert row["state"] == "succeeded"
    assert row["queue_state"] == "inline"
    assert row["producer"] == PLANNING_PRODUCER_DETERMINISTIC
    assert row["scope_json"]["workspace_id"] == _WORKSPACE
    assert row["charter_json"]["max_steps"] == 8


def test_cancel_epoch_makes_a_reissued_objective_a_new_job(db: Session) -> None:
    """A post-cancel turn must mint a FRESH job, not revive the one the owner just killed.

    The cancel epoch is folded into ``plan_input_revision``, so the idempotency key differs and
    the INSERT branch is taken.
    """
    task_id, _state_id = _fresh_task(db)
    _objective(task_id, db, "ship it")
    before = load_task_state(db, workspace_id=_WORKSPACE, owner_id=_OWNER, task_id=task_id)
    assert before is not None
    revision_before = plan_input_revision(
        objective_hash=before[0].objective_hash,
        contract_revision=before[0].contract_revision,
        policy_revision=TASK_STATE_POLICY_REVISION,
        scope_digest="digest",
        cancel_epoch=before[0].last_cancel_seq,
    )

    cancel_task(
        db,
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        session_id=task_id,
        task_id=task_id,
        reason="owner_cancelled",
        actor="tester",
        now=_NOW,
    )
    after = load_task_state(db, workspace_id=_WORKSPACE, owner_id=_OWNER, task_id=task_id)
    assert after is not None
    assert after[0].status is TaskStatus.CANCELLED
    assert after[0].last_cancel_seq > before[0].last_cancel_seq, "the epoch must advance"

    revision_after = plan_input_revision(
        objective_hash=after[0].objective_hash,
        contract_revision=after[0].contract_revision,
        policy_revision=TASK_STATE_POLICY_REVISION,
        scope_digest="digest",
        cancel_epoch=after[0].last_cancel_seq,
    )
    assert revision_after != revision_before


def test_truncation_window_matches_the_fold(db: Session) -> None:
    """R12 — the SQL window and the pure fold must retain the same events.

    Exactly one pinned kind is present, which is the common case and the one where the two
    formulas diverged.
    """
    task_id, state_id = _fresh_task(db)
    total = MAX_TASK_STATE_EVENTS_PER_FOLD + 400
    _bulk_insert_events(db, state_id=state_id, task_id=task_id, total=total)

    windowed = load_task_state_events(db, task_state_id=state_id)
    windowed_fold = fold_task_state(
        windowed,
        task_id=task_id,
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        session_id=task_id,
        revision=1,
        now=_NOW,
    )
    full_rows = db.execute(
        text(
            "SELECT seq, kind, contract_revision, payload_json, occurred_at, actor, "
            "source_event_id, directive_id, goal_id FROM task_state_events "
            "WHERE task_state_id = CAST(:state_id AS UUID) ORDER BY seq"
        ),
        {"state_id": state_id},
    ).mappings().all()
    full_fold = fold_task_state(
        [_event_from_mapping(row) for row in full_rows],
        task_id=task_id,
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        session_id=task_id,
        revision=1,
        now=_NOW,
    )
    assert windowed_fold.source_revision == full_fold.source_revision
    assert windowed_fold.projection.objective_text == full_fold.projection.objective_text
    assert windowed_fold.projection.objective_text == "the pinned objective"
    assert len(windowed) <= MAX_TASK_STATE_EVENTS_PER_FOLD + len(PINNED_KINDS)


def _bulk_insert_events(db: Session, *, state_id: str, task_id: str, total: int) -> None:
    rows: list[dict[str, Any]] = [
        {
            "id": uuid.uuid4(),
            "seq": 1,
            "kind": str(TaskStateEventKind.OBJECTIVE_SET),
            "payload": '{"objective_text": "the pinned objective", "objective_hash": "h-pin"}',
        }
    ]
    rows.extend(
        {
            "id": uuid.uuid4(),
            "seq": seq,
            "kind": str(TaskStateEventKind.STEP_COMPLETED),
            "payload": "{}",
        }
        for seq in range(2, total + 1)
    )
    db.execute(
        text(
            """
            INSERT INTO task_state_events(
                id, task_state_id, workspace_id, owner_id, task_id, seq, kind,
                contract_revision, payload_json, actor, occurred_at, schema_version
            )
            VALUES(
                :id, CAST(:state_id AS UUID), :workspace_id, :owner_id, :task_id, :seq, :kind,
                1, CAST(:payload AS JSONB), 'tester', :now, 'v1'
            )
            ON CONFLICT (task_state_id, seq) DO NOTHING
            """
        ),
        [
            {
                **row,
                "state_id": state_id,
                "workspace_id": _WORKSPACE,
                "owner_id": _OWNER,
                "task_id": task_id,
                "now": _NOW,
            }
            for row in rows
        ],
    )


def _event_from_mapping(row: Any) -> TaskStateEvent:
    return TaskStateEvent(
        seq=int(row["seq"]),
        kind=str(row["kind"]),
        contract_revision=int(row["contract_revision"] or 0),
        payload=row["payload_json"] or {},
        occurred_at=row["occurred_at"],
        actor=str(row["actor"] or ""),
    )


def test_rebuild_matches_the_stored_projection(db: Session) -> None:
    task_id, _state_id = _fresh_task(db)
    _objective(task_id, db, "rebuild me")
    stored = load_task_state(db, workspace_id=_WORKSPACE, owner_id=_OWNER, task_id=task_id)
    rebuilt = rebuild_task_state(
        db, workspace_id=_WORKSPACE, owner_id=_OWNER, task_id=task_id
    )
    assert stored is not None and rebuilt is not None
    assert rebuilt.projection.objective_text == stored[0].objective_text
    assert rebuilt.projection.contract_revision == stored[0].contract_revision
    assert rebuilt.source_revision == stored[2]


def test_a_blocked_step_never_turns_the_root_goal_done(db: Session) -> None:
    """G6's live half (design §0.3 G6, §4.5 items 4-5), against real rows.

    Step 2 is reported blocked and step 1 is then reported succeeded — the ordering that used
    to be fatal, because ``_select_next_plan_step`` returns ``None`` for "the next step is
    blocked" exactly as it does for "every step finished", and the completion path read that
    one ``None`` as "the plan is complete" and flipped the root goal to ``done``.

    Three things are asserted, and each one is a separate defect:
      (a) the root ``autonomy_goals`` row is NOT ``done``;
      (b) the projection reports ``BLOCKED``, with step 2 actually carrying ``status='blocked'``
          — which only holds if the emitted events carried the REAL ``step_index`` resolved
          from the goal row, rather than nothing (``STEP_COMPLETED``) or ``-1``
          (``STEP_BLOCKED``); both used to be folded away as ``"*:unknown_step"``, leaving every
          step of every projection at ``candidate`` forever;
      (c) ``_select_next_plan_step`` and ``_plan_root_status`` disagree — ``None`` and
          ``"blocked"`` — which is the discrimination the turn path short-circuits on.
    """
    from tce_api.auth import AuthContext
    from tce_api.main import (
        _advance_plan_after_completion,
        _blocked_plan_message,
        _goal_step_index,
        _plan_root_status,
        _select_next_plan_step,
    )
    from tce_shared.events import AgentRole, AutonomyGoalStatus, TakeoverState
    from tce_shared.task_state import plan_steps_to_json

    objective = "ship the blocked plan"
    task_id, _state_id = _fresh_task(db)
    _objective(task_id, db, objective, contract_revision=1)
    steps = deterministic_plan(objective, charter=charter_for_task(max_steps=4))
    assert len(steps) >= 2, "the scenario needs a step to block behind a completed one"
    root_id = write_plan_rows(
        db,
        scope=_scope(task_id),
        session_id=task_id,
        task_id=task_id,
        steps=steps,
        producer=PLANNING_PRODUCER_DETERMINISTIC,
        contract_revision=1,
        now=_NOW,
        objective_text=objective,
    )
    apply_task_state_events(
        db,
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        session_id=task_id,
        task_id=task_id,
        new_events=[
            TaskStateEvent(
                seq=0,
                kind=TaskStateEventKind.PLAN_APPROVED,
                contract_revision=1,
                payload={
                    "producer": PLANNING_PRODUCER_DETERMINISTIC,
                    "root_goal_id": root_id,
                    "steps": plan_steps_to_json(steps),
                },
                occurred_at=_NOW,
                actor="tester",
            )
        ],
        now=_NOW,
        expected_revision=None,
    )

    goal_ids = {
        int(row["step_index"]): str(row["id"])
        for row in db.execute(
            text(
                """
                SELECT id, step_index FROM autonomy_goals
                 WHERE session_id = :s AND workspace_id = :w AND user_id = :u
                   AND parent_goal_id = CAST(:root AS uuid)
                """
            ),
            {"s": task_id, "w": _WORKSPACE, "u": _OWNER, "root": root_id},
        ).mappings()
    }
    assert set(goal_ids) == {int(step.step_index) for step in steps}

    state = TakeoverState(
        session_id=task_id,
        workspace_id=_WORKSPACE,
        user_id=_OWNER,
        updated_at=_NOW,
        takeover_context={"plan_root_goal_id": root_id, "plan_step_count": len(steps)},
    )
    auth = AuthContext(
        consumer="tester",
        mode="server",
        role=AgentRole.EXECUTOR,
        workspace_id=_WORKSPACE,
        user_id=_OWNER,
    )

    # The index the events must carry is read off the goal row, not invented.
    assert _goal_step_index(db, state=state, goal_id=goal_ids[2]) == 2
    assert _goal_step_index(db, state=state, goal_id=root_id) is None, "the root is not a step"
    assert _goal_step_index(db, state=state, goal_id="not-a-uuid") is None

    _report(db, auth=auth, state=state, goal_id=goal_ids[2], failed=True)
    db.execute(
        text(
            """
            UPDATE autonomy_goals
               SET status = :status, blocked_reason = :reason,
                   attempt_count = attempt_count + 1, updated_at = :now
             WHERE id = CAST(:id AS uuid)
            """
        ),
        {
            "status": AutonomyGoalStatus.BLOCKED.value,
            "reason": "upstream service is down",
            "now": _NOW,
            "id": goal_ids[2],
        },
    )
    contract_revision = _report(db, auth=auth, state=state, goal_id=goal_ids[1], failed=False)
    db.execute(
        text("UPDATE autonomy_goals SET status = :s WHERE id = CAST(:id AS uuid)"),
        {"s": AutonomyGoalStatus.DONE.value, "id": goal_ids[1]},
    )

    # (c) the discrimination the turn path and the completion path both make.
    assert _select_next_plan_step(db, state, contract_revision=contract_revision) is None
    assert _plan_root_status(db, state, contract_revision=contract_revision) == "blocked"
    message = _blocked_plan_message(db, state, contract_revision=contract_revision)
    assert "blocked" in message.lower() and "upstream service is down" in message

    context = dict(state.takeover_context)
    _advance_plan_after_completion(
        db,
        auth=auth,
        state=state,
        context=context,
        contract_revision=contract_revision,
    )

    # (a) the root goal row.
    root_row_status = db.execute(
        text("SELECT status FROM autonomy_goals WHERE id = CAST(:id AS uuid)"),
        {"id": root_id},
    ).scalar()
    assert root_row_status != AutonomyGoalStatus.DONE.value, (
        "a plan whose next step is blocked has not finished; marking the root done is how a "
        "blocked dependency reached DONE"
    )
    assert context.get("plan_root_goal_id") == root_id, "the stalled plan stays pinned"
    assert "plan_completed_at" not in context

    # (b) the projection.
    loaded = load_task_state(db, workspace_id=_WORKSPACE, owner_id=_OWNER, task_id=task_id)
    assert loaded is not None
    projection = loaded[0]
    assert projection.plan is not None
    by_index = {int(step.step_index): step for step in projection.plan.steps}
    assert by_index[1].status == "done", (
        "STEP_COMPLETED carried no step_index at all, so the fold discarded it as unknown_step"
    )
    assert by_index[2].status == "blocked", (
        "STEP_BLOCKED hardcoded step_index=-1, so the fold discarded it as unknown_step"
    )
    assert by_index[2].blocked_reason == "upstream service is down"
    assert projection.status is TaskStatus.BLOCKED
    assert projection.plan.root_status() == "blocked"


def _report(
    db: Session,
    *,
    auth: Any,
    state: Any,
    goal_id: str,
    failed: bool,
) -> int:
    """One ``report_execution`` worth of task-state events. Returns the contract revision."""
    from tce_api.main import _record_execution_task_state
    from tce_shared.events import DirectiveExecutionState, ExecutionReportRequest

    directive_id = str(uuid.uuid4())
    effective = (
        DirectiveExecutionState.FAILED if failed else DirectiveExecutionState.SUCCEEDED
    )
    return _record_execution_task_state(
        db,
        auth=auth,
        state=state,
        body=ExecutionReportRequest(
            session_id=state.session_id,
            directive_id=uuid.UUID(directive_id),
            state=effective,
            result="failure" if failed else "success",
            failure_reason="upstream service is down" if failed else None,
        ),
        directive_id=directive_id,
        goal_id=goal_id,
        effective_state=effective,
        now=_NOW,
    )


def test_stand_down_then_reactivating_on_the_same_objective_unblocks_dispatch(
    db: Session,
) -> None:
    """RULING V1, Full half: revival on an UNCHANGED objective.

    CLAUDE.md mandates one stable ``session_id``, ``reset_takeover_state`` on stand-down, and
    re-activation later. Because the re-activation restates the SAME objective its hash does
    not change, so "did the objective change?" says no — and with no ``OBJECTIVE_SET``
    appended, the retained ``CANCELLATION_REQUESTED`` stays newer than the last
    ``OBJECTIVE_SET`` forever, R1 keeps returning ``CANCELLED``, and the session is
    dispatch-blocked for good. This drives the real turn function on both sides of a real
    stand-down.
    """
    from tce_api.auth import AuthContext
    from tce_api.main import _record_stand_down, _takeover_turn_task_state
    from tce_shared.events import AgentRole, TakeoverState, TakeoverStepRequest

    objective = "keep shipping the same thing"
    task_id, _state_id = _fresh_task(db)
    auth = AuthContext(
        consumer="tester",
        mode="server",
        role=AgentRole.EXECUTOR,
        workspace_id=_WORKSPACE,
        user_id=_OWNER,
    )
    state = TakeoverState(
        session_id=task_id,
        workspace_id=_WORKSPACE,
        user_id=_OWNER,
        active=True,
        updated_at=_NOW,
    )
    body = TakeoverStepRequest(message=objective, session_id=task_id, task=objective)

    def _turn(now: datetime) -> Any:
        return _takeover_turn_task_state(
            db,
            auth=auth,
            scope=_scope(task_id),
            state=state,
            body=body,
            resolved_task=objective,
            awaiting_next_objective=False,
            has_new_objective_signal=True,
            pending_objective=None,
            now=now,
        )

    first = _turn(_NOW)
    assert first.projection is not None
    assert first.projection.objective_hash
    assert first.dispatch_blocked is False
    contract_after_activation = int(first.projection.contract_revision)

    _record_stand_down(db, auth=auth, session_id=task_id, now=_NOW + timedelta(minutes=1))
    stood_down = load_task_state(
        db, workspace_id=_WORKSPACE, owner_id=_OWNER, task_id=task_id
    )
    assert stood_down is not None
    assert stood_down[0].status is TaskStatus.CANCELLED

    revived = _turn(_NOW + timedelta(minutes=2))
    assert revived.projection is not None
    assert revived.projection.status is not TaskStatus.CANCELLED, (
        "an activation on the same objective must append OBJECTIVE_SET on a cancelled "
        "projection; without it the session is dispatch-blocked for life"
    )
    assert revived.dispatch_blocked is False, "revival must unblock dispatch, not just status"
    # Revival is not a contract change: the owner never restated a DIFFERENT objective, so
    # nothing was invalidated and the approved plan is still the one being executed.
    assert int(revived.projection.contract_revision) == contract_after_activation
    assert revived.projection.objective_hash == first.projection.objective_hash
    # S4: the cancel epoch is a high-water mark that revival never clears.
    assert int(revived.projection.last_cancel_seq) >= int(stood_down[0].last_cancel_seq)

    # Idempotent: a second turn on a live projection appends no further OBJECTIVE_SET.
    before = len(load_task_state_events(db, task_state_id=_state_id))
    again = _turn(_NOW + timedelta(minutes=3))
    assert again.projection is not None
    assert again.projection.status is not TaskStatus.CANCELLED
    assert len(load_task_state_events(db, task_state_id=_state_id)) == before


def test_a_blocked_report_is_not_swallowed_by_the_full_backend(db: Session) -> None:
    """G6, the parity leg: ``state="blocked"`` must stall the step in Full exactly as in Lite.

    Full narrowed the blocking report states to FAILED and CANCELLED while the Lite twin
    (``store.py::_record_execution_task_state_lite``) used all four, so an executor reporting
    the most literally named block there is — ``state="blocked"`` — emitted no ``STEP_BLOCKED``
    and moved no goal row in Full. The step stayed ``candidate`` forever, ``/v1/tasks/{id}/state``
    reported ACTIVE where Lite reported BLOCKED, and the next turn re-dispatched the blocker
    instead of surfacing it. The two sets are asserted equal so they cannot drift apart again.
    """
    from tce_api.auth import AuthContext
    from tce_api.main import _BLOCKING_REPORT_STATES, _record_execution_task_state
    from tce_shared.events import (
        AgentRole,
        DirectiveExecutionState,
        ExecutionReportRequest,
        TakeoverState,
    )
    from tce_shared.task_state import plan_steps_to_json

    assert _BLOCKING_REPORT_STATES == frozenset(
        {
            DirectiveExecutionState.FAILED,
            DirectiveExecutionState.BLOCKED,
            DirectiveExecutionState.ABANDONED,
            DirectiveExecutionState.CANCELLED,
        }
    ), "Full and Lite must agree on which reports stall a plan step"

    objective = "report a blocked step to the full backend"
    task_id, _state_id = _fresh_task(db)
    _objective(task_id, db, objective, contract_revision=1)
    steps = deterministic_plan(objective, charter=charter_for_task(max_steps=4))
    root_id = write_plan_rows(
        db,
        scope=_scope(task_id),
        session_id=task_id,
        task_id=task_id,
        steps=steps,
        producer=PLANNING_PRODUCER_DETERMINISTIC,
        contract_revision=1,
        now=_NOW,
        objective_text=objective,
    )
    apply_task_state_events(
        db,
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        session_id=task_id,
        task_id=task_id,
        new_events=[
            TaskStateEvent(
                seq=0,
                kind=TaskStateEventKind.PLAN_APPROVED,
                contract_revision=1,
                payload={
                    "producer": PLANNING_PRODUCER_DETERMINISTIC,
                    "root_goal_id": root_id,
                    "steps": plan_steps_to_json(steps),
                },
                occurred_at=_NOW,
                actor="tester",
            )
        ],
        now=_NOW,
        expected_revision=None,
    )
    goal_id = str(
        db.execute(
            text(
                """
                SELECT id FROM autonomy_goals
                 WHERE parent_goal_id = CAST(:root AS uuid) AND step_index = 2
                """
            ),
            {"root": root_id},
        ).scalar()
    )
    state = TakeoverState(
        session_id=task_id,
        workspace_id=_WORKSPACE,
        user_id=_OWNER,
        updated_at=_NOW,
        takeover_context={"plan_root_goal_id": root_id, "plan_step_count": len(steps)},
    )
    auth = AuthContext(
        consumer="tester",
        mode="server",
        role=AgentRole.EXECUTOR,
        workspace_id=_WORKSPACE,
        user_id=_OWNER,
    )
    directive_id = str(uuid.uuid4())
    _record_execution_task_state(
        db,
        auth=auth,
        state=state,
        body=ExecutionReportRequest(
            session_id=task_id,
            directive_id=uuid.UUID(directive_id),
            state=DirectiveExecutionState.BLOCKED,
            result="failure",
            failure_reason="upstream dependency unavailable",
        ),
        directive_id=directive_id,
        goal_id=goal_id,
        effective_state=DirectiveExecutionState.BLOCKED,
        now=_NOW,
    )

    loaded = load_task_state(db, workspace_id=_WORKSPACE, owner_id=_OWNER, task_id=task_id)
    assert loaded is not None
    projection = loaded[0]
    assert projection.plan is not None
    by_index = {int(step.step_index): step for step in projection.plan.steps}
    assert by_index[2].status == "blocked", (
        "a state='blocked' report emitted no STEP_BLOCKED at all in Full, so the step stayed "
        "candidate and the projection reported ACTIVE where Lite reported BLOCKED"
    )
    assert by_index[2].blocked_reason == "upstream dependency unavailable"
    assert projection.plan.root_status() == "blocked"
    assert projection.status is TaskStatus.BLOCKED
