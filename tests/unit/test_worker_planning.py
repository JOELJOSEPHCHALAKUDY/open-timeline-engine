"""Unit tests for the planning and dream-synthesis worker jobs.

Every store call the jobs make is monkeypatched against a fake session, so these run without
Postgres and without waiting on Builder B's SQL. What they actually assert is the *ordering*
and the *refusals*: that a stale result writes nothing, that a lost lease writes nothing, and
that the task-state write happens before any goal row is inserted.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from tce_api.plan_rows import resolved_scope_from_json, resolved_scope_to_json
from tce_api.planning_store import complete_planning_job
from tce_shared.plan_decomposition import PLAN_DECOMPOSITION_PROMPT
from tce_shared.scope import PROJECT_UNBOUND, ResolvedScope
from tce_shared.task_state import (
    PLANNING_JOB_KIND_DECOMPOSE,
    PLANNING_JOB_KIND_DREAM,
    PLANNING_STALE_REASON,
    NextPermittedAction,
    PlanCharter,
    TaskStatePreconditionFailed,
    TaskStateProjection,
    TaskStatus,
    charter_to_json,
)
from tce_worker.jobs import dream_synthesis, planning

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
JOB_ID = "11111111-1111-4111-8111-111111111111"


class _Settings:
    planning_enabled = True
    planning_job_lease_seconds = 120
    planning_job_max_attempts = 3
    planning_job_batch_size = 20
    planning_job_backoff_cap_seconds = 900
    takeover_plan_llm_enabled = True
    takeover_dream_llm_enabled = True
    takeover_plan_llm_timeout_seconds = 25.0
    takeover_plan_max_steps = 8
    model_provider = "ollama"
    ollama_url = "http://ollama:11434"
    embed_model = "mxbai-embed-large"
    extract_model = "qwen2.5:3b"
    redis_url = ""
    block_sensitivity = 3


class _Result:
    def __init__(self, *, scalar: Any = None, rows: list[Any] | None = None) -> None:
        self._scalar = scalar
        self._rows = rows or []
        self.rowcount = len(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.committed = 0
        self.rolled_back = 0

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def execute(self, query: Any, params: Any = None) -> _Result:
        self.executed.append((str(query), dict(params or {})))
        return _Result(scalar=False)

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


def _scope() -> ResolvedScope:
    return ResolvedScope(
        workspace_id="ws-1",
        executor_id="exec-1",
        owner_id="owner-1",
        subject_user_id="subject-1",
        project_id=None,
        project_binding=PROJECT_UNBOUND,
        task_id="session-1",
        owner_ids=frozenset({"owner-1", "peer-1"}),
    )


def _projection(**overrides: Any) -> TaskStateProjection:
    base = TaskStateProjection(
        task_id="session-1",
        workspace_id="ws-1",
        owner_id="owner-1",
        session_id="session-1",
        revision=3,
        contract_revision=1,
        objective_text="ship the thing",
        objective_hash="h1",
        objective_set_at=NOW,
        objective_set_seq=1,
        status=TaskStatus.PLANNING,
        next_permitted_action=NextPermittedAction.AWAIT_PLANNING,
        constraints=(),
        open_decisions=(),
        plan=None,
        unresolved_effects=(),
        latest_verification=None,
        citations=(),
    )
    return replace(base, **overrides) if overrides else base


def _job_row(**overrides: Any) -> dict[str, Any]:
    projection = _projection()
    scope = _scope()
    row: dict[str, Any] = {
        "id": JOB_ID,
        "workspace_id": "ws-1",
        "owner_id": "owner-1",
        "session_id": "session-1",
        "task_id": "session-1",
        "task_state_id": "22222222-2222-4222-8222-222222222222",
        "job_kind": PLANNING_JOB_KIND_DECOMPOSE,
        "input_revision": planning._current_input_revision(projection, scope),
        "contract_revision": 1,
        "objective_hash": "h1",
        "objective_text": "ship the thing",
        "charter_json": json.dumps(charter_to_json(PlanCharter(max_steps=8))),
        "scope_json": json.dumps(resolved_scope_to_json(scope)),
        "attempts": 1,
        "max_attempts": 3,
        "cancel_requested": False,
    }
    row.update(overrides)
    return row


_MODEL_PLAN = {
    "steps": [
        {"id": "a", "title": "first", "description": "do the first thing"},
        {"id": "b", "title": "second", "description": "do the second thing", "depends_on": ["a"]},
    ]
}


class _Gateway:
    def __init__(self, payload: Any = None, *, boom: Exception | None = None) -> None:
        self.payload = payload if payload is not None else _MODEL_PLAN
        self.boom = boom
        self.prompts: list[str] = []

    def extract_structured(self, prompt: str, schema_name: str) -> Any:
        self.prompts.append(prompt)
        if self.boom is not None:
            raise self.boom
        return self.payload


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Wire both jobs to a fake session and record every store call."""
    session = _FakeSession()
    state: dict[str, Any] = {
        "session": session,
        "claimed": [_job_row()],
        "loaded": (_projection(), 7, "sr-1"),
        "completions": [],
        "complete_returns": True,
        "plan_rows": [],
        "applied": [],
        "gateway": _Gateway(),
        "revalidate_projection": None,
    }

    def _factory() -> Any:
        return lambda: session

    def _claim(_db: Any, **_kwargs: Any) -> list[Any]:
        return list(state["claimed"])

    def _load(_db: Any, **_kwargs: Any) -> Any:
        return state["loaded"]

    def _complete(_db: Any, **kwargs: Any) -> bool:
        state["completions"].append(kwargs)
        return bool(state["complete_returns"])

    def _apply(db: Any, **kwargs: Any) -> Any:
        state["applied"].append(kwargs)
        db.execute("UPDATE task_states SET revision = revision + 1", {})
        revalidate = kwargs.get("revalidate")
        fresh = state["revalidate_projection"]
        if revalidate is not None and fresh is not None:
            ok, reason = revalidate(fresh)
            if not ok:
                raise TaskStatePreconditionFailed(task_id=str(kwargs.get("task_id")), reason=reason)
        return None

    def _write_plan(db: Any, **kwargs: Any) -> str:
        state["plan_rows"].append(kwargs)
        db.execute("INSERT INTO autonomy_goals (...) VALUES (...)", {})
        return str(kwargs.get("root_goal_id") or "root")

    for module in (planning,):
        monkeypatch.setattr(module, "get_settings", lambda: _Settings())
        monkeypatch.setattr(module, "get_session_factory", _factory)
        monkeypatch.setattr(module, "claim_planning_job", _claim)
        monkeypatch.setattr(module, "load_task_state", _load)
        monkeypatch.setattr(module, "complete_planning_job", _complete)
        monkeypatch.setattr(module, "apply_task_state_events", _apply)
        monkeypatch.setattr(module, "get_gateway", lambda _settings: state["gateway"])
    monkeypatch.setattr(planning, "write_plan_rows", _write_plan)
    return state


