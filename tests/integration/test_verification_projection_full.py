"""The graded verdict must reach the projection, or nothing can legitimately reach ``DONE``.

``R9`` (``shared/tce_shared/task_state.py``) is the only route to ``TaskStatus.DONE`` and it reads
one thing: the projection's ``latest_verification``, which is written only by a
``VERIFICATION_RECORDED`` task-state event.

Two halves have to hold at once, and before this file existed only the first did:

* the **report** path -- the implementing agent describing its own work in
  ``details["verification"]["state"]`` -- is pinned to ``unverified``, so an agent cannot declare
  its own task complete;
* the **evidence-graded** path -- ``decide_verdict`` behind ``POST /v1/verification/results``,
  under a bound verifier credential -- must emit that event with the GRADED state, stamped with
  the contract revision and plan id ``verification_is_current`` demands.

Measured on the live Postgres before the fix: the graded route returned ``verdict="passed"`` and
flipped ``directive_executions.verification_state`` to ``passed``, and the projection's
``latest_verification`` was still the report-derived row reading ``unverified`` -- the honest path
did not emit the event and the dishonest one was closed, so ``DONE`` was unreachable by any route.

The Lite half of this gate is
``tests/integration/test_task_state_lite.py::test_a_finished_plan_reaches_done_through_r9``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tce_api.auth import AuthContext
from tce_api.config import get_settings
from tce_api.db import get_session_factory
from tce_api.main import _record_execution_task_state, app
from tce_api.plan_rows import write_plan_rows
from tce_api.task_state_store import apply_task_state_events, ensure_task_state, load_task_state
from tce_shared.events import (
    AgentRole,
    AutonomyGoalStatus,
    DirectiveExecutionState,
    ExecutionReportRequest,
    TakeoverState,
)
from tce_shared.identity import credential_fingerprint
from tce_shared.scope import PROJECT_UNBOUND, ResolvedScope
from tce_shared.task_state import (
    PLANNING_PRODUCER_DETERMINISTIC,
    UNKNOWN_CONTRACT_REVISION,
    StatusInputs,
    TaskStateEvent,
    TaskStateEventKind,
    TaskStatus,
    approved_plan_id,
    charter_for_task,
    deterministic_plan,
    plan_steps_to_json,
    root_status,
    verification_is_current,
)
from tce_shared.verification import VERIFICATION_METHOD_EVIDENCE_GRADED, corpus_digest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("TCE_DATABASE_URL"),
        reason="Full-backend integration tests need TCE_DATABASE_URL",
    ),
]

_WORKSPACE = "p3-verification-projection-full"
_OWNER = "verification-projection-owner"
_EXECUTOR = "codex-executor"
_RUNNER = "system:verifier"
_VERIFIER_TOKEN = "verification-projection-verifier-token"
_OBJECTIVE = "wire the graded verdict into the projection"
_NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)

# The freeze route refuses a manifest that could not detect a build-deciding file swap.
_MANIFEST: list[list[str]] = [["pyproject.toml", "a" * 64]]
_MANIFEST_DIGEST = corpus_digest([("pyproject.toml", "a" * 64)])
_CHECK = {
    "check_id": "pytest",
    "argv": ["/bin/sh", "-c", "exit 0"],
    "cwd_rel": ".",
    "expect_exit_code": 0,
    "timeout_seconds": 60,
}


@pytest.fixture()
def db() -> Iterator[Session]:
    session = get_session_factory()()
    present = session.execute(text("SELECT to_regclass('verification_results')")).scalar()
    if present is None:
        session.rollback()
        session.close()
        pytest.skip("alembic revision 20260909_0039 has not been applied to this database")
    try:
        yield session
    finally:
        session.rollback()
        _purge(session)
        session.close()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """A bound verifier credential. ``tests/conftest.py`` restores the settings singleton."""
    settings = get_settings()
    settings.api_tokens = f"{settings.api_tokens},{_VERIFIER_TOKEN}".strip(",")
    settings.identity_claims_mode = "compat"
    settings.workspace_access_mode = "compat"
    settings.task_state_enabled = True
    settings.verification_enabled = True
    settings.verification_runner_principal = _RUNNER
    settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", _VERIFIER_TOKEN): {
                "consumer": _RUNNER,
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _OWNER,
                "behavior_subject_id": _OWNER,
            }
        }
    )
    yield TestClient(app)


def _purge(db: Session) -> None:
    for table in (
        "verification_results",
        "acceptance_criteria",
        "task_verifications",
        "task_state_events",
        "task_states",
        "autonomy_goals",
        "directive_executions",
    ):
        column = "user_id" if table == "directive_executions" else "workspace_id"
        value = _WORKSPACE if column == "workspace_id" else _OWNER
        try:
            db.execute(text(f"DELETE FROM {table} WHERE {column} = :v"), {"v": value})  # noqa: S608
        except Exception:  # pragma: no cover - a table this revision does not have
            db.rollback()
    db.commit()


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_VERIFIER_TOKEN}",
        "X-TCE-Consumer": _RUNNER,
        "X-TCE-Role": "user",
        "X-TCE-Workspace": _WORKSPACE,
        "X-TCE-User": _OWNER,
    }


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


def _auth() -> AuthContext:
    return AuthContext(
        consumer=_EXECUTOR,
        mode="server",
        role=AgentRole.EXECUTOR,
        workspace_id=_WORKSPACE,
        user_id=_OWNER,
    )


def _finished_plan(db: Session) -> tuple[str, str]:
    """A task whose every plan step is reported ``succeeded`` by the implementing agent itself.

    Every report carries ``details["verification"]["state"] = "passed"`` -- the agent's own claim
    about its own work. Returns ``(task_id, last_directive_id)``, committed.
    """
    task_id = f"task-{uuid.uuid4()}"
    ensure_task_state(
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
                payload={"objective_text": _OBJECTIVE, "objective_hash": "h-1"},
                occurred_at=_NOW,
                actor="owner",
            )
        ],
        now=_NOW,
        expected_revision=None,
    )
    steps = deterministic_plan(_OBJECTIVE, charter=charter_for_task(max_steps=3))
    root_id = write_plan_rows(
        db,
        scope=_scope(task_id),
        session_id=task_id,
        task_id=task_id,
        steps=steps,
        producer=PLANNING_PRODUCER_DETERMINISTIC,
        contract_revision=1,
        now=_NOW,
        objective_text=_OBJECTIVE,
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
                kind=TaskStateEventKind.PLAN_APPROVED,
                contract_revision=1,
                payload={
                    "producer": PLANNING_PRODUCER_DETERMINISTIC,
                    "root_goal_id": root_id,
                    "steps": plan_steps_to_json(steps),
                },
                occurred_at=_NOW,
                actor="planner",
            )
        ],
        now=_NOW,
        expected_revision=None,
    )
    goal_ids = {
        int(row["step_index"]): str(row["id"])
        for row in db.execute(
            text(
                """
                SELECT id, step_index FROM autonomy_goals
                 WHERE session_id = :s AND workspace_id = :w AND user_id = :u
                   AND parent_goal_id = CAST(:root AS uuid)
                """
            ),
            {"s": task_id, "w": _WORKSPACE, "u": _OWNER, "root": root_id},
        ).mappings()
    }
    assert goal_ids, "the plan must have written its step rows"

    state = TakeoverState(
        session_id=task_id,
        workspace_id=_WORKSPACE,
        user_id=_OWNER,
        updated_at=_NOW,
        takeover_context={"plan_root_goal_id": root_id, "plan_step_count": len(steps)},
    )
    directive_id = ""
    for index in sorted(goal_ids):
        directive_id = str(uuid.uuid4())
        _record_execution_task_state(
            db,
            auth=_auth(),
            state=state,
            body=ExecutionReportRequest(
                session_id=task_id,
                directive_id=uuid.UUID(directive_id),
                state=DirectiveExecutionState.SUCCEEDED,
                result="success",
                # The agent's own claim about its own work. R9 must not accept it.
                details={
                    "verification": {"state": "passed", "method": "tests", "summary": "ok"}
                },
            ),
            directive_id=directive_id,
            goal_id=goal_ids[index],
            effective_state=DirectiveExecutionState.SUCCEEDED,
            now=_NOW,
        )
        db.execute(
            text("UPDATE autonomy_goals SET status = :s WHERE id = CAST(:id AS uuid)"),
            {"s": AutonomyGoalStatus.DONE.value, "id": goal_ids[index]},
        )
    _seed_directive(db, task_id=task_id, directive_id=directive_id)
    db.commit()
    return task_id, directive_id


def _seed_directive(db: Session, *, task_id: str, directive_id: str) -> None:
    """The row ``record_verification`` locks. ``claimed_executor`` is NOT the verifier."""
    db.execute(
        text(
            """
            INSERT INTO directive_executions(
              directive_id, session_id, workspace_id, user_id, action_kind, attempt, state,
              requires_permit, meta, created_at, updated_at, lease_generation, claimed_executor,
              verification_state
            ) VALUES (
              CAST(:directive_id AS uuid), :session_id, :workspace_id, :user_id, 'takeover_step',
              1, 'succeeded', FALSE, CAST('{}' AS jsonb), :now, :now, 1, :executor, 'unverified'
            )
            """
        ),
        {
            "directive_id": directive_id,
            "session_id": task_id,
            "workspace_id": _WORKSPACE,
            "user_id": _OWNER,
            "now": _NOW + timedelta(minutes=1),
            "executor": _EXECUTOR,
        },
    )


def _projection(db: Session, task_id: str) -> Any:
    db.rollback()  # drop this session's snapshot so the API's commit is visible
    loaded = load_task_state(db, workspace_id=_WORKSPACE, owner_id=_OWNER, task_id=task_id)
    assert loaded is not None
    return loaded[0]


def _freeze(client: TestClient, directive_id: str) -> None:
    frozen = client.post(
        "/v1/verification/criteria",
        json={"directive_id": directive_id, "checks": [_CHECK], "corpus_manifest": _MANIFEST},
        headers=_headers(),
    )
    assert frozen.status_code == 200, frozen.text


def _grade(client: TestClient, directive_id: str, *, exit_code: int) -> dict[str, Any]:
    graded = client.post(
        "/v1/verification/results",
        json={
            "directive_id": directive_id,
            "results": [
                {
                    "check_id": "pytest",
                    "argv": ["/bin/sh", "-c", "exit 0"],
                    "exit_code": exit_code,
                    "duration_ms": 11,
                    "stdout_sha256": "b" * 64,
                    "stderr_sha256": "c" * 64,
                    "excerpt": "",
                }
            ],
            "observed_corpus_digest": _MANIFEST_DIGEST,
            "observed_corpus_manifest": _MANIFEST,
            "platform": "darwin/arm64 python3.12",
            "commit_sha": "d" * 40,
            "tree_sha": "e" * 40,
        },
        headers=_headers(),
    )
    assert graded.status_code == 200, graded.text
    body: dict[str, Any] = graded.json()
    return body


def test_a_graded_pass_is_what_moves_a_finished_plan_to_done(
    db: Session, client: TestClient
) -> None:
    task_id, directive_id = _finished_plan(db)

    # Every step is reported done, but the only verification on file is the agent's self-report,
    # so the task is held at AWAITING_VERIFICATION rather than DONE.
    self_reported = _projection(db, task_id)
    assert self_reported.plan is not None
    assert root_status(self_reported.plan.steps) == "done"
    assert self_reported.status is TaskStatus.AWAITING_VERIFICATION
    assert self_reported.latest_verification is not None
    assert self_reported.latest_verification.state == "unverified", (
        "the report path may not lift the implementing agent's own verdict"
    )

    _freeze(client, directive_id)
    body = _grade(client, directive_id, exit_code=0)
    assert body["verdict"] == "passed"
    assert body["verification_state"] == "passed"
    assert body["criteria_digest_match"] is True
    assert body["corpus_digest_match"] is True
    assert body["runner_principal"] == _RUNNER

    projection = _projection(db, task_id)
    assert projection.latest_verification is not None
    assert projection.latest_verification.state == "passed", (
        "the graded verdict never reached the projection, so nothing could reach DONE"
    )
    assert projection.latest_verification.method == VERIFICATION_METHOD_EVIDENCE_GRADED
    assert projection.latest_verification.directive_id == directive_id
    # The same S5 provenance the report path stamps, from the same helper.
    assert projection.plan is not None
    assert projection.latest_verification.contract_revision == projection.contract_revision
    assert projection.latest_verification.plan_id == projection.plan.plan_id
    assert projection.status is TaskStatus.DONE

    state = client.get(f"/v1/tasks/{task_id}/state", headers=_headers())
    assert state.status_code == 200, state.text
    assert state.json()["status"] == "done"
    assert state.json()["next_permitted_action"] == "none"


def test_a_verification_before_the_report_is_refused(db: Session, client: TestClient) -> None:
    """Design §7.4 step 1: ``is_terminal(row.state)`` must hold -- a verification only means
    anything after a report.

    Lite has enforced this since it shipped; Full did not, and the gap became load-bearing the
    moment a graded verdict started moving the projection. Measured before the guard: the same
    request that Lite answers ``409 directive_not_terminal`` returned ``200 passed`` on Full and
    drove ``latest_verification`` -- and therefore R9 -- to ``DONE`` on a directive still
    ``in_progress``, i.e. on work the implementing agent had not yet reported.
    """
    task_id, directive_id = _finished_plan(db)
    db.execute(
        text("UPDATE directive_executions SET state = 'in_progress' WHERE directive_id = CAST(:d AS uuid)"),
        {"d": directive_id},
    )
    db.commit()
    _freeze(client, directive_id)

    refused = client.post(
        "/v1/verification/results",
        json={
            "directive_id": directive_id,
            "results": [
                {
                    "check_id": "pytest",
                    "argv": ["/bin/sh", "-c", "exit 0"],
                    "exit_code": 0,
                    "duration_ms": 11,
                    "stdout_sha256": "b" * 64,
                    "stderr_sha256": "c" * 64,
                    "excerpt": "",
                }
            ],
            "observed_corpus_digest": _MANIFEST_DIGEST,
            "observed_corpus_manifest": _MANIFEST,
            "platform": "darwin/arm64 python3.12",
            "commit_sha": "d" * 40,
            "tree_sha": "e" * 40,
        },
        headers=_headers(),
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"] == "directive_not_terminal"

    # Nothing was written: not the verdict column, not the projection.
    verdict_state = db.execute(
        text("SELECT verification_state FROM directive_executions WHERE directive_id = CAST(:d AS uuid)"),
        {"d": directive_id},
    ).scalar()
    assert verdict_state == "unverified"
    projection = _projection(db, task_id)
    assert projection.latest_verification is not None
    assert projection.latest_verification.state == "unverified"
    assert projection.status is not TaskStatus.DONE


def test_a_graded_failure_reaches_the_projection_and_does_not_satisfy_r9(
    db: Session, client: TestClient
) -> None:
    """A failed verdict is evidence too: it must be visible, and it must not open the door."""
    task_id, directive_id = _finished_plan(db)

    _freeze(client, directive_id)
    body = _grade(client, directive_id, exit_code=1)
    assert body["verdict"] == "failed"
    assert body["verification_state"] == "failed"
    assert body["reason"].startswith("check_failed:pytest")

    projection = _projection(db, task_id)
    assert projection.plan is not None
    assert root_status(projection.plan.steps) == "done"
    assert projection.latest_verification is not None
    assert projection.latest_verification.state == "failed", (
        "a failed verdict must reach the projection, not be silently suppressed"
    )
    # Stamped exactly like the passing one -- current, and still not a pass.
    assert projection.latest_verification.contract_revision == projection.contract_revision
    assert projection.latest_verification.plan_id == projection.plan.plan_id
    assert verification_is_current(
        StatusInputs(
            cancelled_at=projection.cancelled_at,
            cancelled_seq=projection.cancelled_seq,
            objective_set_at=projection.objective_set_at,
            objective_set_seq=projection.objective_set_seq,
            objective_text=projection.objective_text,
            contract_revision=projection.contract_revision,
            open_decisions=projection.open_decisions,
            plan=projection.plan,
            unresolved_effects=projection.unresolved_effects,
            latest_verification=projection.latest_verification,
        )
    ) is True
    assert projection.status is TaskStatus.AWAITING_VERIFICATION
    assert projection.status is not TaskStatus.DONE


# --------------------------------------------------------------------------- the stamp


_OTHER_OBJECTIVE = "write the release notes instead"


def _status_inputs(projection: Any) -> StatusInputs:
    """The exact tuple ``derive_status`` folds, rebuilt from the stored projection.

    Asserting ``verification_is_current`` directly is what separates "R9 declined because the
    provenance is stale" from "R9 declined for some other reason it happens to share".
    """
    return StatusInputs(
        cancelled_at=projection.cancelled_at,
        cancelled_seq=projection.cancelled_seq,
        objective_set_at=projection.objective_set_at,
        objective_set_seq=projection.objective_set_seq,
        objective_text=projection.objective_text,
        contract_revision=projection.contract_revision,
        open_decisions=projection.open_decisions,
        plan=projection.plan,
        unresolved_effects=projection.unresolved_effects,
        latest_verification=projection.latest_verification,
    )


def _restate_and_finish(db: Session, task_id: str, *, later: datetime) -> None:
    """Restate the objective, approve the plan the bump demands, and report every step of it.

    The second objective is driven all the way to the position the first one reached — plan
    approved at the NEW contract revision, every step complete, no unresolved effects — so that
    when the first objective's verification finally lands, the provenance stamp is the only thing
    R9 has left to decide on.
    """
    steps = deterministic_plan(_OTHER_OBJECTIVE, charter=charter_for_task(max_steps=3))
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
                contract_revision=2,
                payload={"objective_text": _OTHER_OBJECTIVE, "objective_hash": "h-2"},
                occurred_at=later,
                actor="owner",
            ),
            TaskStateEvent(
                seq=0,
                kind=TaskStateEventKind.PLAN_APPROVED,
                contract_revision=2,
                payload={
                    "producer": PLANNING_PRODUCER_DETERMINISTIC,
                    "root_goal_id": str(uuid.uuid4()),
                    "steps": plan_steps_to_json(steps),
                },
                occurred_at=later,
                actor="planner",
            ),
            *[
                TaskStateEvent(
                    seq=0,
                    kind=TaskStateEventKind.STEP_COMPLETED,
                    contract_revision=2,
                    payload={"step_index": int(step.step_index)},
                    occurred_at=later,
                    actor=_EXECUTOR,
                    directive_id=str(uuid.uuid4()),
                )
                for step in steps
            ],
        ],
        now=later,
        expected_revision=None,
    )
    db.commit()


def test_a_verdict_graded_after_the_objective_moved_is_not_evidence_for_the_new_one(
    db: Session, client: TestClient
) -> None:
    """THE STAMP, Full's half. A verification in flight across an objective change is not re-badged.

    Reading the ref's ``contract_revision`` off the projection at grading time makes both sides of
    ``verification_is_current`` the same value, read from the same row in the same instant: the
    predicate then says "this verification is about whatever the contract is now", which is true
    of every verification and so excludes none. Here that would hand objective TWO a ``DONE`` on
    the strength of checks frozen against objective ONE, which nobody ran against objective two.

    Everything else R9 needs is deliberately already true for the second objective — its plan is
    approved at the new revision, every step is reported, there are no unresolved effects, and the
    arriving verdict is ``passed`` — so the stamp is the only thing left to decide the case.
    """
    task_id, stale_directive_id = _finished_plan(db)
    _freeze(client, stale_directive_id)

    before = _projection(db, task_id)
    assert before.plan is not None
    superseded_revision = before.contract_revision
    superseded_plan_id = before.plan.plan_id
    assert before.status is TaskStatus.AWAITING_VERIFICATION

    _restate_and_finish(db, task_id, later=_NOW + timedelta(minutes=5))
    second = _projection(db, task_id)
    assert second.contract_revision == superseded_revision + 1
    assert second.plan is not None
    assert second.plan.plan_id != superseded_plan_id
    assert root_status(second.plan.steps) == "done"
    assert second.status is TaskStatus.AWAITING_VERIFICATION

    # Only NOW does the first objective's verification land. It grades honestly, and passes.
    body = _grade(client, stale_directive_id, exit_code=0)
    assert body["verdict"] == "passed"
    assert body["verification_state"] == "passed"

    after = _projection(db, task_id)
    ref = after.latest_verification
    assert ref is not None
    # The event really did land with the graded state — dropping it is not how this case passes.
    assert ref.state == "passed"
    assert ref.method == VERIFICATION_METHOD_EVIDENCE_GRADED
    assert ref.directive_id == stale_directive_id
    # ...stamped with the contract the WORK happened under, not the one in force at grading.
    assert ref.contract_revision == superseded_revision
    assert ref.plan_id == superseded_plan_id
    assert verification_is_current(_status_inputs(after)) is False
    assert after.status is TaskStatus.AWAITING_VERIFICATION
    assert after.status is not TaskStatus.DONE

    state = client.get(f"/v1/tasks/{task_id}/state", headers=_headers())
    assert state.status_code == 200, state.text
    assert state.json()["status"] != "done"

    # The durable row agrees with the projection: one transaction, one stamp.
    row = db.execute(
        text(
            """
            SELECT contract_revision, plan_id FROM task_verifications
             WHERE directive_id = CAST(:d AS uuid) AND method = :m
            """
        ),
        {"d": stale_directive_id, "m": VERIFICATION_METHOD_EVIDENCE_GRADED},
    ).mappings().all()
    assert [int(entry["contract_revision"]) for entry in row] == [superseded_revision]
    assert [str(entry["plan_id"]) for entry in row] == [superseded_plan_id]


def test_a_verdict_for_a_superseded_plan_names_that_plan_not_the_live_one(
    db: Session, client: TestClient
) -> None:
    """The ref must NAME the plan it is evidence for, not merely fail to match the live one.

    A stamp that fell back to ``None`` or to the live plan's id would also make R9 decline, and
    would tell a reader nothing about which plan run the evidence belongs to. §S3.6 makes the plan
    id ``uuid5(task_id | contract_revision)``, so the superseded plan's id is recoverable from the
    revision the work happened under and the ref stays readable as history.
    """
    task_id, stale_directive_id = _finished_plan(db)
    _freeze(client, stale_directive_id)
    before = _projection(db, task_id)
    assert before.plan is not None
    superseded_revision = before.contract_revision
    superseded_plan_id = before.plan.plan_id

    _restate_and_finish(db, task_id, later=_NOW + timedelta(minutes=5))
    assert _grade(client, stale_directive_id, exit_code=0)["verdict"] == "passed"

    after = _projection(db, task_id)
    ref = after.latest_verification
    assert ref is not None
    assert ref.plan_id == superseded_plan_id
    assert ref.plan_id == approved_plan_id(task_id, superseded_revision)
    assert after.plan is not None
    assert ref.plan_id != after.plan.plan_id
    assert verification_is_current(_status_inputs(after)) is False


def test_a_verdict_arriving_after_a_cancel_and_revival_is_still_current(
    db: Session, client: TestClient
) -> None:
    """RULING: a stand-down and revival on the SAME objective does NOT stale a verification.

    Revival is deliberately not a contract change — ``objective_set_required`` accepts a same-hash
    ``OBJECTIVE_SET`` purely to clear the cancel, the revision does not move, no invalidation runs
    and the plan pointer stays put. The plan id is ``uuid5(task_id | contract_revision)``, so it is
    the same plan id at the same revision, and the completed steps the verification is evidence
    about are the same completed steps. Staling the evidence here would say a stand-down
    retroactively unmakes finished work, and would force every revived session to re-verify work
    nothing touched.

    Pinned in both directions: this fails if the stamp is later made epoch-sensitive without an
    argument for why revived work is unverified work.
    """
    task_id, directive_id = _finished_plan(db)
    _freeze(client, directive_id)
    before = _projection(db, task_id)
    assert before.plan is not None
    revision = before.contract_revision
    plan_id = before.plan.plan_id

    later = _NOW + timedelta(minutes=5)
    apply_task_state_events(
        db,
        workspace_id=_WORKSPACE,
        owner_id=_OWNER,
        session_id=task_id,
        task_id=task_id,
        new_events=[
            TaskStateEvent(
                seq=0,
                kind=TaskStateEventKind.CANCELLATION_REQUESTED,
                contract_revision=revision,
                payload={"reason": "stand_down"},
                occurred_at=later,
                actor="owner",
            ),
            # Revival: the SAME objective hash, so the contract revision does not move.
            TaskStateEvent(
                seq=0,
                kind=TaskStateEventKind.OBJECTIVE_SET,
                contract_revision=revision,
                payload={"objective_text": _OBJECTIVE, "objective_hash": "h-1"},
                occurred_at=later + timedelta(minutes=1),
                actor="owner",
            ),
        ],
        now=later,
        expected_revision=None,
    )
    db.commit()
    revived = _projection(db, task_id)
    assert revived.contract_revision == revision
    assert revived.plan is not None
    assert revived.plan.plan_id == plan_id

    assert _grade(client, directive_id, exit_code=0)["verdict"] == "passed"

    after = _projection(db, task_id)
    ref = after.latest_verification
    assert ref is not None
    assert ref.state == "passed"
    assert ref.contract_revision == revision
    assert ref.plan_id == plan_id
    assert verification_is_current(_status_inputs(after)) is True


def test_a_directive_with_no_projection_history_is_stamped_unknown_and_never_current(
    db: Session, client: TestClient
) -> None:
    """Fail-closed: a verification that cannot be tied to a contract is not evidence FOR one.

    A directive that left no ``task_state_events`` row — cancelled by an objective-change
    invalidation before it ever touched the projection, or reported while task state was off — has
    no recoverable contract of its own. Guessing "the current one" there is exactly the clock stamp
    the two cases above refuse, so the stamp is ``UNKNOWN_CONTRACT_REVISION``, which no real
    revision equals.
    """
    task_id, directive_id = _finished_plan(db)
    _freeze(client, directive_id)
    # Erase only THIS directive's trace on the projection; the projection itself stays intact.
    db.execute(
        text("UPDATE task_state_events SET directive_id = NULL WHERE directive_id = CAST(:d AS uuid)"),
        {"d": directive_id},
    )
    db.commit()

    assert _grade(client, directive_id, exit_code=0)["verdict"] == "passed"

    after = _projection(db, task_id)
    ref = after.latest_verification
    assert ref is not None
    assert ref.state == "passed"
    assert ref.contract_revision == UNKNOWN_CONTRACT_REVISION
    assert ref.plan_id is None
    assert verification_is_current(_status_inputs(after)) is False
    assert after.status is not TaskStatus.DONE


def test_criteria_cannot_be_frozen_for_a_directive_that_is_not_ours(
    db: Session, client: TestClient
) -> None:
    """Full's half of the freeze-scope check; Lite's is
    ``tests/integration/test_verification_freeze_order_lite.py``.

    The freeze is one-shot -- ``UNIQUE (directive_id)``, no UPDATE path, no DELETE path -- so the
    first freeze for a directive id wins permanently. Measured over HTTP before this check, on both
    backends: an ordinary bound caller froze ``/bin/sh -c "exit 0"`` against a directive id it had
    simply invented, for a directive that existed in no workspace, and got 200. That buys criteria
    for work someone else will do, and a pre-emptive 409 against the supervisor's own honest freeze.
    """
    invented = str(uuid.uuid4())
    refused = client.post(
        "/v1/verification/criteria",
        json={"directive_id": invented, "checks": [_CHECK], "corpus_manifest": _MANIFEST},
        headers=_headers(),
    )
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"]["error"] == "unknown_directive"
    db.rollback()
    stored = db.execute(
        text("SELECT id FROM acceptance_criteria WHERE directive_id = CAST(:directive_id AS uuid)"),
        {"directive_id": invented},
    ).first()
    assert stored is None
