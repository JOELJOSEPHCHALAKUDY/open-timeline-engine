"""``tce_worker.jobs.planning.run`` against the real Postgres (design §8.3-§8.5).

``tests/unit/test_worker_planning.py`` proves the job's *decisions* against a fake session:
the ordering, the refusals, and the SQL text of the fence. What it cannot prove is that any
of those decisions survive contact with a database — a `FOR UPDATE SKIP LOCKED` claim only
means anything when two connections actually contend for a row, and a fenced UPDATE only
means anything when a real ``rowcount`` comes back 0.

So this file runs the real ``run()`` against real rows, on the three properties the job's
docstring promises:

* **Stale results cannot dispatch work** — a job whose ``input_revision`` no longer matches
  the projection is discarded having written no ``autonomy_goals`` row and no task-state
  event.
* **A lost lease writes nothing** — a cancel that lands *while the model is running* makes
  the completion UPDATE match zero rows, and the whole transaction (task state + goal rows)
  rolls back with it.
* **Exactly one worker wins a job** — two connections claiming the same row concurrently.

Skipped unless ``TCE_DATABASE_URL`` is set, and skipped with an explicit message when
migration ``20260909_0037`` has not been applied: a silent pass on a missing table would be a
gate that tests nothing.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_api.db import get_session_factory
from tce_api.planning_store import (
    claim_planning_job,
    complete_planning_job,
    enqueue_planning_job,
    get_planning_job,
)
from tce_api.task_state_store import apply_task_state_events, ensure_task_state, load_task_state
from tce_shared.scope import PROJECT_UNBOUND, ResolvedScope
from tce_shared.task_state import (
    PLANNING_JOB_KIND_DECOMPOSE,
    PLANNING_LOST_LEASE_REASON,
    PLANNING_PRODUCER_MODEL,
    PLANNING_STALE_REASON,
    PlanCharter,
    TaskStateEvent,
    TaskStateEventKind,
    TaskStateProjection,
)
from tce_worker.jobs import planning

pytestmark = pytest.mark.skipif(
    not os.environ.get("TCE_DATABASE_URL"),
    reason="Full-backend integration tests need TCE_DATABASE_URL",
)

_WORKSPACE = "p2-worker-planning-live"
_OWNER = "codex-executor"
_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

_MODEL_PLAN = {
    "steps": [
        {"id": "a", "title": "read the failing test", "description": "start where it breaks"},
        {
            "id": "b",
            "title": "explain the failure",
            "description": "say what the test proves",
            "depends_on": ["a"],
        },
    ]
}


class _Settings:
    """Only the fields ``planning.run`` reads. The model is ON — these tests need the job to
    reach its write-back, which the default ``takeover_plan_llm_enabled=False`` would refuse."""

    planning_enabled = True
    planning_job_lease_seconds = 120
    planning_job_max_attempts = 3
    planning_job_batch_size = 20
    planning_job_backoff_cap_seconds = 900
    takeover_plan_llm_enabled = True
    takeover_plan_max_steps = 8
    takeover_plan_llm_timeout_seconds = 25.0
    model_provider = "ollama"
    redis_url = ""


class _Gateway:
    """A stand-in for the model call — and, optionally, the window in which the world moves.

    ``during`` runs *inside* ``extract_structured``, which is precisely where a real cancel
    lands: after the lease was taken and committed, before the write-back is attempted.
    """

    def __init__(self, *, during: Any = None) -> None:
        self.during = during
        self.calls = 0

    def extract_structured(self, prompt: str, schema_name: str) -> Any:
        self.calls += 1
        _ = prompt, schema_name
        if self.during is not None:
            self.during()
        return _MODEL_PLAN


@pytest.fixture()
def db() -> Iterator[Session]:
    session = get_session_factory()()
    if session.execute(text("SELECT to_regclass('planning_jobs')")).scalar() is None:
        session.rollback()
        session.close()
        pytest.skip("alembic revision 20260909_0037 has not been applied to this database")
    try:
        yield session
    finally:
        session.rollback()
        _purge(session)
        session.close()


@pytest.fixture()
def worker(monkeypatch: pytest.MonkeyPatch) -> _Gateway:
    """``run()`` with the settings it needs and a gateway that never leaves the process."""
    gateway = _Gateway()
    monkeypatch.setattr(planning, "get_settings", lambda: _Settings())
    monkeypatch.setattr(planning, "get_gateway", lambda _settings: gateway)
    return gateway


def _purge(session: Session) -> None:
    """The worker commits its own sessions, so teardown cannot rely on a rollback."""
    for statement in (
        "DELETE FROM autonomy_goals WHERE workspace_id = :ws",
        "DELETE FROM planning_jobs WHERE workspace_id = :ws",
        "DELETE FROM task_state_events WHERE workspace_id = :ws",
        "DELETE FROM task_states WHERE workspace_id = :ws",
    ):
        session.execute(text(statement), {"ws": _WORKSPACE})
    session.commit()


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


def _seed_task(db: Session, objective: str = "ship the takeover fix") -> tuple[str, str, TaskStateProjection]:
    """A committed task with an objective — the worker reads it on its own connection."""
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
                contract_revision=1,
                payload={"objective_text": objective, "objective_hash": f"h-{objective}"},
                occurred_at=_NOW,
                actor="tester",
            )
        ],
        now=_NOW,
        expected_revision=None,
    )
    db.commit()
    loaded = load_task_state(db, workspace_id=_WORKSPACE, owner_id=_OWNER, task_id=task_id)
    assert loaded is not None
    projection, _highest, _source = loaded
    return task_id, state_id, projection


def _enqueue(
    db: Session,
    *,
    task_id: str,
    state_id: str,
    projection: TaskStateProjection,
    input_revision: str,
    contract_revision: int | None = None,
) -> str:
    job_id, created = enqueue_planning_job(
        db,
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        session_id=task_id,
        task_id=task_id,
        task_state_id=state_id,
        job_kind=PLANNING_JOB_KIND_DECOMPOSE,
        input_revision=input_revision,
        contract_revision=projection.contract_revision if contract_revision is None else contract_revision,
        objective_hash=projection.objective_hash,
        objective_text=projection.objective_text,
        charter=PlanCharter(max_steps=4),
        scope=_scope(task_id),
        now=_NOW,
        max_attempts=3,
    )
    assert created, "the fixture must enqueue a fresh job, not revive one"
    db.commit()
    return job_id


def _live_input_revision(task_id: str, projection: TaskStateProjection) -> str:
    return planning._current_input_revision(projection, _scope(task_id))


def _goal_count(db: Session, task_id: str) -> int:
    return int(
        db.execute(
            text("SELECT count(*) FROM autonomy_goals WHERE workspace_id = :ws AND session_id = :sid"),
            {"ws": _WORKSPACE, "sid": task_id},
        ).scalar()
        or 0
    )


def _event_kinds(db: Session, task_id: str) -> list[str]:
    return [
        str(row[0])
        for row in db.execute(
            text(
                "SELECT kind FROM task_state_events "
                " WHERE workspace_id = :ws AND task_id = :tid ORDER BY seq"
            ),
            {"ws": _WORKSPACE, "tid": task_id},
        ).all()
    ]


def _revision(db: Session, task_id: str) -> int:
    return int(
        db.execute(
            text("SELECT revision FROM task_states WHERE workspace_id = :ws AND task_id = :tid"),
            {"ws": _WORKSPACE, "tid": task_id},
        ).scalar()
        or 0
    )


# ------------------------------------------------------------------ positive control


def test_a_live_job_writes_the_plan_and_succeeds(db: Session, worker: _Gateway) -> None:
    """Without this, every "wrote nothing" assertion below could be passing vacuously."""
    task_id, state_id, projection = _seed_task(db)
    job_id = _enqueue(
        db,
        task_id=task_id,
        state_id=state_id,
        projection=projection,
        input_revision=_live_input_revision(task_id, projection),
    )
    before = _revision(db, task_id)
    db.commit()

    assert planning.run(job_id) == {
        "status": "succeeded",
        "processed": 1,
        "succeeded": 1,
        "failed": 0,
        "discarded": 0,
        "cancelled": 0,
        "skipped": 0,
    }

    row = get_planning_job(db, workspace_id=_WORKSPACE, job_id=job_id)
    assert row is not None
    assert row["state"] == "succeeded"
    assert row["producer"] == PLANNING_PRODUCER_MODEL
    assert row["lease_owner"] is None, "a completed job must release its lease"
    # A root row plus one row per model step.
    assert _goal_count(db, task_id) == 3
    assert TaskStateEventKind.PLAN_APPROVED.value in _event_kinds(db, task_id)
    assert _revision(db, task_id) > before


# ------------------------------------------------------------------ stale input revision


def test_stale_input_revision_is_discarded_and_writes_nothing(db: Session, worker: _Gateway) -> None:
    """Design §8.4 step 3 / gate G2, against real rows rather than a transcript.

    The objective moved on while the job sat in the queue, so the plan the job would produce
    is for a contract nobody is on any more. It must never reach ``autonomy_goals``.
    """
    task_id, state_id, projection = _seed_task(db)
    job_id = _enqueue(
        db,
        task_id=task_id,
        state_id=state_id,
        projection=projection,
        input_revision="an-input-revision-from-a-superseded-objective",
    )
    kinds_before = _event_kinds(db, task_id)
    revision_before = _revision(db, task_id)
    db.commit()

    result = planning.run(job_id)
    assert result["status"] == "discarded"
    assert result["reason"] == PLANNING_STALE_REASON
    assert worker.calls == 0, "a stale job must be refused before the model is called"

    row = get_planning_job(db, workspace_id=_WORKSPACE, job_id=job_id)
    assert row is not None
    assert row["state"] == "discarded"
    assert row["last_error"] == PLANNING_STALE_REASON
    assert row["lease_owner"] is None
    assert row["attempts"] == 1, "the row really was claimed and leased before being refused"

    assert _goal_count(db, task_id) == 0
    assert _event_kinds(db, task_id) == kinds_before
    assert _revision(db, task_id) == revision_before


# ------------------------------------------------------------------ the cancel fence


def test_cancel_during_the_model_call_makes_the_fence_match_zero_rows(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Design §8.5 step 3 — the third fence predicate, with a real ``rowcount``.

    ``lease_owner`` and ``state = 'leased'`` both still hold here: this worker owns the lease
    and never lost it. The only thing that changed is ``cancel_requested``, flipped on another
    connection while the model was running. Without ``AND cancel_requested = false`` the
    completion lands, and with it the plan write that shares its transaction — the owner
    cancels and gets a plan anyway.
    """
    task_id, state_id, projection = _seed_task(db)
    job_id = _enqueue(
        db,
        task_id=task_id,
        state_id=state_id,
        projection=projection,
        input_revision=_live_input_revision(task_id, projection),
    )
    kinds_before = _event_kinds(db, task_id)
    revision_before = _revision(db, task_id)
    db.commit()

    def _cancel_on_another_connection() -> None:
        with get_session_factory()() as other:
            other.execute(
                text(
                    "UPDATE planning_jobs SET cancel_requested = true "
                    " WHERE id = CAST(:job_id AS UUID)"
                ),
                {"job_id": job_id},
            )
            other.commit()

    gateway = _Gateway(during=_cancel_on_another_connection)
    monkeypatch.setattr(planning, "get_settings", lambda: _Settings())
    monkeypatch.setattr(planning, "get_gateway", lambda _settings: gateway)

    result = planning.run(job_id)
    assert gateway.calls == 1, "the cancel must land mid-flight, not before the claim"
    assert result["status"] == PLANNING_LOST_LEASE_REASON

    row = get_planning_job(db, workspace_id=_WORKSPACE, job_id=job_id)
    assert row is not None
    assert bool(row["cancel_requested"]) is True
    assert row["state"] == "leased", "the fence matched 0 rows, so the row is untouched"
    assert row["lease_owner"] is not None, "the lease was never lost — only the task was cancelled"
    assert row["producer"] is None

    # Everything the job had already staged went back with the rollback.
    assert _goal_count(db, task_id) == 0
    assert _event_kinds(db, task_id) == kinds_before
    assert _revision(db, task_id) == revision_before

    # And the fence itself, called directly with the *correct* lease owner and a row still in
    # 'leased': the only predicate that can be failing is cancel_requested.
    with get_session_factory()() as fresh:
        assert (
            complete_planning_job(
                fresh,
                job_id=job_id,
                lease_owner=str(row["lease_owner"]),
                state="succeeded",
                producer=PLANNING_PRODUCER_MODEL,
                result={"steps": 2},
                last_error=None,
                next_attempt_at=None,
                now=datetime.now(tz=UTC),
            )
            is False
        )
        fresh.rollback()


