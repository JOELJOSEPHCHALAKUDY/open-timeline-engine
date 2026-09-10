"""G8 Lite — the retrieval deadline against the real SQLite store (design §10.3, Builder C).

The four properties that make the deadline safe rather than merely present:

1. The FIRST query of a turn is unguarded, so it always completes and `queries_executed` is
   exactly 1 even at a 1 ms budget.
2. No progress handler is ever leaked. A leaked handler would abort the turn's OWN commits,
   because Lite reuses one connection for retrieval and for the task-state CAS.
3. `OperationalError("interrupted")` is never retried, and is never laundered into
   `like_fallback_error`.
4. With `retrieval_deadline_enabled=False` nothing is installed at all — the knob is an
   off-switch, not a 120 ms cap.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.db import _connect
from tce_lite_api.deadline_sqlite import (
    absorb_sqlite_deadline,
    begin_sqlite_deadline,
    finish_sqlite_deadline,
)
from tce_lite_api.main import app
from tce_lite_api.store import _is_retryable_db_error, _run_sql_retry
from tce_shared.deadline import (
    RETRIEVAL_REASON_DEADLINE_SKIP,
    SQLITE_INTERRUPT_MARKER,
    Deadline,
    RetrievalLedger,
)

_TOKEN = "retrieval-deadline-lite-token"
_MISSING = object()
_GUARDED_SETTINGS = (
    "lite_db_path",
    "api_tokens",
    "default_operation_mode",
    "identity_claims_mode",
    "identity_claims_json",
    "workspace_access_mode",
    "takeover_turn_budget_ms",
    "task_state_enabled",
    "retrieval_deadline_enabled",
    "retrieval_deadline_floor_ms",
    "retrieval_statement_floor_ms",
    "retrieval_advisor_min_ms",
    "sqlite_progress_instructions",
    "context_retrieval_budget_ms",
    "search_retry_enabled",
    "search_retry_max_attempts",
    "search_retry_backoff_ms",
    "search_retry_budget_ms",
)

_APP_CONTEXT: dict[str, Any] = {"domain": "coding", "project": "open-timeline-engine"}


def _snapshot_settings() -> dict[str, Any]:
    settings = get_settings()
    return {key: getattr(settings, key, _MISSING) for key in _GUARDED_SETTINGS}


def _restore_settings(snapshot: dict[str, Any]) -> None:
    settings = get_settings()
    for key, value in snapshot.items():
        if value is _MISSING:
            if hasattr(settings, key):
                try:
                    delattr(settings, key)
                except AttributeError:
                    pass
            continue
        setattr(settings, key, value)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "retrieval-deadline-lite.db"


@pytest.fixture()
def lite_client(db_path: Path) -> Iterator[TestClient]:
    snapshot = _snapshot_settings()
    settings = get_settings()
    settings.lite_db_path = str(db_path)
    settings.api_tokens = _TOKEN
    settings.default_operation_mode = "clone_advisor"
    settings.identity_claims_mode = "compat"
    settings.identity_claims_json = "{}"
    settings.workspace_access_mode = "compat"
    settings.retrieval_deadline_enabled = True
    try:
        with TestClient(app) as client:
            yield client
    finally:
        _restore_settings(snapshot)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "X-TCE-Consumer": "codex",
        "X-TCE-Role": "executor",
        "X-TCE-Workspace": "personal",
        "X-TCE-User": "human-1",
    }


def _search(client: TestClient, *, k: int = 5) -> dict[str, Any]:
    response = client.post(
        "/v1/search",
        json={"query": "deadline", "k": k},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _step(client: TestClient, session_id: str) -> dict[str, Any]:
    response = client.post(
        "/v1/takeover/step",
        json={
            "message": "beru take over",
            "session_id": session_id,
            "task": "check the retrieval deadline",
            "persona_mode": "shadow",
            "app_context": dict(_APP_CONTEXT),
            "constraints": {"k": 4},
            "allow_fallback": True,
        },
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --------------------------------------------------------------------------- G8


def test_first_query_is_unguarded() -> None:
    """R2-G14/C3-G14: at a 1 ms budget the first query still completes and is recorded."""
    conn = sqlite3.connect(":memory:")
    try:
        deadline = Deadline.start(budget_ms=1, backend_budget_ms=1, label="turn")
        ledger = RetrievalLedger()
        time.sleep(0.01)  # the budget is provably gone
        assert deadline.expired()

        guard = begin_sqlite_deadline(
            conn,
            deadline,
            name="lexical",
            ledger=ledger,
            requested_ms=120,
            floor_ms=10,
            instructions=1000,
        )
        assert guard is not None, "the first query of a turn must never be skipped"
        assert guard.unbounded is True
        conn.execute("SELECT 1").fetchone()
        finish_sqlite_deadline(guard)

        assert len(ledger.executed) == 1
        assert not ledger.timed_out
        assert not ledger.skipped

        # The SECOND query is guarded, and with the budget gone it is skipped, not run.
        second = begin_sqlite_deadline(
            conn,
            deadline,
            name="trigram",
            ledger=ledger,
            requested_ms=120,
            floor_ms=10,
            instructions=1000,
        )
        assert second is None
        assert ledger.skipped[-1]["reason"] == RETRIEVAL_REASON_DEADLINE_SKIP
        assert ledger.degraded() is True
    finally:
        conn.close()


class _RecordingConnection(sqlite3.Connection):
    """Records every set_progress_handler call so a test can prove nothing was installed."""

    installed: list[Any]

    def set_progress_handler(self, handler: Any, n: int) -> None:
        try:
            self.installed.append((handler, n))
        except AttributeError:  # pragma: no cover - the list is attached right after connect()
            pass
        super().set_progress_handler(handler, n)


def _recording_connection() -> _RecordingConnection:
    conn = sqlite3.connect(":memory:", factory=_RecordingConnection)
    conn.installed = []
    return conn


def test_retrieval_disabled_installs_no_handler() -> None:
    """S6/R4-G2: the off-switch installs nothing, rather than capping retrieval at 120 ms."""
    conn = _recording_connection()
    try:
        unbounded = Deadline.unbounded()
        ledger = RetrievalLedger()
        # Burn the first-query exemption so the next call is judged purely on the deadline.
        ledger.record_executed("primary", ms=1)
        guard = begin_sqlite_deadline(
            conn,
            unbounded,
            name="trigram",
            ledger=ledger,
            requested_ms=120,
            floor_ms=10,
            instructions=1000,
        )
        assert guard is not None
        assert guard.unbounded is True
        finish_sqlite_deadline(guard)
        assert conn.installed == [], "an unbounded turn must install no progress handler"
        assert not ledger.skipped

        # A BOUNDED turn, by contrast, installs exactly one handler and removes it.
        bounded = Deadline.start(budget_ms=5000, backend_budget_ms=60, label="turn")
        guard2 = begin_sqlite_deadline(
            conn, bounded, name="ann", ledger=ledger, requested_ms=60, floor_ms=10, instructions=1000
        )
        assert guard2 is not None and guard2.unbounded is False
        finish_sqlite_deadline(guard2)
        assert [n for _handler, n in conn.installed] == [1000, 0]
    finally:
        conn.close()


def test_no_progress_handler_is_leaked() -> None:
    """A leaked handler would abort the turn's OWN commits: teardown is unconditional."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (a INTEGER)")
        deadline = Deadline.start(budget_ms=5000, backend_budget_ms=60, label="turn")
        ledger = RetrievalLedger()
        ledger.record_executed("primary", ms=1)

        guard = begin_sqlite_deadline(
            conn, deadline, name="trigram", ledger=ledger, requested_ms=60, floor_ms=10, instructions=1000
        )
        assert guard is not None and guard.unbounded is False
        finish_sqlite_deadline(guard)

        # A guard that absorbed a timeout tears the handler down too.
        guard2 = begin_sqlite_deadline(
            conn, deadline, name="ann", ledger=ledger, requested_ms=60, floor_ms=10, instructions=1000
        )
        assert guard2 is not None
        assert absorb_sqlite_deadline(guard2, sqlite3.OperationalError("interrupted")) is True

        # ... and so does one whose exception was NOT its own abort.
        guard3 = begin_sqlite_deadline(
            conn, deadline, name="entity", ledger=ledger, requested_ms=60, floor_ms=10, instructions=1000
        )
        assert guard3 is not None
        assert absorb_sqlite_deadline(guard3, sqlite3.OperationalError("no such table: nope")) is False

        # The connection is still usable and can still commit its own writes.
        conn.execute("INSERT INTO t(a) VALUES (1)")
        conn.commit()
        assert conn.execute("SELECT COUNT(1) FROM t").fetchone()[0] == 1
    finally:
        conn.close()


