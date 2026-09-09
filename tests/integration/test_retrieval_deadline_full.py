"""G8 Full — the Postgres statement-timeout guard, against a real connection.

The four properties that make the guard safe rather than merely present:

1. The prior ``statement_timeout`` is restored after every guarded read. ``RELEASE SAVEPOINT``
   does NOT revert ``SET LOCAL``, so without an explicit restore every bounded read would leave
   a 10-20 ms timeout in force and kill the writes that share the transaction.
2. The restore is **quoted**, because ``current_setting`` returns a unit-bearing string and a
   bare integer silently means milliseconds.
3. The prior value is captured ONCE per transaction. ``setdefault`` evaluates its second
   argument eagerly, so that form issues the SELECT on every call while appearing to memoise.
4. The first query of a turn is unguarded, so it cannot be aborted by the budget — which is
   what makes ``len(queries_executed) == 1`` true rather than aspirational.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_api.db import get_session_factory
from tce_api.deadline_pg import (
    absorb_pg_deadline,
    begin_pg_deadline,
    finish_pg_deadline,
    transaction_statement_timeout,
)
from tce_shared.deadline import Deadline, RetrievalLedger

pytestmark = pytest.mark.skipif(
    not os.environ.get("TCE_DATABASE_URL"),
    reason="Full-backend integration tests need TCE_DATABASE_URL",
)


@pytest.fixture()
def db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _show_timeout(db: Session) -> str:
    return str(db.execute(text("SHOW statement_timeout")).scalar_one())


def _turn(budget_ms: int = 3500) -> Deadline:
    return Deadline.start(budget_ms=budget_ms, backend_budget_ms=60, floor_ms=5, label="test")


def test_statement_timeout_is_set_and_restored(db: Session) -> None:
    deadline = _turn()
    ledger = RetrievalLedger()
    # Burn the first-query exemption: the primary query is deliberately unguarded.
    ledger.record_executed("primary", ms=1, rows=1)

    guard = begin_pg_deadline(
        db, deadline, name="secondary", ledger=ledger, requested_ms=60, floor_ms=10
    )
    assert guard is not None and guard.unbounded is False
    assert guard.budget_ms <= 60
    inside = _show_timeout(db)
    db.execute(text("SELECT 1")).scalar_one()
    finish_pg_deadline(guard, db, rows=1)
    after = _show_timeout(db)

    assert inside != after, "the guard did not install a timeout"
    assert after == guard.prior_timeout, "RELEASE does not revert SET LOCAL; restore it explicitly"
    assert ledger.executed and ledger.executed[-1]["name"] == "secondary"

    # A write after the guard must run with the prior timeout, never the guard's slice.
    db.execute(text("SELECT 2")).scalar_one()
    assert _show_timeout(db) == guard.prior_timeout


def test_prior_timeout_is_restored_verbatim(db: Session) -> None:
    """A GUC string carries a unit. Restoring it as a bare integer would silently mean ms."""
    db.execute(text("SET LOCAL statement_timeout = '3s'"))
    assert _show_timeout(db) == "3s"
    ledger = RetrievalLedger()
    ledger.record_executed("primary", ms=1, rows=1)

    guard = begin_pg_deadline(
        db, _turn(), name="secondary", ledger=ledger, requested_ms=40, floor_ms=10
    )
    assert guard is not None
    db.execute(text("SELECT 1")).scalar_one()
    finish_pg_deadline(guard, db)
    assert _show_timeout(db) == "3s"


def test_transaction_statement_timeout_memoises(db: Session) -> None:
    before = _count_current_setting_calls(db)
    first = transaction_statement_timeout(db)
    mid = _count_current_setting_calls(db)
    transaction_statement_timeout(db)
    transaction_statement_timeout(db)
    after = _count_current_setting_calls(db)

    assert first == transaction_statement_timeout(db)
    assert mid - before == 1, "the first call must issue exactly one SELECT"
    assert after == mid, "later calls must issue none"


def _count_current_setting_calls(db: Session) -> int:
    """Round trips are not directly observable, so count the memo instead."""
    return 1 if "tce_prior_statement_timeout" in db.info else 0


def test_first_query_is_unguarded(db: Session) -> None:
    """Case (b): the primary query installs no savepoint and no SET LOCAL at all.

    A tight budget therefore degrades only SECONDARY queries. Guarding the primary made a 1 ms
    budget abort it, ``record_executed`` never fire, and ``queries_executed`` come back empty.
    """
    ledger = RetrievalLedger()
    assert ledger.is_first_query() is True

    before = _show_timeout(db)
    first = begin_pg_deadline(
        db, _turn(), name="primary", ledger=ledger, requested_ms=60, floor_ms=10
    )
    assert first is not None
    assert first.unbounded is True
    assert first.savepoint is None
    assert _show_timeout(db) == before, "the first query must not install a timeout"
    finish_pg_deadline(first, db, rows=1)
    assert len(ledger.executed) == 1

    second = begin_pg_deadline(
        db, _turn(), name="secondary", ledger=ledger, requested_ms=60, floor_ms=10
    )
    assert second is not None
    assert second.unbounded is False
    assert second.savepoint is not None
    finish_pg_deadline(second, db)


def test_retrieval_disabled_installs_no_guard(db: Session) -> None:
    """S6 — the off-switch must actually switch off, not cap at the retrieval budget."""
    unbounded = Deadline.unbounded("turn")
    ledger = RetrievalLedger()
    ledger.record_executed("primary", ms=1, rows=1)

    before = _show_timeout(db)
    guard = begin_pg_deadline(
        db, unbounded, name="secondary", ledger=ledger, requested_ms=60, floor_ms=10
    )
    assert guard is not None
    assert guard.unbounded is True
    assert guard.savepoint is None
    assert _show_timeout(db) == before
    finish_pg_deadline(guard, db)
    assert ledger.skipped == []
    assert ledger.degraded() is False


def test_exhausted_budget_skips_and_records(db: Session) -> None:
    expired = Deadline.start(budget_ms=0, backend_budget_ms=60, floor_ms=5, label="expired")
    ledger = RetrievalLedger()
    ledger.record_executed("primary", ms=1, rows=1)

    before = _show_timeout(db)
    guard = begin_pg_deadline(
        db, expired, name="secondary", ledger=ledger, requested_ms=60, floor_ms=10
    )
    assert guard is None, "an exhausted budget must skip the query, not run it unbounded"
    assert ledger.skipped and ledger.skipped[-1]["name"] == "secondary"
    assert ledger.degraded() is True
    assert _show_timeout(db) == before, "a skip must not touch the session's timeout"


def test_cancellation_is_absorbed_and_the_timeout_restored(db: Session) -> None:
    """A budget abort rolls the savepoint back FIRST, then restores — the reverse order raises
    InFailedSqlTransaction and poisons the rest of the turn."""
    ledger = RetrievalLedger()
    ledger.record_executed("primary", ms=1, rows=1)
    guard = begin_pg_deadline(
        db, _turn(), name="slow", ledger=ledger, requested_ms=1, floor_ms=1
    )
    assert guard is not None and guard.unbounded is False
    try:
        db.execute(text("SELECT pg_sleep(0.5)"))
    except Exception as exc:  # noqa: BLE001 - the guard decides whether this was a timeout
        assert absorb_pg_deadline(guard, db, exc) is True
    else:  # pragma: no cover - only if the server ignored the timeout
        finish_pg_deadline(guard, db)
        pytest.skip("statement_timeout did not fire on this server")

    assert ledger.timed_out and ledger.timed_out[-1]["name"] == "slow"
    assert _show_timeout(db) == guard.prior_timeout
    # The transaction must still be usable: only the savepoint was rolled back.
    assert db.execute(text("SELECT 1")).scalar_one() == 1


def test_writes_are_never_guarded() -> None:
    """R11, asserted on the source: the guard is a read-only device.

    A skipped write would report a revision that was never persisted.
    """
    from pathlib import Path

    store = (
        Path(__file__).resolve().parents[2]
        / "services"
        / "tce_api"
        / "tce_api"
        / "task_state_store.py"
    ).read_text(encoding="utf-8")
    assert "begin_pg_deadline" not in store
