"""P2 durable task state against the Lite HTTP boundary (design §10.3, Builder C).

Every case drives the real FastAPI app over ``TestClient`` with the real SQLite store. SQL is
used only to *observe* state, never as the assertion path for behaviour the HTTP boundary must
enforce.

Coverage notes, stated rather than implied:

* Every scenario of §10.3 is covered here. Scenarios 2, 3 and 7 drive
  ``store.report_execution``'s task-state emission (``STEP_COMPLETED`` / ``STEP_BLOCKED`` with
  the real ``autonomy_goals.step_index``) and the ``root_status``-gated root-done rule.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.db import _connect
from tce_lite_api.main import app
from tce_lite_api.task_state_store import (
    apply_task_state_events,
    begin_immediate_cas,
    end_immediate_cas,
    ensure_task_state,
    load_task_state,
    load_task_state_events,
    rebuild_task_state,
)
from tce_shared.identity import credential_fingerprint
from tce_shared.task_state import (
    MAX_TASK_STATE_EVENTS_PER_FOLD,
    PINNED_KINDS,
    READ_ONLY_DIAGNOSIS_TEMPLATE,
    TaskStateEvent,
    TaskStateEventKind,
    TaskStatePreconditionFailed,
    TaskStateProjection,
    TaskStateRevisionConflict,
    TaskStatus,
    canonical_json,
    fold_task_state,
    root_status,
)
from tce_shared.verification import corpus_digest

_TOKEN = "task-state-lite-token"
_VERIFIER_TOKEN = "task-state-lite-verifier-token"
# R9 needs a verification the IMPLEMENTING agent did not write, so the verifier carries its own
# bound credential. A bound claim resolves by credential fingerprint even in compat mode.
_VERIFY_MANIFEST: list[list[str]] = [["pyproject.toml", "a" * 64]]
_VERIFY_MANIFEST_DIGEST = corpus_digest([("pyproject.toml", "a" * 64)])
_MISSING = object()
_GUARDED_SETTINGS = (
    "lite_db_path",
    "api_tokens",
    "default_operation_mode",
    "identity_claims_mode",
    "identity_claims_json",
    "workspace_access_mode",
    "takeover_lease_strict",
    "scope_strict_tags",
    "takeover_turn_budget_ms",
    "task_state_enabled",
    "task_state_markdown_enabled",
    "task_state_markdown_max_steps",
    "planning_async_enabled",
    "planning_job_lease_seconds",
    "planning_job_max_attempts",
    "planning_job_batch_size",
    "planning_job_backoff_cap_seconds",
    "planning_pending_hint_ms",
    "retrieval_deadline_enabled",
    "retrieval_deadline_floor_ms",
    "retrieval_statement_floor_ms",
    "retrieval_advisor_min_ms",
    "sqlite_progress_instructions",
    "takeover_plan_max_steps",
)

_APP_CONTEXT: dict[str, Any] = {
    "domain": "coding",
    "project": "open-timeline-engine",
    "project_root": "/work/open-timeline-engine",
}
_OBJECTIVE = "audit the retrieval deadline wiring"
_OTHER_OBJECTIVE = "write the release notes for v0.4"


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
    return tmp_path / "task-state-lite.db"


@pytest.fixture()
def lite_client(db_path: Path) -> Iterator[TestClient]:
    snapshot = _snapshot_settings()
    settings = get_settings()
    settings.lite_db_path = str(db_path)
    settings.api_tokens = f"{_TOKEN},{_VERIFIER_TOKEN}"
    settings.default_operation_mode = "clone_advisor"
    # Pre-charter fixture. P3's U2 gate refuses a mutating claim without an active charter;
    # this file measures transitions that predate charters, so it pins the documented off
    # switch (design §0.7 / G6(d)) and its assertions keep measuring exactly what they did.
    # Enforcement ON is covered in tests/integration/test_charter_lite.py, both positions.
    settings.charter_enforcement_enabled = False
    settings.identity_claims_mode = "compat"
    settings.verification_enabled = True
    settings.verification_runner_principal = "system:verifier"
    settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", _VERIFIER_TOKEN): {
                "consumer": "system:verifier",
                "role": "user",
                "workspace_id": "personal",
                "user_id": "human-1",
                "behavior_subject_id": "human-1",
            }
        }
    )
    settings.workspace_access_mode = "compat"
    settings.task_state_enabled = True
    settings.task_state_markdown_enabled = True
    try:
        with TestClient(app) as client:
            yield client
    finally:
        _restore_settings(snapshot)


def _verifier_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_VERIFIER_TOKEN}"}


def _verify_directive(client: TestClient, directive_id: str) -> None:
    """Reach a passing verification the only way R9 accepts: frozen criteria graded against evidence."""

    frozen = client.post(
        "/v1/verification/criteria",
        json={
            "directive_id": directive_id,
            "checks": [
                {
                    "check_id": "test",
                    "argv": ["/bin/sh", "-c", "exit 0"],
                    "cwd_rel": ".",
                    "expect_exit_code": 0,
                    "timeout_seconds": 60,
                }
            ],
            "corpus_manifest": _VERIFY_MANIFEST,
        },
        headers=_verifier_headers(),
    )
    assert frozen.status_code == 200, frozen.text
    graded = client.post(
        "/v1/verification/results",
        json={
            "directive_id": directive_id,
            "results": [
                {
                    "check_id": "test",
                    "argv": ["/bin/sh", "-c", "exit 0"],
                    "exit_code": 0,
                    "duration_ms": 9,
                    "stdout_sha256": "b" * 64,
                    "stderr_sha256": "c" * 64,
                    "excerpt": "",
                }
            ],
            "observed_corpus_digest": _VERIFY_MANIFEST_DIGEST,
            "observed_corpus_manifest": _VERIFY_MANIFEST,
            "platform": "darwin/arm64 python3.12.12",
            "commit_sha": "d" * 40,
            "tree_sha": "e" * 40,
        },
        headers=_verifier_headers(),
    )
    assert graded.status_code == 200, graded.text


def _headers(consumer: str = "codex", user_id: str = "human-1") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": "executor",
        "X-TCE-Workspace": "personal",
        "X-TCE-User": user_id,
    }


def _step(
    client: TestClient,
    session_id: str,
    *,
    message: str = "beru take over",
    task: str | None = _OBJECTIVE,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message": message,
        "session_id": session_id,
        "persona_mode": "shadow",
        "app_context": dict(_APP_CONTEXT),
        "constraints": {"k": 4},
        "allow_fallback": True,
    }
    if task is not None:
        payload["task"] = task
    response = client.post("/v1/takeover/step", json=payload, headers=_headers())
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _claim(client: TestClient, session_id: str, directive_id: str) -> None:
    response = client.post(
        "/v1/takeover/execution/claim",
        json={"session_id": session_id, "directive_id": directive_id, "action_kind": "execute"},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text


def _report(
    client: TestClient,
    session_id: str,
    directive_id: str,
    state: str,
    *,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": session_id,
        "directive_id": directive_id,
        "state": state,
        "result": "success" if state == "succeeded" else "failure",
    }
    if failure_reason is not None:
        payload["failure_reason"] = failure_reason
    response = client.post("/v1/takeover/execution/report", json=payload, headers=_headers())
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _projection(session_id: str) -> TaskStateProjection:
    """Observe the folded projection. Read-only; behaviour is always asserted over HTTP."""
    conn = _connect()
    try:
        loaded = load_task_state(
            conn, workspace_id="personal", owner_id="human-1", task_id=session_id
        )
        assert loaded is not None
        return loaded[0]
    finally:
        conn.close()


def _event_kinds(session_id: str) -> list[str]:
    conn = _connect()
    try:
        loaded = load_task_state(
            conn, workspace_id="personal", owner_id="human-1", task_id=session_id
        )
        assert loaded is not None
        row = conn.execute(
            "SELECT id FROM task_states WHERE workspace_id = ? AND owner_id = ? AND task_id = ?",
            ("personal", "human-1", session_id),
        ).fetchone()
        assert row is not None
        return [str(event.kind) for event in load_task_state_events(conn, task_state_id=str(row[0]))]
    finally:
        conn.close()


# --------------------------------------------------------------------------- 1


def test_activation_writes_a_deterministic_inline_plan(lite_client: TestClient, db_path: Path) -> None:
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    body = _step(lite_client, session_id)

    # The turn did NOT 409: under a pinned expected_revision this exact turn returned 409
    # deterministically (R3-G1). The CAS reads the revision under its own lock instead.
    assert body["task_state_revision"] >= 1
    assert body["planning_pending"] is False
    assert body["task_state"]["contract_revision"] == 1
    assert body["task_state"]["plan_state"] == "approved"
    assert body["task_state"]["plan_producer"] == "deterministic"

    states = _rows(db_path, "SELECT revision, contract_revision, plan_state, plan_producer, objective_hash FROM task_states")
    assert len(states) == 1
    assert states[0]["revision"] == 1
    assert states[0]["plan_state"] == "approved"
    assert states[0]["plan_producer"] == "deterministic"

    jobs = _rows(db_path, "SELECT state, producer, queue_state, scope_json FROM planning_jobs")
    assert len(jobs) == 1
    assert jobs[0]["state"] == "succeeded"
    assert jobs[0]["producer"] == "deterministic"
    assert jobs[0]["queue_state"] == "inline"
    assert json.loads(jobs[0]["scope_json"])["workspace_id"] == "personal"

    steps = _rows(
        db_path,
        "SELECT step_index, title, mutating, plan_contract_revision, parent_goal_id FROM autonomy_goals "
        "WHERE session_id = ? AND step_index > 0 ORDER BY step_index",
        (session_id,),
    )
    assert [row["step_index"] for row in steps] == [1, 2, 3]
    assert all(row["parent_goal_id"] for row in steps)
    assert all(row["plan_contract_revision"] == 1 for row in steps)


# --------------------------------------------------------------------------- 7 (write path)


def test_deterministic_plan_writes_non_mutating_goal_rows(lite_client: TestClient, db_path: Path) -> None:
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    _step(lite_client, session_id)
    rows = _rows(
        db_path,
        "SELECT title, mutating FROM autonomy_goals WHERE session_id = ? AND step_index > 0 ORDER BY step_index",
        (session_id,),
    )
    assert rows, "the deterministic plan must have written step rows"
    template_titles = {title for title, _ in READ_ONLY_DIAGNOSIS_TEMPLATE}
    assert all(int(row["mutating"]) == 0 for row in rows)
    assert {row["title"] for row in rows} <= template_titles


# --------------------------------------------------------------------------- 4


def test_objective_change_bumps_the_contract_and_invalidates_the_plan(
    lite_client: TestClient, db_path: Path
) -> None:
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    first = _step(lite_client, session_id)
    assert first["task_state"]["contract_revision"] == 1

    second = _step(lite_client, session_id, message="new objective", task=_OTHER_OBJECTIVE)
    assert second["task_state"]["contract_revision"] == 2

    # The old plan's steps are still on disk but belong to the previous contract, so a step
    # selection for contract 2 can never reach them.
    by_contract = _rows(
        db_path,
        "SELECT plan_contract_revision, COUNT(1) AS n FROM autonomy_goals WHERE session_id = ? AND step_index > 0 "
        "GROUP BY plan_contract_revision ORDER BY plan_contract_revision",
        (session_id,),
    )
    assert [row["plan_contract_revision"] for row in by_contract] == [1, 2]

    jobs = _rows(db_path, "SELECT input_revision FROM planning_jobs WHERE task_id = ?", (session_id,))
    assert len({row["input_revision"] for row in jobs}) == 2


def test_step_advance_does_not_bump_the_contract(lite_client: TestClient) -> None:
    """R3-G3: a turn that states no new owner objective must leave contract_revision alone."""
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    first = _step(lite_client, session_id)
    assert first["task_state"]["contract_revision"] == 1
    # A follow-up turn carrying no `task` and no new-objective phrasing.
    second = _step(lite_client, session_id, message="continue", task=None)
    assert second["task_state"]["contract_revision"] == 1


# --------------------------------------------------------------------------- 5


def test_cancel_propagates(lite_client: TestClient, db_path: Path) -> None:
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    _step(lite_client, session_id)

    response = lite_client.post(
        f"/v1/tasks/{session_id}/cancel",
        json={"reason": "owner_cancelled"},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_id"] == session_id
    assert body["revision"] >= 2

    state = lite_client.get(f"/v1/tasks/{session_id}/state", headers=_headers())
    assert state.status_code == 200, state.text
    assert state.json()["status"] == "cancelled"
    assert state.json()["next_permitted_action"] == "none"

    jobs = _rows(db_path, "SELECT state, cancel_requested FROM planning_jobs WHERE task_id = ?", (session_id,))
    assert jobs and all(row["state"] in {"cancelled", "succeeded"} for row in jobs)


def test_cancelled_projection_blocks_dispatch(lite_client: TestClient, db_path: Path) -> None:
    """A cancelled task mints NOTHING (gate 2a).

    The status is read before the turn's directive decision, not after it. Before that read
    existed this exact turn wrote a brand new ``directive_executions`` row while the projection
    still read ``cancelled`` — asserting only ``status == "cancelled"`` never saw it, because the
    status was never the thing the dispatch decision consulted.
    """
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    first = _step(lite_client, session_id)
    assert first["directive_id"], "the uncancelled turn must dispatch, or this proves nothing"

    lite_client.post(
        f"/v1/tasks/{session_id}/cancel", json={"reason": "owner_cancelled"}, headers=_headers()
    )
    before = {
        row["directive_id"]
        for row in _rows(
            db_path, "SELECT directive_id FROM directive_executions WHERE session_id = ?", (session_id,)
        )
    }

    # No explicit task and no activation phrase: nothing on this turn asserts an objective, so
    # the cancellation stands.
    body = _step(lite_client, session_id, message="continue", task=None)
    assert body["task_state"]["status"] == "cancelled"
    assert body["task_state"]["contract_revision"] == 1
    assert body["planning_pending"] is False
    assert body["directive_id"] is None
    after = {
        row["directive_id"]
        for row in _rows(
            db_path, "SELECT directive_id FROM directive_executions WHERE session_id = ?", (session_id,)
        )
    }
    assert after == before, "a cancelled projection must not mint a new directive"


def test_cancel_then_same_objective_revives_the_task(lite_client: TestClient, db_path: Path) -> None:
    """RULING V1: re-activating on the SAME objective revives a cancelled task.

    With the objective hash unchanged no ``OBJECTIVE_SET`` used to be appended, so the last
    retained ``CANCELLATION_REQUESTED`` stayed newer than the last ``OBJECTIVE_SET`` forever and
    R1 returned ``CANCELLED`` for the life of the session — permanently dispatch-blocked, on the
    exact one-stable-session-id cycle ``CLAUDE.md`` mandates.
    """
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    _step(lite_client, session_id)
    lite_client.post(
        f"/v1/tasks/{session_id}/cancel", json={"reason": "owner_cancelled"}, headers=_headers()
    )
    assert (
        lite_client.get(f"/v1/tasks/{session_id}/state", headers=_headers()).json()["status"]
        == "cancelled"
    )

    body = _step(lite_client, session_id, message="beru take over", task=_OBJECTIVE)
    assert body["task_state"]["status"] != "cancelled"
    assert body["task_state"]["next_permitted_action"] != "none"
    # The objective did not change, so the contract does not move and no new plan is authored.
    assert body["task_state"]["contract_revision"] == 1
    assert body["planning_pending"] is False
    assert body["directive_id"], "revival must unblock dispatch, not merely clear the status"
    assert TaskStateEventKind.OBJECTIVE_SET in _event_kinds(session_id)


def test_stand_down_appends_cancellation_requested(lite_client: TestClient) -> None:
    """Gate 2b: BOTH Lite stand-down paths record the cancellation, as Full's twin does."""
    turn_session = f"codex-{uuid.uuid4().hex[:8]}"
    _step(lite_client, turn_session)
    stood_down = _step(lite_client, turn_session, message="beru stand down", task=None)
    assert stood_down["action"] == "stopped"
    assert TaskStateEventKind.CANCELLATION_REQUESTED in _event_kinds(turn_session)
    assert (
        lite_client.get(f"/v1/tasks/{turn_session}/state", headers=_headers()).json()["status"]
        == "cancelled"
    )

    reset_session = f"codex-{uuid.uuid4().hex[:8]}"
    _step(lite_client, reset_session)
    response = lite_client.post(
        "/v1/takeover/reset", params={"session_id": reset_session}, headers=_headers()
    )
    assert response.status_code == 200, response.text
    assert TaskStateEventKind.CANCELLATION_REQUESTED in _event_kinds(reset_session)
    assert (
        lite_client.get(f"/v1/tasks/{reset_session}/state", headers=_headers()).json()["status"]
        == "cancelled"
    )


