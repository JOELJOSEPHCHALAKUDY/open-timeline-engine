"""Worker-side guards for trusted decision extraction (P1 §6 review fixes).

The shared extractor is the source of truth for *whether* a message answers a question; these tests cover the two
places the worker can still get it wrong on its own: which opportunities it loads, and which clock it trusts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from tce_worker.jobs import decision_extraction

OBSERVED_AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class _Settings:
    decision_extraction_enabled = True
    decision_extraction_batch_size = 100
    decision_extraction_lease_seconds = 300
    decision_extraction_max_attempts = 10
    capture_opportunity_ttl_seconds = 3600
    behavior_storage_min_score = 0.55
    security_encryption_secret = ""


class _Result:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.rowcount = len(self._rows)

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def mappings(self) -> _Result:
        return self


class _FakeSession:
    def __init__(self, responses: list[_Result]) -> None:
        self._responses = list(responses)
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query: Any, params: dict[str, Any] | None = None) -> _Result:
        self.executed.append((str(query), dict(params or {})))
        return self._responses.pop(0) if self._responses else _Result()

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _receipt_row(*, receipt_id: UUID, event_id: UUID, observed_at: datetime, ingested_at: datetime | None) -> dict[str, Any]:
    return {
        "id": receipt_id,
        "workspace_id": "personal",
        "owner_id": "human-1",
        "subject_user_id": "human-1",
        "host_session_id": "claude-session",
        "event_id": event_id,
        "origin_kind": "human_input",
        "capture_principal": "host:host-capture-claude",
        "project_id": None,
        "observed_at": observed_at,
        "ingested_at": ingested_at,
        "content_sha256": "ab" * 32,
        "extraction_attempts": 1,
    }


def _opportunity_row(*, opportunity_id: UUID, shadow_id: UUID, frozen_at: datetime) -> dict[str, Any]:
    return {
        "id": opportunity_id,
        "session_id": "codex",
        "turn": 1,
        "objective_hash": "obj-1",
        "task_id": None,
        "project_id": None,
        "decision_family": "safety_confirmation",
        "situation_type": "approval_requested",
        "question_text": "Safety pause: high-risk action detected",
        "alternatives_json": ["confirm", "abort"],
        "pre_answer_snapshot_json": {"objective": "delete the build artifacts"},
        "evidence_revision": "rev-1",
        "shadow_prediction_id": shadow_id,
        "status": "open",
        "expires_at": frozen_at + timedelta(hours=1),
        "created_at": frozen_at,
        "frozen_at": frozen_at,
        "resolved_at": None,
    }


def _run(monkeypatch: Any, *, observed_at: datetime, ingested_at: datetime | None, frozen_at: datetime, shadow_frozen_at: datetime | None = None) -> tuple[dict[str, Any], _FakeSession]:
    receipt_id, event_id, opportunity_id, shadow_id = (uuid4() for _ in range(4))
    session = _FakeSession(
        [
            _Result([_receipt_row(receipt_id=receipt_id, event_id=event_id, observed_at=observed_at, ingested_at=ingested_at)]),
            _Result([{"payload": {"input_excerpt": "confirm"}, "context": {}}]),
            _Result([_opportunity_row(opportunity_id=opportunity_id, shadow_id=shadow_id, frozen_at=frozen_at)]),
            _Result([]),
            _Result([{"id": uuid4()}]),
            _Result([{"id": shadow_id, "frozen_at": shadow_frozen_at or frozen_at, "predicted_choice": "confirm", "abstained": False}]),
        ]
    )
    monkeypatch.setattr(decision_extraction, "get_settings", lambda: _Settings())
    monkeypatch.setattr(decision_extraction, "SessionLocal", lambda: session)
    monkeypatch.setattr(decision_extraction, "save_behavior_evidence", lambda _db, **_kw: uuid4())
    return decision_extraction.run(str(receipt_id)), session


def _query(session: _FakeSession, fragment: str) -> tuple[str, dict[str, Any]]:
    return next((query, params) for query, params in session.executed if fragment in query)


def test_open_opportunity_load_is_bounded_by_the_answer_moment() -> None:
    """A question frozen after the human spoke is not loaded as one this message could answer."""

    session = _FakeSession([_Result([])])
    decision_extraction._load_opportunities(
        session,  # type: ignore[arg-type]
        {"workspace_id": "personal", "subject_user_id": "human-1", "project_id": None},
        status="open",
        since=OBSERVED_AT - timedelta(hours=1),
        observed_at=OBSERVED_AT,
        expiry_at=OBSERVED_AT,
    )
    query, params = session.executed[0]
    assert "AND frozen_at <= :observed_at" in query
    # Liveness is bounded by server time, not by the host-supplied answer moment.
    assert "AND (expires_at IS NULL OR expires_at > :expiry_at)" in query
    assert params["observed_at"] == OBSERVED_AT
    assert params["expiry_at"] == OBSERVED_AT
    assert params["status"] == "open"


def test_resolved_opportunity_load_keeps_its_resolution_window() -> None:
    session = _FakeSession([_Result([])])
    decision_extraction._load_opportunities(
        session,  # type: ignore[arg-type]
        {"workspace_id": "personal", "subject_user_id": "human-1", "project_id": None},
        status="resolved",
        since=OBSERVED_AT - timedelta(hours=1),
        observed_at=OBSERVED_AT,
        expiry_at=OBSERVED_AT,
    )
    query, _params = session.executed[0]
    assert "frozen_at <=" not in query
    assert "expires_at > :expiry_at" not in query
    assert "resolved_at >= :since" in query


def test_answer_moment_clamps_a_host_clock_running_ahead() -> None:
    ahead = {"observed_at": OBSERVED_AT + timedelta(minutes=10), "ingested_at": OBSERVED_AT}
    behind = {"observed_at": OBSERVED_AT - timedelta(minutes=10), "ingested_at": OBSERVED_AT}
    assert decision_extraction._answer_moment(ahead, OBSERVED_AT) == OBSERVED_AT
    assert decision_extraction._answer_moment(behind, OBSERVED_AT) == OBSERVED_AT - timedelta(minutes=10)
    assert decision_extraction._answer_moment({"observed_at": OBSERVED_AT, "ingested_at": None}, OBSERVED_AT) == OBSERVED_AT
    assert decision_extraction._answer_moment({}, OBSERVED_AT) == OBSERVED_AT


def test_host_clock_ahead_does_not_make_a_late_prediction_look_prospective(monkeypatch: Any) -> None:
    """Host says it answered at T+10m; the server ingested at T and froze the prediction at T+1s => retrospective.

    Without the clamp the unclamped host stamp (T+10m) would call this prediction prospective and inflate the
    exit-gate denominator.
    """

    result, session = _run(
        monkeypatch,
        observed_at=OBSERVED_AT + timedelta(minutes=10),
        ingested_at=OBSERVED_AT,
        frozen_at=OBSERVED_AT - timedelta(minutes=5),
        shadow_frozen_at=OBSERVED_AT + timedelta(seconds=1),
    )
    assert result["promoted"] == 1
    _sql, params = _query(session, "UPDATE behavior_shadow_predictions")
    assert params["retro"] is True


def test_prediction_frozen_before_the_answer_stays_prospective(monkeypatch: Any) -> None:
    result, session = _run(
        monkeypatch,
        observed_at=OBSERVED_AT,
        ingested_at=OBSERVED_AT + timedelta(seconds=2),
        frozen_at=OBSERVED_AT - timedelta(minutes=5),
    )
    assert result["promoted"] == 1
    _sql, params = _query(session, "UPDATE behavior_shadow_predictions")
    assert params["retro"] is False
    # the clamp never pushes the answer moment forward past what the host reported
    _load_sql, load_params = _query(session, "FROM decision_opportunities")
    assert load_params["observed_at"] == OBSERVED_AT


class _FreezeAwareSession(_FakeSession):
    """A fake that actually honours the SQL freeze bound, so the load is proved rather than string-matched."""

    def __init__(self, responses: list[_Result], *, opportunity_rows: list[dict[str, Any]]) -> None:
        super().__init__(responses)
        self._opportunity_rows = opportunity_rows
        self.loaded_open: list[dict[str, Any]] | None = None

    def execute(self, query: Any, params: dict[str, Any] | None = None) -> _Result:
        sql, args = str(query), dict(params or {})
        if "FROM decision_opportunities" in sql and args.get("status") == "open":
            self.executed.append((sql, args))
            bounded = "AND frozen_at <= :observed_at" in sql
            rows = [row for row in self._opportunity_rows if not bounded or row["frozen_at"] <= args["observed_at"]]
            self.loaded_open = rows
            return _Result(rows)
        return super().execute(query, params)


def _run_forged(monkeypatch: Any, *, frozen_at: datetime) -> tuple[dict[str, Any], _FreezeAwareSession]:
    receipt_id, event_id, opportunity_id, shadow_id = (uuid4() for _ in range(4))
    session = _FreezeAwareSession(
        [
            _Result([_receipt_row(receipt_id=receipt_id, event_id=event_id, observed_at=OBSERVED_AT, ingested_at=OBSERVED_AT)]),
            _Result([{"payload": {"input_excerpt": "confirm"}, "context": {}}]),
            _Result([]),  # resolved opportunities
            _Result([{"id": uuid4()}]),  # candidate insert
            _Result([{"id": shadow_id, "frozen_at": frozen_at, "predicted_choice": "confirm", "abstained": False}]),
        ],
        opportunity_rows=[_opportunity_row(opportunity_id=opportunity_id, shadow_id=shadow_id, frozen_at=frozen_at)],
    )
    monkeypatch.setattr(decision_extraction, "get_settings", lambda: _Settings())
    monkeypatch.setattr(decision_extraction, "SessionLocal", lambda: session)
    monkeypatch.setattr(decision_extraction, "save_behavior_evidence", lambda _db, **_kw: uuid4())
    return decision_extraction.run(str(receipt_id)), session


def test_forged_consent_prompt_is_not_even_loaded_against_the_later_question(monkeypatch: Any) -> None:
    """The hook POSTs 'confirm' at T; takeover_step freezes the safety question at T+60s.

    The freeze bound keeps that question out of the load entirely, so the prompt cannot resolve it: no promotion,
    no human_resolutions row, and the opportunity is left open for a real answer.
    """

    result, session = _run_forged(monkeypatch, frozen_at=OBSERVED_AT + timedelta(seconds=60))
    assert session.loaded_open == []
    assert result["promoted"] == 0
    assert result["discarded"] == 1
    assert not [query for query, _params in session.executed if "INSERT INTO human_resolutions" in query]
    assert not [query for query, _params in session.executed if "SET status = 'resolved'" in query]


def test_the_same_answer_after_the_freeze_still_promotes(monkeypatch: Any) -> None:
    """Control for the case above: identical receipt and question, ordering reversed."""

    result, session = _run_forged(monkeypatch, frozen_at=OBSERVED_AT - timedelta(seconds=60))
    assert session.loaded_open is not None
    assert len(session.loaded_open) == 1
    assert result["promoted"] == 1
    assert [query for query, _params in session.executed if "INSERT INTO human_resolutions" in query]