# --------------------------------------------------------------------------- scope round trip


def test_scope_json_round_trips() -> None:
    scope = _scope()
    assert resolved_scope_from_json(resolved_scope_to_json(scope)) == scope
    # A job row written by an older build must still be claimable, so a malformed blob
    # returns a usable scope rather than raising.
    revived = resolved_scope_from_json({})
    assert isinstance(revived, ResolvedScope)
    assert resolved_scope_from_json({"owner_id": "owner-9"}).owner_ids == frozenset({"owner-9"})


# --------------------------------------------------------------------------- happy path


def test_run_accepts_both_call_shapes(harness: dict[str, Any]) -> None:
    one = planning.run(JOB_ID)
    assert set(one) >= {"status", "processed", "succeeded", "failed", "discarded", "cancelled", "skipped"}
    many = planning.run(None, 20)
    assert set(many) >= {"status", "processed"}


def test_successful_plan_writes_rows_and_succeeds(harness: dict[str, Any]) -> None:
    result = planning.run(JOB_ID)
    assert result["status"] == "succeeded"
    assert harness["plan_rows"], "the plan step rows were never written"
    assert harness["completions"][-1]["state"] == "succeeded"
    assert harness["session"].committed >= 1


def test_write_back_takes_task_states_before_autonomy_goals(harness: dict[str, Any]) -> None:
    planning.run(JOB_ID)
    statements = [sql for sql, _ in harness["session"].executed]
    first_task_state = next(i for i, sql in enumerate(statements) if "task_states" in sql)
    first_goal = next(i for i, sql in enumerate(statements) if "INSERT INTO autonomy_goals" in sql)
    assert first_task_state < first_goal

    payload = harness["applied"][0]["new_events"][0].payload
    assert payload["root_goal_id"] == harness["plan_rows"][0]["root_goal_id"]
    assert payload["producer"] == "model"