def test_stand_down_then_reactivate_on_the_same_objective(lite_client: TestClient) -> None:
    """RULING V1 over the documented cycle: one session id, stand down, come back to the SAME
    objective. The task must be live and dispatch unblocked."""
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    _step(lite_client, session_id)
    _step(lite_client, session_id, message="beru stand down", task=None)
    assert (
        lite_client.get(f"/v1/tasks/{session_id}/state", headers=_headers()).json()["status"]
        == "cancelled"
    )

    revived = _step(lite_client, session_id, message="beru take over", task=_OBJECTIVE)
    assert revived["task_state"]["status"] != "cancelled"
    assert revived["task_state"]["next_permitted_action"] != "none"
    assert revived["task_state"]["contract_revision"] == 1
    assert revived["directive_id"]


# --------------------------------------------------------------------------- 6


def test_cas_conflict(db_path: Path, lite_client: TestClient) -> None:
    """G7, the primary reachable conflict: two connections, one expected_revision."""
    _step(lite_client, f"codex-{uuid.uuid4().hex[:8]}")  # force init_db + schema

    settings = get_settings()
    settings.lite_db_path = str(db_path)
    task_id = f"conflict-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)

    writer = _connect()
    other = _connect()
    try:
        begin_immediate_cas(writer)
        ensure_task_state(
            writer,
            workspace_id="personal",
            owner_id="human-1",
            subject_user_id="human-1",
            session_id=task_id,
            task_id=task_id,
            project_id=None,
            now=now,
        )
        end_immediate_cas(writer, ok=True)

        loaded = load_task_state(writer, workspace_id="personal", owner_id="human-1", task_id=task_id)
        assert loaded is not None
        stale_revision = loaded[0].revision

        event = TaskStateEvent(
            seq=0,
            kind=TaskStateEventKind.OBJECTIVE_SET,
            contract_revision=1,
            payload={"objective_text": _OBJECTIVE, "objective_hash": "h"},
            occurred_at=now,
            actor="codex",
        )
        begin_immediate_cas(other)
        apply_task_state_events(
            other,
            workspace_id="personal",
            owner_id="human-1",
            session_id=task_id,
            task_id=task_id,
            new_events=[event],
            now=now,
            expected_revision=stale_revision,
        )
        end_immediate_cas(other, ok=True)

        begin_immediate_cas(writer)
        try:
            with pytest.raises(TaskStateRevisionConflict) as excinfo:
                apply_task_state_events(
                    writer,
                    workspace_id="personal",
                    owner_id="human-1",
                    session_id=task_id,
                    task_id=task_id,
                    new_events=[event],
                    now=now,
                    expected_revision=stale_revision,
                    retry_once=False,
                )
        finally:
            end_immediate_cas(writer, ok=False)
        assert excinfo.value.expected_revision == stale_revision
        assert excinfo.value.actual_revision != stale_revision
        assert excinfo.value.actual_revision == stale_revision + 1

        # Third leg (G7, §10.3): the SAME stale precondition with retry_once=True re-reads under
        # the lock and asks `revalidate` whether the write is still valid. A False verdict must
        # raise TaskStatePreconditionFailed and leave the revision exactly where it was — the
        # branch that stops a retry from clobbering the write that beat it.
        seen: list[int] = []

        def _refuse(fresh: TaskStateProjection) -> tuple[bool, str]:
            seen.append(int(fresh.revision))
            return False, "objective_changed_again"

        begin_immediate_cas(writer)
        try:
            with pytest.raises(TaskStatePreconditionFailed) as failure:
                apply_task_state_events(
                    writer,
                    workspace_id="personal",
                    owner_id="human-1",
                    session_id=task_id,
                    task_id=task_id,
                    new_events=[event],
                    now=now,
                    expected_revision=stale_revision,
                    revalidate=_refuse,
                    retry_once=True,
                )
        finally:
            end_immediate_cas(writer, ok=False)
        assert failure.value.reason == "objective_changed_again"
        assert seen == [stale_revision + 1], "revalidate must see the FRESH revision"
        settled = load_task_state(writer, workspace_id="personal", owner_id="human-1", task_id=task_id)
        assert settled is not None
        assert settled[0].revision == stale_revision + 1
    finally:
        writer.close()
        other.close()