def test_interrupted_is_not_retryable() -> None:
    assert _is_retryable_db_error(sqlite3.OperationalError("interrupted")) is False
    # ... and the token that WOULD have matched it is still retryable on its own.
    assert _is_retryable_db_error(sqlite3.OperationalError("database is locked")) is True


def test_retry_sleeps_respect_the_deadline() -> None:
    """C3-G8: an exhausted deadline re-raises instead of sleeping out the retry budget."""

    class _Settings:
        search_retry_enabled = True
        search_retry_max_attempts = 3
        search_retry_backoff_ms = 500
        search_retry_budget_ms = 5000
        retrieval_statement_floor_ms = 10

    calls: list[int] = []

    def _always_locked() -> None:
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    deadline = Deadline.start(budget_ms=1, backend_budget_ms=1, label="turn")
    time.sleep(0.01)
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        _run_sql_retry(_always_locked, settings=_Settings(), deadline=deadline)  # type: ignore[arg-type]
    elapsed_ms = (time.monotonic() - started) * 1000
    assert len(calls) == 1, "the exhausted deadline must stop the retry before the second attempt"
    assert elapsed_ms < 250, "no 500 ms backoff sleep may happen outside the deadline"


def test_interrupt_marker_is_the_shared_literal() -> None:
    assert SQLITE_INTERRUPT_MARKER == "interrupted"
    assert SQLITE_INTERRUPT_MARKER in str(sqlite3.OperationalError("interrupted")).lower()


