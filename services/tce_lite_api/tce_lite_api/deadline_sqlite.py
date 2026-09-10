"""SQLite enforcement of the retrieval deadline (P2 §6.3, §0.7 S10.3).

The Lite twin of `tce_api.deadline_pg`. Same three-case shape, same call protocol, same
ledger bookkeeping — only the mechanism differs: SQLite has no ``statement_timeout``, so a
bounded read installs a progress handler that returns non-zero once the wall clock passes the
slice, which raises ``sqlite3.OperationalError("interrupted")`` from inside the statement.

Two rules that are not negotiable:

* **The handler teardown is unconditional.** Every exit path — success, absorbed timeout,
  re-raised error — calls ``conn.set_progress_handler(None, 0)``. The Lite connection is reused
  for the turn's own writes (``_record_takeover_action``, the ``context_bundles`` insert, the
  task-state CAS UPDATE), and a leaked handler would abort the turn's own COMMIT.
* **Writes are NEVER wrapped in a deadline guard (R11).** The guard is applied to *read* queries
  only. A write that is interrupted mid-statement is a lost turn, not a degraded one.

``conn.interrupt()`` is deliberately not used: it needs a watchdog thread and races at statement
boundaries. ``busy_timeout`` bounds lock waits only, can never serve as a query deadline, and is
never lowered (§5.2 rule 4).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from tce_shared.deadline import (
    RETRIEVAL_REASON_DEADLINE_SKIP,
    SQLITE_INTERRUPT_MARKER,
    Deadline,
    RetrievalLedger,
)

# An "unbounded" turn deadline reports a remaining budget larger than any real one.
_UNBOUNDED_REMAINING_MS = 10**8


@dataclass(slots=True)
class SqliteDeadlineGuard:
    name: str
    budget_ms: int
    conn: sqlite3.Connection
    started: float
    ledger: RetrievalLedger | None
    unbounded: bool


def _teardown(guard: SqliteDeadlineGuard) -> None:
    """Remove the progress handler. Safe to call on an unbounded guard, which installed none."""
    if guard.unbounded:
        return
    try:
        guard.conn.set_progress_handler(None, 0)
    except Exception:  # pragma: no cover - a closed connection cannot leak a handler
        pass


def begin_sqlite_deadline(
    conn: sqlite3.Connection,
    deadline: Deadline | None,
    *,
    name: str,
    ledger: RetrievalLedger | None,
    requested_ms: int,
    floor_ms: int,
    instructions: int,
) -> SqliteDeadlineGuard | None:
    """``None`` => the caller must SKIP the query; a ledger skip has been recorded.

    THREE cases, pinned (identical to ``begin_pg_deadline``):

      (a) ``deadline is None`` or the turn is unbounded: return an unbounded guard. Nothing is
          installed. This is what makes ``retrieval_deadline_enabled=False`` genuinely free
          rather than a 120 ms cap (S6).
      (b) ``ledger.is_first_query()``: SAME as (a) — the first query of a turn is UNGUARDED, so
          it cannot be aborted by the budget and ``record_executed`` always fires.
      (c) otherwise: skip when the deadline no longer allows ``floor_ms``; else install the
          progress handler for ``deadline.slice_ms(requested_ms)``.
    """
    started = time.monotonic()
    if deadline is None or deadline.remaining_ms() >= _UNBOUNDED_REMAINING_MS:
        return SqliteDeadlineGuard(
            name=name,
            budget_ms=0,
            conn=conn,
            started=started,
            ledger=ledger,
            unbounded=True,
        )
    if ledger is not None and ledger.is_first_query():
        return SqliteDeadlineGuard(
            name=name,
            budget_ms=0,
            conn=conn,
            started=started,
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
    abort_at = started + (budget_ms / 1000.0)

    def _handler() -> int:
        return 1 if time.monotonic() >= abort_at else 0

    conn.set_progress_handler(_handler, int(max(1, instructions)))
    return SqliteDeadlineGuard(
        name=name,
        budget_ms=budget_ms,
        conn=conn,
        started=started,
        ledger=ledger,
        unbounded=False,
    )


def finish_sqlite_deadline(guard: SqliteDeadlineGuard) -> None:
    """Success path. ALWAYS tears the handler down, then records the executed query."""
    _teardown(guard)
    if guard.ledger is not None:
        elapsed_ms = int(max(0.0, (time.monotonic() - guard.started)) * 1000)
        guard.ledger.record_executed(guard.name, ms=elapsed_ms)


def absorb_sqlite_deadline(guard: SqliteDeadlineGuard, exc: BaseException) -> bool:
    """``True`` when the exception is this guard's own abort.

    The handler is torn down on **both** paths — a re-raised error must not leave it installed
    either. An unbounded guard always returns ``False``: it installed nothing, so nothing it did
    can have caused the exception.
    """
    _teardown(guard)
    if guard.unbounded:
        return False
    if SQLITE_INTERRUPT_MARKER not in str(exc).lower():
        return False
    if guard.ledger is not None:
        guard.ledger.record_timeout(guard.name, budget_ms=guard.budget_ms)
    return True


__all__ = [
    "SqliteDeadlineGuard",
    "absorb_sqlite_deadline",
    "begin_sqlite_deadline",
    "finish_sqlite_deadline",
]