# --------------------------------------------------------------------------- 2, 3


def test_blocked_step_never_completes_the_root_goal(
    lite_client: TestClient, db_path: Path
) -> None:
    """G6, live half: step 1 succeeds, step 2 blocks, and NOTHING reaches done.

    Three separate defects meet here. ``report_execution`` emitted no task-state events at all,
    so every step of every Lite projection stayed ``candidate``; the events it now emits carry
    the real ``autonomy_goals.step_index`` rather than a placeholder the fold discards; and the
    root-done decision consults ``root_status`` instead of treating "no next step" as "finished".
    """
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    _step(lite_client, session_id)
    second = _step(lite_client, session_id, message="continue", task=None)
    first_directive = second["directive_id"]
    assert first_directive

    _claim(lite_client, session_id, first_directive)
    _report(lite_client, session_id, first_directive, "succeeded")

    done_projection = _projection(session_id)
    assert done_projection.plan is not None
    assert [(step.step_index, step.status) for step in done_projection.plan.steps][0] == (1, "done")
    assert root_status(done_projection.plan.steps) == "active"

    third = _step(lite_client, session_id, message="continue", task=None)
    second_directive = third["directive_id"]
    assert second_directive and second_directive != first_directive
    _claim(lite_client, session_id, second_directive)
    _report(
        lite_client,
        session_id,
        second_directive,
        "failed",
        failure_reason="the upstream dependency is unavailable",
    )

    projection = _projection(session_id)
    assert projection.plan is not None
    statuses = {int(step.step_index): str(step.status) for step in projection.plan.steps}
    assert statuses[1] == "done"
    assert statuses[2] == "blocked", "the STEP_BLOCKED payload must carry the real step_index"
    assert statuses[3] == "candidate", "a blocked step must not be skipped over"
    assert root_status(projection.plan.steps) == "blocked"

    # (a) the root goal row is NOT done
    root_rows = _rows(
        db_path,
        "SELECT status FROM autonomy_goals WHERE session_id = ? AND step_index = 0",
        (session_id,),
    )
    assert root_rows and all(row["status"] != "done" for row in root_rows)
    assert not [
        row
        for row in _rows(
            db_path,
            "SELECT status FROM autonomy_goals WHERE session_id = ? AND step_index = 3",
            (session_id,),
        )
        if row["status"] == "selected"
    ], "the turn after the block must not select the next floor"

    # (b) the wire reports BLOCKED
    state = lite_client.get(f"/v1/tasks/{session_id}/state", headers=_headers())
    assert state.status_code == 200, state.text
    assert state.json()["status"] == "blocked"
    assert state.json()["next_permitted_action"] == "blocked"