# --------------------------------------------------------------------------- live turns


def test_turn_reports_the_ten_deadline_keys(lite_client: TestClient, db_path: Path) -> None:
    _search(lite_client)  # force init_db
    from tce_lite_api.store import search_events
    from tce_shared.events import EventSearchRequest

    conn = _connect()
    try:
        _response, _blocked, meta = search_events(
            conn,
            EventSearchRequest(query="deadline", k=3),
            get_settings(),
            workspace_id="personal",
            owner_id="human-1",
        )
    finally:
        conn.close()
    for key in (
        "deadline_policy_revision",
        "deadline_budget_ms",
        "deadline_backend_budget_ms",
        "deadline_remaining_ms",
        "deadline_expired",
        "queries_executed",
        "queries_timed_out",
        "queries_skipped",
        "queries_cached",
        "retrieval_degraded",
    ):
        assert key in meta, f"retrieval_meta is missing {key}"
    # With no deadline supplied the ten keys are still present, at their neutral values.
    assert meta["deadline_budget_ms"] == 0
    assert meta["deadline_expired"] is False
    assert meta["retrieval_degraded"] is False


def test_a_tight_budget_does_not_break_the_turn_or_leak_a_handler(
    lite_client: TestClient, db_path: Path
) -> None:
    """G8 live: a 1 ms turn budget still writes task state (R11) and leaves the DB healthy."""
    settings = get_settings()
    settings.takeover_turn_budget_ms = 1
    settings.context_retrieval_budget_ms = 1
    session_id = f"codex-{uuid.uuid4().hex[:8]}"

    body = _step(lite_client, session_id)
    # Writes are NEVER deadline-gated (R11): the task-state revision advanced anyway.
    assert body["task_state_revision"] >= 1

    conn = _connect()
    try:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        assert int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
    finally:
        conn.close()
