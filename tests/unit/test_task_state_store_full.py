"""Full task-state store, driven against a transcript-recording fake Session.

No Postgres here: these tests assert the SHAPE of what the store issues — statement order, lock
order, savepoint discipline, and that it never commits or rolls back the caller's transaction.
The behaviour under real concurrency is the integration file's job.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from tce_api.task_state_store import (
    apply_task_state_events,
    cancel_task,
    load_task_state_events,
)
from tce_shared.task_state import (
    TaskStateEvent,
    TaskStateEventKind,
    TaskStatePreconditionFailed,
    TaskStateProjection,
    TaskStateRevisionConflict,
)

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
_TASK_ID = "session-abc"
_WORKSPACE = "ws-1"
_OWNER = "owner-1"


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]], rowcount: int = 0) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def mappings(self) -> _FakeResult:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def scalar(self) -> Any:
        if not self._rows:
            return None
        return next(iter(self._rows[0].values()))

    def scalar_one(self) -> Any:
        return self.scalar()


class _FakeSavepoint:
    def __init__(self, transcript: list[str], params: list[dict[str, Any]]) -> None:
        self._transcript = transcript
        self._params = params

    def commit(self) -> None:
        self._transcript.append("SAVEPOINT RELEASE")
        self._params.append({})

    def rollback(self) -> None:
        self._transcript.append("SAVEPOINT ROLLBACK")
        self._params.append({})


class _FakeSession:
    """Records every statement it is handed and answers by SQL shape, not by position.

    Position-based scripting breaks the moment the store issues one more statement than the
    fixture anticipated, which makes the test assert the fixture rather than the store. This
    dispatches on the statement itself and keeps a real, mutating revision.
    """

    def __init__(
        self,
        *,
        revision: int = 0,
        cas_rowcount: int = 1,
        script: list[Any] | None = None,
    ) -> None:
        self.executed: list[str] = []
        self.params: list[dict[str, Any]] = []
        self.info: dict[str, Any] = {}
        self.revision = revision
        self.highest_seq = revision * 2
        self._cas_rowcount = cas_rowcount
        self._script = list(script or [])
        self.commits = 0
        self.rollbacks = 0
        self.state_row_id = str(uuid.uuid4())

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = " ".join(str(statement).split())
        self.executed.append(sql)
        self.params.append(dict(params or {}))
        if self._script:
            nxt = self._script.pop(0)
            scripted: _FakeResult = nxt(sql, params or {}) if callable(nxt) else nxt
            return scripted
        if "FOR UPDATE" in sql and "FROM task_states" in sql:
            if self.revision < 0:
                return _FakeResult([])
            return _FakeResult(
                [
                    {
                        "id": self.state_row_id,
                        "revision": self.revision,
                        "highest_seq": self.highest_seq,
                    }
                ]
            )
        if sql.startswith("UPDATE task_states"):
            if self._cas_rowcount == 1:
                self.revision = int(params["next_revision"]) if params else self.revision + 1
                self.highest_seq = int(params["highest_seq"]) if params else self.highest_seq
            return _FakeResult([], rowcount=self._cas_rowcount)
        if sql.startswith("UPDATE"):
            return _FakeResult([], rowcount=1)
        return _FakeResult([])

    def begin_nested(self) -> _FakeSavepoint:
        self.executed.append("SAVEPOINT")
        self.params.append({})
        return _FakeSavepoint(self.executed, self.params)

    def commit(self) -> None:  # pragma: no cover - must never be called
        self.commits += 1

    def rollback(self) -> None:  # pragma: no cover - must never be called
        self.rollbacks += 1


def _objective_event(seq: int = 0) -> TaskStateEvent:
    return TaskStateEvent(
        seq=seq,
        kind=TaskStateEventKind.OBJECTIVE_SET,
        contract_revision=1,
        payload={"objective_text": "ship the thing", "objective_hash": "h1"},
        occurred_at=_NOW,
        actor="tester",
    )


def _apply(session: _FakeSession, **kwargs: Any) -> Any:
    return apply_task_state_events(
        session,  # type: ignore[arg-type]
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        session_id=_TASK_ID,
        task_id=_TASK_ID,
        new_events=[_objective_event()],
        now=_NOW,
        **kwargs,
    )


def test_store_never_commits() -> None:
    session = _FakeSession(revision=3)
    _apply(session)
    assert session.commits == 0
    assert session.rollbacks == 0


def test_savepoint_not_session_rollback() -> None:
    """A conflict must roll back the store's OWN savepoint, never the caller's transaction."""
    session = _FakeSession(revision=3)
    with pytest.raises(TaskStateRevisionConflict):
        _apply(session, expected_revision=99)
    assert "SAVEPOINT ROLLBACK" in session.executed
    assert session.rollbacks == 0


def test_events_are_inserted_before_the_cas_update() -> None:
    session = _FakeSession(revision=2)
    _apply(session)
    insert_at = next(
        i for i, sql in enumerate(session.executed) if "INSERT INTO task_state_events" in sql
    )
    update_at = next(
        i for i, sql in enumerate(session.executed) if sql.startswith("UPDATE task_states")
    )
    assert insert_at < update_at


def test_expected_revision_none_uses_the_locked_read() -> None:
    """Two writes in one turn: the second's CAS binds what the first left, not the turn's
    first read. Pinning the revision at the turn level made the second write conflict on
    every first activation."""
    session = _FakeSession(revision=4)
    _apply(session)
    _apply(session)
    cas_params = [
        params
        for sql, params in zip(session.executed, session.params, strict=True)
        if sql.startswith("UPDATE task_states")
    ]
    assert [p["locked_revision"] for p in cas_params] == [4, 5]
    assert [p["next_revision"] for p in cas_params] == [5, 6]


def test_cas_conflict_and_revalidated_retry() -> None:
    # (i) caller precondition disagrees with the locked revision, no retry: raise, no UPDATE.
    session = _FakeSession(revision=7)
    with pytest.raises(TaskStateRevisionConflict) as excinfo:
        _apply(session, expected_revision=3)
    assert excinfo.value.expected_revision == 3
    assert excinfo.value.actual_revision == 7
    assert not any(sql.startswith("UPDATE task_states") for sql in session.executed)

    # (ii) retry allowed, but revalidate says the write is stale: nothing is applied.
    def _stale(_fresh: TaskStateProjection) -> tuple[bool, str]:
        return (False, "stale")

    session = _FakeSession(revision=7, script=[_lock(7), _projection_row(7)])
    with pytest.raises(TaskStatePreconditionFailed) as precondition:
        _apply(session, expected_revision=3, retry_once=True, revalidate=_stale)
    assert precondition.value.reason == "stale"
    assert not any(sql.startswith("UPDATE task_states") for sql in session.executed)

    # (iii) the concurrently-deleted-row path: rowcount 0 under FOR UPDATE.
    session = _FakeSession(revision=7, cas_rowcount=0)
    with pytest.raises(TaskStateRevisionConflict):
        _apply(session)
    assert session.executed.count("SAVEPOINT ROLLBACK") == 1


def test_the_retry_re_read_holds_the_row_lock() -> None:
    """RULING V2: the retry's re-read must take lock 1, and hold it through the recursive CAS.

    Rolling back to the savepoint released every row lock the aborted subtransaction held, so
    an UNLOCKED re-read leaves a window between "revalidate said yes" and the recursion's own
    CAS — and that recursion runs with ``expected_revision=None``, i.e. with no precondition of
    its own. An objective committed inside the window would never be re-checked: the write
    would land at the superseded contract revision and author orphan ``autonomy_goals`` rows.

    The assertion is made at the moment ``revalidate`` is called, because that is the instant
    the decision is taken: the statement that produced the projection it is judging must be a
    locking read, not a snapshot.
    """
    seen: dict[str, Any] = {}

    def _revalidate(_fresh: TaskStateProjection) -> tuple[bool, str]:
        seen["sql"] = session.executed[-1]
        return (True, "ok")

    session = _FakeSession(revision=7, script=[_lock(7), _projection_row(9)])
    _apply(session, expected_revision=3, retry_once=True, revalidate=_revalidate)
    assert "FROM task_states" in seen["sql"]
    assert "FOR UPDATE" in seen["sql"], (
        "an unlocked re-read reopens the very race the revalidation exists to close"
    )
    # And the retry really did complete a CAS after that locked read.
    assert any(sql.startswith("UPDATE task_states") for sql in session.executed)


def test_the_retry_carries_revalidate_into_the_recursion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recursion inherits the caller's precondition rather than dropping it.

    With the lock held the recursive CAS cannot lose, so this callback cannot fire today — but
    a future caller reaching this path with ``retry_once=True`` must not silently inherit an
    unvalidated write. Asserted on the kwargs actually handed to the recursion.
    """
    from tce_api import task_state_store

    captured: dict[str, Any] = {}
    sentinel = object()

    def _recorder(_db: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(task_state_store, "apply_task_state_events", _recorder)

    def _revalidate(_fresh: TaskStateProjection) -> tuple[bool, str]:
        return (True, "ok")

    session = _FakeSession(revision=5, script=[_projection_row(5)])
    result = task_state_store._retry_after_conflict(
        session,  # type: ignore[arg-type]
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        session_id=_TASK_ID,
        task_id=_TASK_ID,
        new_events=[_objective_event()],
        now=_NOW,
        invalidation=None,
        apply_side_effects=None,
        revalidate=_revalidate,
    )
    assert result is sentinel
    assert "FOR UPDATE" in session.executed[0]
    assert captured["revalidate"] is _revalidate
    assert captured["expected_revision"] is None
    assert captured["retry_once"] is False


def test_an_objective_committed_in_the_retry_window_is_still_rejected() -> None:
    """The whole point of holding the lock: the precondition is binding, not advisory."""

    def _changed_again(fresh: TaskStateProjection) -> tuple[bool, str]:
        assert fresh.objective_hash == "h1"
        return (False, "objective_changed_again")

    session = _FakeSession(revision=7, script=[_lock(7), _projection_row(9)])
    with pytest.raises(TaskStatePreconditionFailed) as precondition:
        _apply(session, expected_revision=3, retry_once=True, revalidate=_changed_again)
    assert precondition.value.reason == "objective_changed_again"
    assert not any(sql.startswith("UPDATE task_states") for sql in session.executed), (
        "nothing may be written once the revalidation says the write is stale"
    )


def test_missing_row_is_a_conflict_not_a_crash() -> None:
    session = _FakeSession(revision=-1)
    with pytest.raises(TaskStateRevisionConflict) as excinfo:
        _apply(session)
    assert excinfo.value.actual_revision == -1


def test_load_events_pins_the_same_window_the_fold_uses() -> None:
    session = _FakeSession(revision=1)
    load_task_state_events(session, task_state_id=str(uuid.uuid4()))  # type: ignore[arg-type]
    sql = session.executed[0]
    assert "UNION ALL" in sql, "UNION needs an equality operator per column; UNION ALL does not"
    assert "DISTINCT ON (kind)" in sql
    # The tail is the constant max_events - len(PINNED_KINDS): SQL cannot know how many pinned
    # events exist without a second round trip, so a constant is the only formula both the SQL
    # and the fold can evaluate identically.
    assert session.params[0]["tail"] == 2000 - 3
    assert session.params[0]["pinned"] == sorted(session.params[0]["pinned"])


def test_cancel_task_emits_fenced_updates() -> None:
    directive_rows: list[dict[str, Any]] = [
        {
            "directive_id": str(uuid.uuid4()),
            "state": "pending",
            "claimed_by": None,
            "claimed_executor": None,
            "lease_generation": 0,
        },
        {
            "directive_id": str(uuid.uuid4()),
            "state": "in_progress",
            "claimed_by": "codex",
            "claimed_executor": "codex",
            "lease_generation": 2,
        },
    ]
    session = _FakeSession(
        script=[
            _FakeResult([{"id": str(uuid.uuid4())}]),  # 1. task_states FOR UPDATE
            _FakeResult([]),  # 2. planning_jobs cancel (no rows)
            _FakeResult(directive_rows),  # 3. directive SELECT
            _FakeResult([], rowcount=1),  # 3a. fenced UPDATE
            _FakeResult([], rowcount=1),  # 3b. fenced UPDATE
            _FakeResult([], rowcount=1),  # 4. permits expiry
        ],
        revision=5,
    )
    response = cancel_task(
        session,  # type: ignore[arg-type]
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        session_id=_TASK_ID,
        task_id=_TASK_ID,
        reason="owner_cancelled",
        actor="tester",
        now=_NOW,
    )
    assert response.cancelled_directives == 2, "the count must be real, not merely 'SQL issued'"

    directive_select = next(
        sql for sql in session.executed if "FROM directive_executions" in sql
    )
    # Keyed on session_id, never on a task column: session_id is the column the mint path
    # actually populates, so a task-keyed predicate would always return zero rows.
    assert "session_id = :session_id" in directive_select
    assert "task_id" not in directive_select

    order = [_table_of(sql) for sql in session.executed if _table_of(sql)]
    assert order.index("task_states") < order.index("planning_jobs")
    assert order.index("planning_jobs") < order.index("directive_executions")
    assert order.index("directive_executions") < order.index("execution_permits")

    permit_update = next(sql for sql in session.executed if "UPDATE execution_permits" in sql)
    assert "session_id = :session_id" in permit_update


def _lock(revision: int) -> _FakeResult:
    return _FakeResult(
        [{"id": str(uuid.uuid4()), "revision": revision, "highest_seq": revision * 2}]
    )


def _projection_row(revision: int) -> _FakeResult:
    return _FakeResult(
        [
            {
                "id": str(uuid.uuid4()),
                "workspace_id": _WORKSPACE,
                "owner_id": _OWNER,
                "subject_user_id": _OWNER,
                "session_id": _TASK_ID,
                "task_id": _TASK_ID,
                "project_id": None,
                "revision": revision,
                "contract_revision": 1,
                "highest_seq": 4,
                "objective_text": "ship the thing",
                "objective_hash": "h1",
                "objective_set_at": _NOW,
                "objective_set_seq": 1,
                "status": "planning",
                "next_permitted_action": "await_planning",
                "plan_state": "absent",
                "plan_producer": None,
                "plan_root_goal_id": None,
                "planning_job_id": None,
                "constraints_json": "[]",
                "open_decisions_json": "[]",
                "plan_json": None,
                "unresolved_effects_json": "[]",
                "latest_verification_json": None,
                "citations_json": "[]",
                "source_revision": "abc",
                "cancelled_at": None,
                "cancelled_seq": 0,
                "last_cancel_seq": 0,
                "cancel_reason": "",
                "schema_version": "v1",
            }
        ]
    )


def _table_of(sql: str) -> str | None:
    for table in (
        "task_states",
        "planning_jobs",
        "directive_executions",
        "execution_permits",
        "autonomy_goals",
    ):
        if table in sql and "task_state_events" not in sql:
            return table
    return None