def test_a_finished_plan_reaches_done_through_r9(lite_client: TestClient, db_path: Path) -> None:
    """The positive half of the same rule: root_status "done" DOES mark the root goal done.

    R9 is the only route to ``DONE`` and it needs a verification stamped with THIS contract and
    THIS plan (S5), which is what ``report_execution`` now writes.
    """
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    _step(lite_client, session_id)
    for _ in range(3):
        body = _step(lite_client, session_id, message="continue", task=None)
        directive_id = body["directive_id"]
        assert directive_id
        _claim(lite_client, session_id, directive_id)
        response = lite_client.post(
            "/v1/takeover/execution/report",
            json={
                "session_id": session_id,
                "directive_id": directive_id,
                "state": "succeeded",
                "result": "success",
                # The agent's own claim about its own work. R9 must not accept it.
                "details": {
                    "verification": {"state": "passed", "method": "tests", "summary": "ok"}
                },
            },
            headers=_headers(),
        )
        assert response.status_code == 200, response.text
        last_directive_id = directive_id

    # Every step is reported done, but the only verification on file is the agent's self-report, so the
    # task is held at AWAITING_VERIFICATION rather than DONE.
    self_reported = _projection(session_id)
    assert self_reported.plan is not None
    assert root_status(self_reported.plan.steps) == "done"
    assert self_reported.status is TaskStatus.AWAITING_VERIFICATION
    assert self_reported.latest_verification is not None
    assert self_reported.latest_verification.state == "unverified"

    # Grade the frozen criteria against recorded evidence, under the verifier's own bound credential.
    _verify_directive(lite_client, last_directive_id)

    projection = _projection(session_id)
    assert projection.plan is not None
    assert root_status(projection.plan.steps) == "done"
    assert projection.latest_verification is not None
    assert projection.latest_verification.contract_revision == projection.contract_revision
    assert projection.latest_verification.plan_id == projection.plan.plan_id

    root_rows = _rows(
        db_path,
        "SELECT status FROM autonomy_goals WHERE session_id = ? AND step_index = 0",
        (session_id,),
    )
    assert root_rows and all(row["status"] == "done" for row in root_rows)

    state = lite_client.get(f"/v1/tasks/{session_id}/state", headers=_headers())
    assert state.json()["status"] == "done"
    assert state.json()["next_permitted_action"] == "none"
    # And a finished plan dispatches nothing further.
    assert _step(lite_client, session_id, message="continue", task=None)["directive_id"] is None