def test_charter_comes_from_the_job_row(harness: dict[str, Any]) -> None:
    """settings.takeover_plan_max_steps is 8; the row says 1, and the row wins."""
    harness["claimed"] = [_job_row(charter_json=json.dumps(charter_to_json(PlanCharter(max_steps=1))))]
    planning.run(JOB_ID)
    assert len(harness["plan_rows"][0]["steps"]) == 1


def test_prompt_uses_replace_not_format(harness: dict[str, Any]) -> None:
    with pytest.raises(KeyError):
        PLAN_DECOMPOSITION_PROMPT.format(objective="x")
    planning.run(JOB_ID)
    assert harness["gateway"].prompts
    assert "ship the thing" in harness["gateway"].prompts[0]


# --------------------------------------------------------------------------- refusals


def test_claim_rowcount_zero_is_skipped(harness: dict[str, Any]) -> None:
    harness["claimed"] = []
    assert planning.run(JOB_ID)["status"] == "skipped"
    assert harness["session"].executed == []


def test_disabled_returns_immediately(monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]) -> None:
    class _Off(_Settings):
        planning_enabled = False

    monkeypatch.setattr(planning, "get_settings", lambda: _Off())
    assert planning.run(JOB_ID)["status"] == "disabled"
    assert harness["session"].executed == []

    class _DreamsOff(_Settings):
        takeover_dream_llm_enabled = False

    monkeypatch.setattr(dream_synthesis, "get_settings", lambda: _DreamsOff())
    assert dream_synthesis.run()["status"] == "disabled"
    assert harness["session"].executed == []


def test_stale_input_revision_is_discarded(harness: dict[str, Any]) -> None:
    harness["claimed"] = [_job_row(input_revision="a-revision-from-a-previous-objective")]
    result = planning.run(JOB_ID)
    assert result["status"] == "discarded"
    assert result["reason"] == PLANNING_STALE_REASON
    statements = [sql for sql, _ in harness["session"].executed]
    assert not any("INSERT INTO autonomy_goals" in sql for sql in statements)
    assert not any("UPDATE task_states" in sql for sql in statements)
    assert harness["completions"][-1]["state"] == "discarded"


def test_cancel_requested_is_cancelled(harness: dict[str, Any]) -> None:
    harness["claimed"] = [_job_row(cancel_requested=True)]
    result = planning.run(JOB_ID)
    assert result["status"] == "cancelled"
    assert not harness["plan_rows"]
    assert harness["completions"][-1]["state"] == "cancelled"


def test_task_state_missing_is_discarded(harness: dict[str, Any]) -> None:
    harness["loaded"] = None
    result = planning.run(JOB_ID)
    assert result["status"] == "discarded"
    assert harness["completions"][-1]["last_error"] == "task_state_missing"


def test_completion_after_cancel_is_rejected_by_the_lease_fence(
    monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]
) -> None:
    """The fence is THREE predicates, and the third is the one this test exists for.

    ``lease_owner`` and ``state = 'leased'`` catch a lost lease. Neither catches the case the
    design names: the owner cancels while the model is running, ``cancel_requested`` flips to
    true on a row this worker still holds a valid lease on, and a two-predicate fence lets the
    completion — and with it the whole plan write, which shares the transaction — land anyway.

    So the assertion is against the REAL SQL: the harness's stub is swapped back out for
    ``complete_planning_job`` itself, which makes ``_FakeSession`` record the statement and,
    because a fake result has ``rowcount == 0``, return False on the way back. Stubbing the
    return value would have proved only that the worker honours a False it was handed.
    """
    monkeypatch.setattr(planning, "complete_planning_job", complete_planning_job)
    result = planning.run(JOB_ID)

    fences = [sql for sql, _ in harness["session"].executed if "UPDATE planning_jobs" in sql]
    assert fences, "the completion UPDATE never reached the session"
    fence = fences[-1]
    assert "lease_owner = :lease_owner" in fence
    assert "state = 'leased'" in fence
    assert "cancel_requested = false" in fence, (
        "a cancel that lands while the model runs must reject the completion; without this "
        "predicate the fence only catches a LOST lease (design p2_design.md:6214)"
    )

    assert result["status"] == "lost_lease"
    assert harness["session"].rolled_back >= 1
    assert harness["session"].committed == 0, "a rejected completion must commit nothing"