# ------------------------------------------------------------------ the lease race


def test_two_connections_racing_for_one_job_produce_exactly_one_winner(db: Session) -> None:
    """``FOR UPDATE SKIP LOCKED`` plus the immediate commit, on two real connections.

    Two workers reach for the same row at the same instant. One leases it; the other must
    come back empty-handed rather than blocking, planning the same objective twice, or
    overwriting the winner's lease.
    """
    task_id, state_id, projection = _seed_task(db)
    job_id = _enqueue(
        db,
        task_id=task_id,
        state_id=state_id,
        projection=projection,
        input_revision=_live_input_revision(task_id, projection),
    )
    db.commit()

    factory = get_session_factory()  # warm the lazy engine before the threads start
    barrier = threading.Barrier(2)
    outcomes: dict[str, list[Any]] = {}
    lock = threading.Lock()

    def _claim(owner: str) -> None:
        session = factory()
        try:
            barrier.wait(timeout=10)
            rows = claim_planning_job(
                session,
                job_id=job_id,
                lease_owner=owner,
                lease_seconds=120,
                now=datetime.now(tz=UTC),
                batch_size=5,
            )
        finally:
            session.rollback()
            session.close()
        with lock:
            outcomes[owner] = list(rows)

    threads = [
        threading.Thread(target=_claim, args=(f"worker-{index}",), name=f"claim-{index}")
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a claim blocked — SKIP LOCKED is not doing its job"

    assert set(outcomes) == {"worker-0", "worker-1"}
    winners = [owner for owner, rows in outcomes.items() if rows]
    assert len(winners) == 1, f"exactly one worker may lease the job, got {outcomes}"

    row = get_planning_job(db, workspace_id=_WORKSPACE, job_id=job_id)
    assert row is not None
    assert row["state"] == "leased"
    assert row["lease_owner"] == winners[0]
    assert row["attempts"] == 1, "the loser must not have burned an attempt"
    assert row["lease_until"] is not None

    claimed = outcomes[winners[0]][0]
    assert str(claimed["id"]) == job_id
    # The two columns the worker cannot rebuild from settings, carried on the claim itself.
    assert claimed["scope_json"], "the claim must return scope_json"
    assert claimed["charter_json"], "the claim must return charter_json"


def test_a_row_another_connection_holds_is_skipped_not_waited_on(db: Session) -> None:
    """The deterministic half of the race: ``SKIP LOCKED``, not a lock wait.

    The barrier race above proves exactly one winner whichever way the two connections
    interleave — including the boring one where the second claim simply finds ``state =
    'leased'``. This pins the interesting one: while another transaction holds the row's lock
    uncommitted, the claim must come back empty *immediately*. A worker that blocked here
    would hold a queue slot for the whole of someone else's model call.
    """
    task_id, state_id, projection = _seed_task(db)
    job_id = _enqueue(
        db,
        task_id=task_id,
        state_id=state_id,
        projection=projection,
        input_revision=_live_input_revision(task_id, projection),
    )
    db.commit()

    factory = get_session_factory()
    holder = factory()
    rows: list[Any] = []
    try:
        locked = holder.execute(
            text("SELECT id FROM planning_jobs WHERE id = CAST(:job_id AS UUID) FOR UPDATE"),
            {"job_id": job_id},
        ).all()
        assert len(locked) == 1  # the lock is held, and NOT committed

        def _claim_while_locked() -> None:
            session = factory()
            try:
                rows.extend(
                    claim_planning_job(
                        session,
                        job_id=job_id,
                        lease_owner="worker-late",
                        lease_seconds=120,
                        now=datetime.now(tz=UTC),
                        batch_size=5,
                    )
                )
            finally:
                session.rollback()
                session.close()

        thread = threading.Thread(target=_claim_while_locked, name="late-claim")
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive(), "the claim waited on the lock instead of skipping it"
    finally:
        holder.rollback()
        holder.close()

    assert rows == []
    row = get_planning_job(db, workspace_id=_WORKSPACE, job_id=job_id)
    assert row is not None
    assert row["state"] == "pending", "a skipped row must stay claimable"
    assert row["lease_owner"] is None
    assert row["attempts"] == 0