# --------------------------------------------------------------------------- 8


def test_state_markdown_carries_provenance(lite_client: TestClient) -> None:
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    _step(lite_client, session_id)
    response = lite_client.get(f"/v1/tasks/{session_id}/state.md", headers=_headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["read_only"] is True
    assert body["projection_learning_eligible"] is False
    assert body["mime_type"] == "text/markdown; charset=utf-8"
    assert isinstance(body["source_evidence_ids"], list)
    assert body["content"]
    # content_sha256 is over the BODY only, not the frontmatter (§S3.11), so this asserts the
    # shape and non-emptiness rather than re-deriving a digest over the wrong span.
    assert len(body["content_sha256"]) == 64
    assert int(body["content_sha256"], 16) >= 0


def test_state_markdown_is_gated(lite_client: TestClient) -> None:
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    _step(lite_client, session_id)
    get_settings().task_state_markdown_enabled = False
    try:
        response = lite_client.get(f"/v1/tasks/{session_id}/state.md", headers=_headers())
        assert response.status_code == 404
    finally:
        get_settings().task_state_markdown_enabled = True


# --------------------------------------------------------------------------- 9


def test_stand_down_then_reactivate(lite_client: TestClient) -> None:
    """R4/C1-G14: a cancelled task must be revived by a later objective, not bricked."""
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    _step(lite_client, session_id)
    lite_client.post(f"/v1/tasks/{session_id}/cancel", json={"reason": "stand_down"}, headers=_headers())

    revived = _step(lite_client, session_id, message="new objective", task=_OTHER_OBJECTIVE)
    assert revived["task_state"]["status"] != "cancelled"
    assert revived["task_state"]["next_permitted_action"] != "none"
    assert revived["task_state"]["contract_revision"] == 2


# --------------------------------------------------------------------------- 10


def test_cancel_then_resume_is_not_deadlocked(lite_client: TestClient, db_path: Path) -> None:
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    _step(lite_client, session_id)
    before = {row["id"] for row in _rows(db_path, "SELECT id FROM planning_jobs WHERE task_id = ?", (session_id,))}
    lite_client.post(f"/v1/tasks/{session_id}/cancel", json={"reason": "owner_cancelled"}, headers=_headers())

    resumed = _step(lite_client, session_id, message="new objective", task=_OTHER_OBJECTIVE)
    assert resumed["planning_pending"] is False
    after = {row["id"] for row in _rows(db_path, "SELECT id FROM planning_jobs WHERE task_id = ?", (session_id,))}
    # A NEW row, not the cancelled one revived: the cancel epoch changed the idempotency key.
    assert after - before


# --------------------------------------------------------------------------- 11


def test_begin_immediate_brackets_only_the_cas_section(lite_client: TestClient, db_path: Path) -> None:
    settings = get_settings()
    settings.lite_db_path = str(db_path)
    _step(lite_client, f"codex-{uuid.uuid4().hex[:8]}")

    conn = _connect()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        # The turn's early bookkeeping DML, then the bracket. Under the naive rule this raises
        # "cannot start a transaction within a transaction".
        conn.execute("DELETE FROM autonomy_goal_cache WHERE session_id = ?", ("nobody",))
        begin_immediate_cas(conn)
        ok = False
        try:
            conn.execute("SELECT COUNT(1) FROM task_states").fetchone()
            ok = True
        finally:
            end_immediate_cas(conn, ok=ok)
    finally:
        conn.set_trace_callback(None)
        conn.close()

    immediates = [item for item in statements if item.strip().upper().startswith("BEGIN IMMEDIATE")]
    assert len(immediates) == 1
    index = statements.index(immediates[0])
    assert statements[index - 1].strip().upper().startswith("COMMIT")


def test_begin_immediate_after_dml_does_not_raise(lite_client: TestClient, db_path: Path) -> None:
    settings = get_settings()
    settings.lite_db_path = str(db_path)
    _step(lite_client, f"codex-{uuid.uuid4().hex[:8]}")
    conn = _connect()
    try:
        conn.execute("UPDATE autonomy_notices SET acknowledged_at = NULL WHERE 0 = 1")
        assert conn.in_transaction
        begin_immediate_cas(conn)  # must not raise
        end_immediate_cas(conn, ok=False)
    finally:
        conn.close()


# --------------------------------------------------------------------------- 12


@pytest.mark.parametrize("pinned_kinds", [1, 3])
def test_truncation_window_matches_the_fold(lite_client: TestClient, db_path: Path, pinned_kinds: int) -> None:
    """R2-G16, the Lite half: the SQL window and the pure fold must agree."""
    settings = get_settings()
    settings.lite_db_path = str(db_path)
    _step(lite_client, f"codex-{uuid.uuid4().hex[:8]}")

    task_id = f"trunc-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    conn = _connect()
    try:
        begin_immediate_cas(conn)
        task_state_id, _projection, _seq, _rev = ensure_task_state(
            conn,
            workspace_id="personal",
            owner_id="human-1",
            subject_user_id="human-1",
            session_id=task_id,
            task_id=task_id,
            project_id=None,
            now=now,
        )
        end_immediate_cas(conn, ok=True)

        # OBJECTIVE_SET first, so the one-pinned-kind fixture is the interesting one: the
        # objective must survive a window that otherwise keeps only the tail.
        ordered_pinned = [str(TaskStateEventKind.OBJECTIVE_SET)] + sorted(
            str(kind) for kind in PINNED_KINDS if str(kind) != str(TaskStateEventKind.OBJECTIVE_SET)
        )
        pinned_values = ordered_pinned[:pinned_kinds]
        rows: list[tuple[Any, ...]] = []
        for seq in range(1, 3001):
            if seq <= len(pinned_values):
                kind = pinned_values[seq - 1]
                payload = canonical_json({"objective_text": _OBJECTIVE, "objective_hash": "h"})
            else:
                kind = str(TaskStateEventKind.STEP_COMPLETED)
                payload = canonical_json({"step_index": seq})
            rows.append(
                (
                    str(uuid.uuid4()),
                    task_state_id,
                    "personal",
                    "human-1",
                    task_id,
                    seq,
                    kind,
                    1,
                    payload,
                    None,
                    None,
                    None,
                    "codex",
                    now.isoformat(),
                    "v1",
                )
            )
        conn.executemany(
            "INSERT INTO task_state_events (id, task_state_id, workspace_id, owner_id, task_id, seq, kind, "
            "contract_revision, payload_json, source_event_id, directive_id, goal_id, actor, occurred_at, "
            "schema_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()

        windowed = load_task_state_events(conn, task_state_id=task_state_id)
        folded_window = fold_task_state(
            windowed,
            task_id=task_id,
            workspace_id="personal",
            owner_id="human-1",
            session_id=task_id,
            revision=1,
            now=now,
        )
        rebuilt = rebuild_task_state(conn, workspace_id="personal", owner_id="human-1", task_id=task_id)
        assert rebuilt is not None
        assert rebuilt.source_revision == folded_window.source_revision

        assert len(windowed) <= MAX_TASK_STATE_EVENTS_PER_FOLD + len(PINNED_KINDS)
        if pinned_kinds == 1:
            # The pinned OBJECTIVE_SET at seq=1 survives a window that keeps only the tail.
            # With all three pinned kinds present the later OBJECTIVE_CLEARED legitimately
            # clears it, which is fold rule 6 working, not truncation losing the objective.
            assert folded_window.projection.objective_text == _OBJECTIVE
        assert folded_window.projection.last_cancel_seq == (2 if pinned_kinds == 3 else 0)
    finally:
        conn.close()


# --------------------------------------------------------------------------- 13


def test_lite_plan_row_matches_full(lite_client: TestClient, db_path: Path) -> None:
    """The pinned §S9.1 column values, asserted field-for-field on the Lite rows.

    Full's twin writes the identical values; this half is what makes the two backends' plan
    tables diffable without running Postgres.
    """
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    _step(lite_client, session_id)
    rows = _rows(
        db_path,
        "SELECT source, priority_score, selection_score, risk_tier, confidence, reasoning, "
        "evidence_event_ids, goal_kind, affective_scores, cache_hit, cache_source, status, "
        "attempt_count, blocked_reason, depends_on_json, mutating, step_index "
        "FROM autonomy_goals WHERE session_id = ? ORDER BY step_index",
        (session_id,),
    )
    assert rows
    root = rows[0]
    assert root["step_index"] == 0
    assert root["status"] == "selected"
    assert root["source"] == "user_objective"
    assert root["priority_score"] == pytest.approx(0.9)
    assert root["selection_score"] == pytest.approx(0.9)
    assert root["risk_tier"] == "medium"
    assert root["confidence"] == pytest.approx(0.85)
    assert root["reasoning"] == "Ordered plan step derived from the user objective."
    assert root["evidence_event_ids"] == "[]"
    assert root["goal_kind"] == "normal"
    assert root["affective_scores"] == "{}"
    assert int(root["cache_hit"]) == 0
    assert root["cache_source"] == "plan"
    assert root["depends_on_json"] == "[]"
    assert int(root["mutating"]) == 0

    for index, step in enumerate(rows[1:], start=1):
        assert step["step_index"] == index
        assert step["status"] == "candidate"
        assert int(step["attempt_count"]) == 0
        assert step["blocked_reason"] == ""
        assert int(step["mutating"]) == 0
        assert json.loads(step["depends_on_json"]) == ([index - 1] if index > 1 else [])