def test_objective_change_during_the_model_call_writes_nothing(harness: dict[str, Any]) -> None:
    """The precondition fires before any goal row exists, because lock 1 is taken first."""
    harness["revalidate_projection"] = _projection(contract_revision=2, objective_hash="h2", revision=4)
    result = planning.run(JOB_ID)
    assert result["status"] == "discarded"
    assert result["reason"] == PLANNING_STALE_REASON
    statements = [sql for sql, _ in harness["session"].executed]
    assert not any("INSERT INTO autonomy_goals" in sql for sql in statements)
    assert harness["session"].rolled_back >= 1
    assert harness["completions"][-1]["state"] == "discarded"


def test_unparseable_model_plan_fails_the_job(harness: dict[str, Any]) -> None:
    harness["gateway"] = _Gateway({"raw": "here is some prose instead of a plan"})
    result = planning.run(JOB_ID)
    assert result["status"] == "failed"
    assert result["reason"] == "unparseable_plan"
    assert not harness["plan_rows"]
    assert harness["completions"][-1]["producer"] != "deterministic"
    assert harness["completions"][-1]["producer"] is None


def test_model_off_is_a_failure_not_a_fallback(monkeypatch: pytest.MonkeyPatch, harness: dict[str, Any]) -> None:
    class _NoModel(_Settings):
        takeover_plan_llm_enabled = False

    monkeypatch.setattr(planning, "get_settings", lambda: _NoModel())
    result = planning.run(JOB_ID)
    assert result["status"] == "failed"
    assert not harness["plan_rows"]


def test_wrong_job_kind_is_skipped(harness: dict[str, Any]) -> None:
    harness["claimed"] = [_job_row(job_kind=PLANNING_JOB_KIND_DREAM)]
    assert planning.run(JOB_ID)["skipped"] == 1
    assert not harness["plan_rows"]


# --------------------------------------------------------------------------- backoff


def test_backoff_is_capped() -> None:
    settings = _Settings()
    assert planning._backoff(settings, 1, NOW) == NOW.replace(microsecond=0) + _seconds(5)
    assert planning._backoff(settings, 2, NOW) == NOW + _seconds(10)

    class _Many(_Settings):
        planning_job_backoff_cap_seconds = 900

    assert planning._backoff(_Many(), 40, NOW) == NOW + _seconds(900)


def _seconds(value: int):
    from datetime import timedelta

    return timedelta(seconds=value)


def test_terminal_attempt_has_no_next_attempt(harness: dict[str, Any]) -> None:
    harness["claimed"] = [_job_row(attempts=3, max_attempts=3)]
    harness["gateway"] = _Gateway(boom=RuntimeError("provider down"))
    result = planning.run(JOB_ID)
    assert result["status"] == "failed"
    assert harness["completions"][-1]["next_attempt_at"] is None


def test_transient_failure_schedules_a_retry(harness: dict[str, Any]) -> None:
    harness["claimed"] = [_job_row(attempts=1, max_attempts=3)]
    harness["gateway"] = _Gateway(boom=RuntimeError("provider down"))
    planning.run(JOB_ID)
    assert harness["completions"][-1]["next_attempt_at"] is not None


def test_replay_is_idempotent(harness: dict[str, Any]) -> None:
    assert planning.run(JOB_ID)["status"] == "succeeded"
    harness["claimed"] = []  # the row is no longer claimable
    assert planning.run(JOB_ID)["status"] == "skipped"
