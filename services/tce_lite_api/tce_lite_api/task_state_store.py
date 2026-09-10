"""Durable task state for Lite (P2 §5.2/§5.3, §0.7 S12.2/S12.3).

The Lite twin of `tce_api.task_state_store`. Same six functions, same CAS semantics, same
exceptions — with `conn: sqlite3.Connection`, `?` params, and `cursor.rowcount` in place of
`RETURNING`.

Two things are specific to SQLite and are the whole reason this file exists rather than a shared
one:

* **`BEGIN IMMEDIATE` is issued in exactly one place**, `begin_immediate_cas`. Python's `sqlite3`
  opens DEFERRED transactions, so a read-then-write upgrade on a stale WAL snapshot fails with
  `SQLITE_BUSY_SNAPSHOT`, which surfaces as the string `'database is locked'` — a token that IS in
  `store._RETRYABLE_DB_TOKENS`, so `_run_sql_retry` would spin on a condition that can never clear
  inside the open transaction.
* **The bracket covers ONLY the read-then-write CAS section.** Retrieval, classification and the
  advisor decision run outside it. With the whole turn inside the write lock a second concurrent
  turn waits up to `busy_timeout=5000` ms — longer than `takeover_turn_budget_ms=3500` — and the
  retry then re-runs retrieval, costing twice the budget.

Nothing here commits except `end_immediate_cas`, which is the CAS section's own boundary.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from tce_shared.events import TaskCancelResponse
from tce_shared.execution_transitions import SYSTEM_ACTOR, DirectiveState, validate_transition
from tce_shared.task_state import (
    CANCEL_REASON_TASK_CANCELLED,
    MAX_TASK_STATE_EVENTS_PER_FOLD,
    PINNED_KIND_VALUES,
    PINNED_KINDS,
    TASK_STATE_POLICY_REVISION,
    TASK_STATE_SCHEMA_VERSION,
    UNKNOWN_CONTRACT_REVISION,
    FoldResult,
    InvalidationPlan,
    NextPermittedAction,
    PlanState,
    TaskStateEvent,
    TaskStateEventKind,
    TaskStatePreconditionFailed,
    TaskStateProjection,
    TaskStateRevisionConflict,
    TaskStateWrite,
    TaskStatus,
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

ApplySideEffectsLite = Callable[[sqlite3.Connection, InvalidationPlan], None]
Revalidate = Callable[[TaskStateProjection], tuple[bool, str]]

_RETRYABLE_CAS_TOKENS = ("database is locked", "database table is locked", "busy")


# --------------------------------------------------------------------------------------
# §5.2 — the BEGIN IMMEDIATE bracket
# --------------------------------------------------------------------------------------


def begin_immediate_cas(conn: sqlite3.Connection) -> None:
    """Open the write-locked CAS section. THE ONLY place Lite issues ``BEGIN IMMEDIATE``.

    The ``conn.commit()`` is what makes the rule satisfiable. ``_connect()`` leaves
    ``isolation_level = ''`` (legacy implicit-BEGIN), so the turn's earlier bookkeeping DML has
    already opened a transaction and a bare ``BEGIN IMMEDIATE`` would raise *cannot start a
    transaction within a transaction* — a token that is NOT retryable and would surface as a 500.
    The earlier DML is independently correct and does not need to be atomic with the CAS, so
    flushing it is the right resolution rather than widening the lock.
    """
    conn.commit()
    assert not conn.in_transaction, "begin_immediate_cas requires no open transaction"
    conn.execute("BEGIN IMMEDIATE")


def end_immediate_cas(conn: sqlite3.Connection, *, ok: bool) -> None:
    """``commit()`` when ``ok`` else ``rollback()``. ALWAYS called, from a ``finally``."""
    if ok:
        conn.commit()
    else:
        conn.rollback()


def _is_retryable_cas_error(exc: BaseException) -> bool:
    message = str(exc).strip().lower()
    return any(token in message for token in _RETRYABLE_CAS_TOKENS)


def run_cas_section[T](conn: sqlite3.Connection, fn: Callable[[], T], *, settings: Any = None) -> T:
    """Run ``fn()`` inside ``begin_immediate_cas`` / ``end_immediate_cas``.

    Retries the WHOLE section at most once on a retryable ``sqlite3.OperationalError``: SQLite
    cannot retry a single statement inside a doomed transaction, so the unit of retry is the
    section. Retrieval and the advisor are outside it and are therefore never re-run.
    """
    del settings  # kept for signature parity with the design; no knob is read
    attempts = 0
    while True:
        attempts += 1
        begin_immediate_cas(conn)
        ok = False
        try:
            result = fn()
            ok = True
        except sqlite3.OperationalError as exc:
            if attempts <= 1 and _is_retryable_cas_error(exc):
                end_immediate_cas(conn, ok=False)
                continue
            end_immediate_cas(conn, ok=False)
            raise
        except BaseException:
            end_immediate_cas(conn, ok=False)
            raise
        else:
            end_immediate_cas(conn, ok=ok)
            return result


# --------------------------------------------------------------------------------------
# Row <-> projection
# --------------------------------------------------------------------------------------


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _json(value: Any) -> Any:
    import json

    if value in (None, ""):
        return None
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return None


def _projection_from_row(row: sqlite3.Row) -> TaskStateProjection:
    """Rebuild the projection from the stored ``*_json`` columns. Total: a malformed column falls
    back to its dataclass default rather than making the task unreadable."""
    plan = approved_plan_from_json(_json(row["plan_json"]))
    citations_raw = _json(row["citations_json"]) or []
    citations = tuple(str(item) for item in citations_raw) if isinstance(citations_raw, list) else ()
    try:
        status = TaskStatus(str(row["status"]))
    except ValueError:
        status = TaskStatus.AWAITING_VERIFICATION
    try:
        action = NextPermittedAction(str(row["next_permitted_action"]))
    except ValueError:
        action = NextPermittedAction.NONE
    return TaskStateProjection(
        task_id=str(row["task_id"]),
        workspace_id=str(row["workspace_id"]),
        owner_id=str(row["owner_id"]),
        session_id=str(row["session_id"] or ""),
        revision=int(row["revision"] or 0),
        contract_revision=int(row["contract_revision"] or 0),
        objective_text=str(row["objective_text"] or ""),
        objective_hash=(str(row["objective_hash"]) if row["objective_hash"] else None),
        objective_set_at=_parse_dt(row["objective_set_at"]),
        objective_set_seq=int(row["objective_set_seq"] or 0),
        status=status,
        next_permitted_action=action,
        constraints=constraints_from_json(_json(row["constraints_json"])),
        open_decisions=decisions_from_json(_json(row["open_decisions_json"])),
        plan=plan,
        unresolved_effects=effects_from_json(_json(row["unresolved_effects_json"])),
        latest_verification=verification_from_json(_json(row["latest_verification_json"])),
        citations=citations,
        cancelled_at=_parse_dt(row["cancelled_at"]),
        cancelled_seq=int(row["cancelled_seq"] or 0),
        last_cancel_seq=int(row["last_cancel_seq"] or 0),
        cancel_reason=str(row["cancel_reason"] or ""),
        schema_version=str(row["schema_version"] or TASK_STATE_SCHEMA_VERSION),
        policy_revision=TASK_STATE_POLICY_REVISION,
    )


def _event_from_row(row: sqlite3.Row) -> TaskStateEvent:
    raw_kind = str(row["kind"])
    kind: TaskStateEventKind | str
    try:
        kind = TaskStateEventKind(raw_kind)
    except ValueError:
        kind = raw_kind
    return TaskStateEvent(
        seq=int(row["seq"]),
        kind=kind,
        contract_revision=int(row["contract_revision"] or 0),
        payload=event_payload_from_json(_json(row["payload_json"]) or {}),
        occurred_at=_parse_dt(row["occurred_at"]) or datetime.now(UTC),
        actor=str(row["actor"] or ""),
        source_event_id=(str(row["source_event_id"]) if row["source_event_id"] else None),
        directive_id=(str(row["directive_id"]) if row["directive_id"] else None),
        goal_id=(str(row["goal_id"]) if row["goal_id"] else None),
    )


# --------------------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------------------


def load_task_state(
    conn: sqlite3.Connection, *, workspace_id: str, owner_id: str, task_id: str
) -> tuple[TaskStateProjection, int, str] | None:
    """``(projection, highest_seq, source_revision)`` or ``None``.

    No ``for_update`` parameter: SQLite has no row locks. ``begin_immediate_cas`` plus
    ``WHERE revision = ?`` is the whole guard (§5.2 rule 4).
    """
    row = conn.execute(
        "SELECT * FROM task_states WHERE workspace_id = ? AND owner_id = ? AND task_id = ?",
        (workspace_id, owner_id, task_id),
    ).fetchone()
    if row is None:
        return None
    return _projection_from_row(row), int(row["highest_seq"] or 0), str(row["source_revision"] or "")


def load_task_state_events(
    conn: sqlite3.Connection,
    *,
    task_state_id: str,
    max_events: int = MAX_TASK_STATE_EVENTS_PER_FOLD,
) -> list[TaskStateEvent]:
    """R12: the SAME pinning window as fold rule 2, in SQL.

    ``UNION ALL``, not ``UNION`` — fold rule 1 already dedups on
    ``(seq, kind, payload_digest)``, and ``UNION`` would additionally force a comparison on the
    payload column. The tail is ``max_events - len(PINNED_KINDS)``, the constant 3, whether or not
    all three pinned kinds are present: the SQL cannot know how many exist without a second round
    trip, and a constant is the only formula the fold and the window can both evaluate identically.

    Served by ``idx_task_state_events_kind (task_state_id, kind, seq DESC)``.
    """
    tail = max(0, int(max_events) - len(PINNED_KINDS))
    pinned = list(PINNED_KIND_VALUES)
    placeholders = ", ".join("?" for _ in pinned)
    rows = conn.execute(
        f"""
        SELECT * FROM task_state_events
         WHERE task_state_id = ? AND kind IN ({placeholders})
         GROUP BY kind HAVING seq = MAX(seq)
        UNION ALL
        SELECT * FROM (SELECT * FROM task_state_events WHERE task_state_id = ?
                        ORDER BY seq DESC LIMIT ?)
        """,
        (task_state_id, *pinned, task_state_id, tail),
    ).fetchall()
    return [_event_from_row(row) for row in rows]


def rebuild_task_state(
    conn: sqlite3.Connection, *, workspace_id: str, owner_id: str, task_id: str
) -> FoldResult | None:
    """SELECT the row, SELECT its events, fold. The deterministic reconstruction path."""
    row = conn.execute(
        "SELECT * FROM task_states WHERE workspace_id = ? AND owner_id = ? AND task_id = ?",
        (workspace_id, owner_id, task_id),
    ).fetchone()
    if row is None:
        return None
    events = load_task_state_events(conn, task_state_id=str(row["id"]))
    return fold_task_state(
        events,
        task_id=str(row["task_id"]),
        workspace_id=str(row["workspace_id"]),
        owner_id=str(row["owner_id"]),
        session_id=str(row["session_id"] or ""),
        revision=int(row["revision"] or 0),
        now=datetime.now(UTC),
    )


def ensure_task_state(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    subject_user_id: str,
    session_id: str,
    task_id: str,
    project_id: str | None,
    now: datetime,
) -> tuple[str, TaskStateProjection, int, str]:
    """``INSERT OR IGNORE`` then ``SELECT``. Returns
    ``(task_state_id, projection, highest_seq, source_revision)``. NEVER bumps ``revision``."""
    stamp = now.isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO task_states
            (id, workspace_id, owner_id, subject_user_id, session_id, task_id, project_id,
             revision, contract_revision, highest_seq, created_at, updated_at, schema_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            workspace_id,
            owner_id,
            subject_user_id,
            session_id,
            task_id,
            project_id,
            stamp,
            stamp,
            TASK_STATE_SCHEMA_VERSION,
        ),
    )
    row = conn.execute(
        "SELECT * FROM task_states WHERE workspace_id = ? AND owner_id = ? AND task_id = ?",
        (workspace_id, owner_id, task_id),
    ).fetchone()
    if row is None:  # pragma: no cover - the INSERT above guarantees a row
        raise TaskStateRevisionConflict(task_id=task_id, expected_revision=0, actual_revision=-1)
    return (
        str(row["id"]),
        _projection_from_row(row),
        int(row["highest_seq"] or 0),
        str(row["source_revision"] or ""),
    )


# --------------------------------------------------------------------------------------
# The CAS write
# --------------------------------------------------------------------------------------

_EVENT_COLUMNS = (
    "id",
    "task_state_id",
    "workspace_id",
    "owner_id",
    "task_id",
    "seq",
    "kind",
    "contract_revision",
    "payload_json",
    "source_event_id",
    "directive_id",
    "goal_id",
    "actor",
    "occurred_at",
    "schema_version",
)


def apply_task_state_events(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    task_id: str,
    new_events: Sequence[TaskStateEvent],
    now: datetime,
    expected_revision: int | None = None,
    invalidation: InvalidationPlan | None = None,
    apply_side_effects: ApplySideEffectsLite | None = None,
    revalidate: Revalidate | None = None,
    retry_once: bool = False,
) -> TaskStateWrite:
    """CAS write.

    S2 — ``expected_revision`` semantics: the revision the CAS compares against is ALWAYS the one
    read by THIS call, inside this transaction. ``None`` (the default, and what both in-turn call
    sites pass) means "no caller precondition"; an ``int`` is a caller precondition for a snapshot
    taken outside this transaction, and a mismatch raises ``TaskStateRevisionConflict`` WITHOUT
    issuing the UPDATE.

    Because Lite has no row locks, ``rowcount == 0`` is genuinely reachable across connections:
    that is the primary conflict case in the whole design, and ``retry_once`` defaults to ``False``
    exactly as in Full.
    """
    row = conn.execute(
        "SELECT * FROM task_states WHERE workspace_id = ? AND owner_id = ? AND task_id = ?",
        (workspace_id, owner_id, task_id),
    ).fetchone()
    if row is None:
        raise TaskStateRevisionConflict(
            task_id=task_id,
            expected_revision=expected_revision or 0,
            actual_revision=-1,
        )
    task_state_id = str(row["id"])
    locked_revision = int(row["revision"] or 0)
    highest_seq = int(row["highest_seq"] or 0)

    if expected_revision is not None and expected_revision != locked_revision:
        if not retry_once:
            raise TaskStateRevisionConflict(
                task_id=task_id,
                expected_revision=expected_revision,
                actual_revision=locked_revision,
            )
        return _revalidated_retry(
            conn,
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

    prior_events = load_task_state_events(conn, task_state_id=task_state_id)

    # Full gets this for free from ``db.begin_nested()``; Lite has to name the savepoint itself.
    # Without it a ``retry_once`` caller carries the failed attempt's event INSERTs into the
    # retry and dies on ``UNIQUE constraint failed: task_state_events.task_state_id, seq``,
    # with ``apply_side_effects`` applied twice on top. Taken ONLY for ``retry_once=True`` so
    # the production shape (``retry_once=False``, which is what every Lite caller passes) issues
    # exactly the statements it issued before.
    savepoint = f"tce_cas_{uuid.uuid4().hex}" if retry_once else None
    if savepoint is not None:
        conn.execute(f"SAVEPOINT {savepoint}")

    if invalidation is not None and apply_side_effects is not None:
        apply_side_effects(conn, invalidation)

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
    _insert_events(
        conn,
        task_state_id=task_state_id,
        workspace_id=workspace_id,
        owner_id=owner_id,
        task_id=task_id,
        events=write.events,
    )
    if not _cas_update(
        conn, task_state_id=task_state_id, write=write, prior_highest_seq=highest_seq, now=now
    ):
        if savepoint is not None:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        if not retry_once:
            raise TaskStateRevisionConflict(
                task_id=task_id,
                expected_revision=locked_revision,
                actual_revision=_current_revision(conn, task_state_id=task_state_id),
            )
        return _revalidated_retry(
            conn,
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
    if savepoint is not None:
        conn.execute(f"RELEASE {savepoint}")
    return write


def _revalidated_retry(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    task_id: str,
    new_events: Sequence[TaskStateEvent],
    now: datetime,
    invalidation: InvalidationPlan | None,
    apply_side_effects: ApplySideEffectsLite | None,
    revalidate: Revalidate | None,
) -> TaskStateWrite:
    """Re-read, ask ``revalidate`` whether the write is still valid, then recurse exactly once."""
    fresh = load_task_state(conn, workspace_id=workspace_id, owner_id=owner_id, task_id=task_id)
    if fresh is None:
        raise TaskStatePreconditionFailed(task_id=task_id, reason="task_state_missing")
    if revalidate is not None:
        allowed, reason = revalidate(fresh[0])
        if not allowed:
            raise TaskStatePreconditionFailed(task_id=task_id, reason=reason)
    return apply_task_state_events(
        conn,
        workspace_id=workspace_id,
        owner_id=owner_id,
        session_id=session_id,
        task_id=task_id,
        new_events=new_events,
        now=now,
        expected_revision=None,
        invalidation=invalidation,
        apply_side_effects=apply_side_effects,
        revalidate=None,
        retry_once=False,
    )


def _current_revision(conn: sqlite3.Connection, *, task_state_id: str) -> int:
    row = conn.execute("SELECT revision FROM task_states WHERE id = ?", (task_state_id,)).fetchone()
    return int(row["revision"] or 0) if row is not None else -1


def _insert_events(
    conn: sqlite3.Connection,
    *,
    task_state_id: str,
    workspace_id: str,
    owner_id: str,
    task_id: str,
    events: Sequence[TaskStateEvent],
) -> None:
    statement = "INSERT INTO task_state_events ({cols}) VALUES ({marks})".format(
        cols=", ".join(_EVENT_COLUMNS),
        marks=", ".join("?" for _ in _EVENT_COLUMNS),
    )
    for event in events:
        conn.execute(
            statement,
            (
                str(uuid.uuid4()),
                task_state_id,
                workspace_id,
                owner_id,
                task_id,
                int(event.seq),
                str(event.kind),
                int(event.contract_revision),
                canonical_json(event_payload_to_json(event.payload)),
                event.source_event_id,
                event.directive_id,
                event.goal_id,
                str(event.actor or ""),
                event.occurred_at.astimezone(UTC).isoformat(),
                TASK_STATE_SCHEMA_VERSION,
            ),
        )


def _cas_update(
    conn: sqlite3.Connection,
    *,
    task_state_id: str,
    write: TaskStateWrite,
    prior_highest_seq: int,
    now: datetime,
) -> bool:
    projection = write.projection
    plan = projection.plan
    next_highest_seq = max([prior_highest_seq, *(int(e.seq) for e in write.events)])
    cursor = conn.execute(
        """
        UPDATE task_states
           SET revision = ?, contract_revision = ?, highest_seq = ?,
               objective_text = ?, objective_hash = ?, objective_set_at = ?, objective_set_seq = ?,
               status = ?, next_permitted_action = ?,
               plan_state = ?, plan_producer = ?, plan_root_goal_id = ?,
               constraints_json = ?, open_decisions_json = ?, plan_json = ?,
               unresolved_effects_json = ?, latest_verification_json = ?, citations_json = ?,
               source_revision = ?,
               cancelled_at = ?, cancelled_seq = ?, last_cancel_seq = ?, cancel_reason = ?,
               updated_at = ?
         WHERE id = ? AND revision = ?
        """,
        (
            int(write.next_revision),
            int(projection.contract_revision),
            int(next_highest_seq),
            projection.objective_text,
            projection.objective_hash,
            projection.objective_set_at.astimezone(UTC).isoformat() if projection.objective_set_at else None,
            int(projection.objective_set_seq),
            str(projection.status),
            str(projection.next_permitted_action),
            str(plan.state) if plan is not None else PlanState.ABSENT.value,
            plan.producer if plan is not None else None,
            plan.root_goal_id if plan is not None else None,
            canonical_json(constraints_to_json(projection.constraints)),
            canonical_json(decisions_to_json(projection.open_decisions)),
            canonical_json(approved_plan_to_json(plan)),
            canonical_json(effects_to_json(projection.unresolved_effects)),
            canonical_json(verification_to_json(projection.latest_verification)),
            canonical_json(list(projection.citations)),
            write.source_revision,
            projection.cancelled_at.astimezone(UTC).isoformat() if projection.cancelled_at else None,
            int(projection.cancelled_seq),
            int(projection.last_cancel_seq),
            projection.cancel_reason,
            now.isoformat(),
            task_state_id,
            int(write.expected_revision),
        ),
    )
    return bool(cursor.rowcount == 1)


# --------------------------------------------------------------------------------------
# task_verifications
# --------------------------------------------------------------------------------------


def directive_contract_revision(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    task_id: str,
    directive_id: str,
) -> int:
    """The contract revision the DIRECTIVE's work happened under -- the twin of
    ``tce_api.task_state_store.directive_contract_revision``, and the same ruling.

    ``task_state_events`` stamps every row with the ``contract_revision`` in force when it was
    appended and with the ``directive_id`` that caused it, so the earliest such row for a directive
    is the contract its work was done for.  A verification's provenance has to be read from THAT,
    not from the projection at grading time: the projection's current revision is true of every
    verification whenever it is asked, so a stamp taken from it excludes nothing.

    A directive with no rows -- cancelled by an objective-change invalidation before it touched the
    projection, or reported while task state was off -- returns :data:`UNKNOWN_CONTRACT_REVISION`,
    which no real revision equals.
    """
    row = conn.execute(
        """
        SELECT MIN(e.contract_revision) AS contract_revision
        FROM task_state_events e
        JOIN task_states t ON t.id = e.task_state_id
        WHERE t.workspace_id = ?
          AND t.owner_id = ?
          AND t.task_id = ?
          AND e.directive_id = ?
        """,
        (workspace_id, owner_id, task_id, str(directive_id)),
    ).fetchone()
    if row is None or row["contract_revision"] is None:
        return UNKNOWN_CONTRACT_REVISION
    return int(row["contract_revision"])


def insert_task_verification(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    task_id: str,
    recorded_by: str,
    ref: Mapping[str, Any],
    now: datetime,
) -> None:
    """The ONE ``task_verifications`` INSERT in Lite, shared by both producers of a
    ``VERIFICATION_RECORDED`` event.

    Two call sites write this table -- ``store._record_execution_task_state_lite`` (the report
    path, whose ``state`` is pinned to ``'unverified'``) and
    ``verification_store.record_verification`` (the evidence-graded path, which carries the
    ``decide_verdict`` outcome). They must agree column-for-column, because the resume packet
    reads these rows as the durable history behind the projection's ``latest_verification``.
    Keeping the statement here rather than duplicating it is what makes that agreement structural.
    """
    conn.execute(
        """
        INSERT INTO task_verifications(
            id, workspace_id, owner_id, task_id, directive_id, state, method, summary,
            evidence_event_ids_json, recorded_by, recorded_at, schema_version,
            contract_revision, plan_id
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1', ?, ?)
        """,
        (
            str(uuid.uuid4()),
            workspace_id,
            owner_id,
            task_id,
            ref.get("directive_id"),
            ref.get("state"),
            ref.get("method"),
            ref.get("summary"),
            canonical_json(list(ref.get("evidence_event_ids") or [])),
            recorded_by,
            now.isoformat(),
            int(ref.get("contract_revision") or 0),
            ref.get("plan_id"),
        ),
    )


# --------------------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------------------


def cancel_task(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    task_id: str,
    reason: str,
    actor: str,
    now: datetime,
) -> TaskCancelResponse:
    """Cancel a task and everything it authorised, in §0.8 lock order.

    Step 3 keys on ``session_id``, never ``task_id``: under D4 they hold the same value, and
    ``directive_executions.session_id`` is the column that is actually populated. Keying on a
    column with no producer is what made ``cancelled_directives`` always 0 in v2.

    The whole body runs inside the ``BEGIN IMMEDIATE`` bracket via ``run_cas_section``.
    """

    def _body() -> TaskCancelResponse:
        stamp = now.isoformat()
        before = load_task_state(
            conn, workspace_id=workspace_id, owner_id=owner_id, task_id=task_id
        )
        if before is None:
            raise TaskStatePreconditionFailed(task_id=task_id, reason="task_state_missing")
        live_constraints = sum(1 for item in before[0].constraints if item.revoked_at is None)

        job_rows = conn.execute(
            """
            SELECT id, rq_job_id FROM planning_jobs
             WHERE workspace_id = ? AND task_id = ? AND state IN ('pending', 'leased')
            """,
            (workspace_id, task_id),
        ).fetchall()
        conn.execute(
            """
            UPDATE planning_jobs
               SET cancel_requested = 1, state = 'cancelled', updated_at = ?
             WHERE workspace_id = ? AND task_id = ? AND state IN ('pending', 'leased')
            """,
            (stamp, workspace_id, task_id),
        )

        directive_rows = conn.execute(
            """
            SELECT directive_id, state, claimed_executor, lease_generation
              FROM directive_executions
             WHERE workspace_id = ? AND user_id = ? AND session_id = ?
               AND state IN ('pending', 'in_progress')
            """,
            (workspace_id, owner_id, session_id),
        ).fetchall()
        cancelled_directives = 0
        for directive in directive_rows:
            decision = validate_transition(
                current_state=str(directive["state"]),
                claimed_by=directive["claimed_executor"],
                lease_generation=int(directive["lease_generation"] or 0),
                requested=DirectiveState.CANCELLED,
                actor=SYSTEM_ACTOR,
                actor_lease=None,
            )
            if not decision.allowed:
                continue
            cursor = conn.execute(
                """
                UPDATE directive_executions
                   SET state = ?, updated_at = ?, finished_at = ?, cancelled_at = ?,
                       cancel_reason = COALESCE(cancel_reason, ?)
                 WHERE directive_id = ? AND state = ? AND lease_generation = ?
                """,
                (
                    DirectiveState.CANCELLED.value,
                    stamp,
                    stamp,
                    stamp,
                    CANCEL_REASON_TASK_CANCELLED,
                    str(directive["directive_id"]),
                    str(directive["state"]),
                    int(directive["lease_generation"] or 0),
                ),
            )
            cancelled_directives += int(cursor.rowcount == 1)

        expired = conn.execute(
            """
            UPDATE execution_permits SET expires_at = ?
             WHERE workspace_id = ? AND session_id = ?
               AND (expires_at IS NULL OR expires_at > ?)
            """,
            (stamp, workspace_id, session_id, stamp),
        ).rowcount

        cancel_event = TaskStateEvent(
            seq=0,
            kind=TaskStateEventKind.CANCELLATION_REQUESTED,
            contract_revision=0,
            payload={"reason": reason, "actor": actor},
            occurred_at=now,
            actor=actor,
        )
        write = apply_task_state_events(
            conn,
            workspace_id=workspace_id,
            owner_id=owner_id,
            session_id=session_id,
            task_id=task_id,
            new_events=[cancel_event],
            now=now,
            expected_revision=None,
            retry_once=False,
        )
        return TaskCancelResponse(
            task_id=task_id,
            revision=int(write.next_revision),
            cancelled_planning_jobs=len(job_rows),
            cancelled_directives=int(cancelled_directives),
            revoked_constraints=max(
                0,
                live_constraints
                - sum(1 for item in write.projection.constraints if item.revoked_at is None),
            ),
            expired_permits=int(max(0, expired)),
            cancelled_rq_jobs=0,
        )

    return run_cas_section(conn, _body)


__all__ = [
    "ApplySideEffectsLite",
    "Revalidate",
    "apply_task_state_events",
    "begin_immediate_cas",
    "cancel_task",
    "directive_contract_revision",
    "end_immediate_cas",
    "ensure_task_state",
    "load_task_state",
    "load_task_state_events",
    "rebuild_task_state",
    "run_cas_section",
]
