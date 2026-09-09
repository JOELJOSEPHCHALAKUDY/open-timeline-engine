"""Postgres enforcement of a retrieval deadline: a guard pair, not a context manager.

A generator context manager cannot yield twice, and the timeout path and the success path need
genuinely different teardown, so this ships as ``begin`` / ``finish`` / ``absorb``.

Five mechanics this module depends on, each measured rather than assumed:

1. ``SET LOCAL`` requires a transaction, so a bounded read opens ``db.begin_nested()``. The
   savepoint's job is to survive the **abort**, not to contain the GUC.
2. **``RELEASE SAVEPOINT`` does NOT revert ``SET LOCAL``.** Both :func:`finish_pg_deadline` and
   :func:`absorb_pg_deadline` therefore issue an explicit restoring ``SET LOCAL``. Without it,
   every successful bounded read would leave a 10-20 ms timeout in force and kill the
   ``task_states`` CAS UPDATE, the event INSERTs and the audit write that share the transaction.
3. The restore interpolates the captured GUC string **quoted** (``= '3s'``, not ``= 3s``),
   because ``current_setting`` returns a unit-bearing string and a bare integer silently means
   milliseconds.
4. ``statement_timeout`` is a GUC, not a bindable parameter, so the value is ``int()``-cast and
   f-string interpolated. There is no user input in it.
5. **No ``db.rollback()`` anywhere in this module** — only ``savepoint.rollback()``. A full
   rollback would discard the whole turn.

**Writes are never guarded.** The guard is applied to read queries only; a skipped write would
report a revision that was never persisted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from tce_shared.deadline import (
    RETRIEVAL_REASON_DEADLINE_SKIP,
    Deadline,
    RetrievalLedger,
)

_PRIOR_TIMEOUT_KEY = "tce_prior_statement_timeout"

# A remaining budget at or above this is an unbounded turn: Deadline.unbounded() reports a
# budget no real turn can carry, so treating it as "install nothing" is exact, not heuristic.
UNBOUNDED_REMAINING_MS = 10**8

# SQLSTATE 57014 — query_canceled. This is what statement_timeout raises.
_QUERY_CANCELED_SQLSTATE = "57014"


@dataclass(slots=True)
class PgDeadlineGuard:
    name: str
    budget_ms: int
    savepoint: Any
    prior_timeout: str
    started: float
    ledger: RetrievalLedger | None
    unbounded: bool


def transaction_statement_timeout(db: Session) -> str:
    """Capture the transaction's prior ``statement_timeout`` ONCE, memoised on ``db.info``.

    Deliberately not ``setdefault``: Python evaluates ``setdefault``'s second argument eagerly,
    so that form issues the SELECT on every call while appearing to memoise. Fixing this
    removes one of the guard's round trips per guarded read.

    Returns a GUC string such as ``'0'`` or ``'3s'``; it is interpolated **quoted** on restore.
    """
    if _PRIOR_TIMEOUT_KEY not in db.info:
        db.info[_PRIOR_TIMEOUT_KEY] = str(
            db.execute(text("SELECT current_setting('statement_timeout')")).scalar_one()
        )
    return str(db.info[_PRIOR_TIMEOUT_KEY])


def begin_pg_deadline(
    db: Session,
    deadline: Deadline | None,
    *,
    name: str,
    ledger: RetrievalLedger | None,
    requested_ms: int,
    floor_ms: int,
) -> PgDeadlineGuard | None:
    """``None`` => the caller must SKIP the query; a ledger skip has already been recorded.

    Three cases:

    (a) ``deadline is None``, or the turn is unbounded: return an ``unbounded`` guard. No
        savepoint, no ``SET LOCAL``, no round trips. This is what makes
        ``retrieval_deadline_enabled=False`` genuinely free rather than a silent cap.
    (b) the ledger says this is the **first query of the turn**: same as (a). The first query
        is unguarded, so it cannot be aborted by the budget and ``record_executed`` always
        fires. A tight budget therefore degrades secondary queries, never the primary one.
    (c) otherwise: skip when the budget is under the floor, else open a savepoint, capture the
        prior timeout and install the slice.
    """
    if deadline is None or deadline.remaining_ms() >= UNBOUNDED_REMAINING_MS:
        return PgDeadlineGuard(
            name=name,
            budget_ms=0,
            savepoint=None,
            prior_timeout="",
            started=time.monotonic(),
            ledger=ledger,
            unbounded=True,
        )
    if ledger is not None and ledger.is_first_query():
        return PgDeadlineGuard(
            name=name,
            budget_ms=0,
            savepoint=None,
            prior_timeout="",
            started=time.monotonic(),
            ledger=ledger,
            unbounded=True,
        )
    if not deadline.allows(floor_ms):
        if ledger is not None:
            ledger.record_skip(
                name,
                remaining_ms=deadline.remaining_ms(),
                reason=RETRIEVAL_REASON_DEADLINE_SKIP,
            )
        return None

    budget_ms = int(max(1, deadline.slice_ms(requested_ms)))
    savepoint = db.begin_nested()
    prior = transaction_statement_timeout(db)
    db.execute(text(f"SET LOCAL statement_timeout = {budget_ms}"))
    return PgDeadlineGuard(
        name=name,
        budget_ms=budget_ms,
        savepoint=savepoint,
        prior_timeout=prior,
        started=time.monotonic(),
        ledger=ledger,
        unbounded=False,
    )


def finish_pg_deadline(guard: PgDeadlineGuard, db: Session, *, rows: int = 0) -> None:
    """Success path: RELEASE the savepoint, restore the prior timeout, record the execution.

    RELEASE does not revert ``SET LOCAL``, so the explicit restore is mandatory.
    """
    elapsed_ms = int((time.monotonic() - guard.started) * 1000)
    if not guard.unbounded:
        try:
            guard.savepoint.commit()
        finally:
            _restore_timeout(db, guard.prior_timeout)
    if guard.ledger is not None:
        guard.ledger.record_executed(guard.name, ms=elapsed_ms, rows=rows)


def absorb_pg_deadline(guard: PgDeadlineGuard, db: Session, exc: BaseException) -> bool:
    """Timeout path.

    ``True`` when ``exc`` is a query cancellation: the savepoint is rolled back FIRST, THEN the
    prior timeout is restored (the reverse order raises ``InFailedSqlTransaction``), a ledger
    timeout is recorded, and the caller continues with an empty result.

    ``False`` otherwise — the caller re-raises, and the prior timeout is restored on that path
    too. An unbounded guard always returns ``False``: it installed nothing, so nothing it did
    can have caused the exception.
    """
    if guard.unbounded:
        return False
    cancelled = _is_query_canceled(exc)
    try:
        guard.savepoint.rollback()
    finally:
        _restore_timeout(db, guard.prior_timeout)
    if not cancelled:
        return False
    if guard.ledger is not None:
        guard.ledger.record_timeout(guard.name, budget_ms=guard.budget_ms)
    return True


def _restore_timeout(db: Session, prior: str) -> None:
    """Quoted, because the GUC string carries a unit and a bare integer means milliseconds."""
    safe = str(prior or "0").replace("'", "")
    try:
        db.execute(text(f"SET LOCAL statement_timeout = '{safe}'"))
    except Exception:  # noqa: BLE001 - restoring must never mask the original failure
        return


def _is_query_canceled(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if str(sqlstate or "") == _QUERY_CANCELED_SQLSTATE:
        return True
    if isinstance(exc, DBAPIError):
        text_value = str(exc).lower()
        return "canceling statement due to statement timeout" in text_value
    return False
