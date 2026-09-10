"""The evidence-graded verification must reach the Lite task-state projection — every verdict.

``derive_status`` R9 is the ONLY route to ``DONE`` and it reads exactly one thing:
``latest_verification``, which only a ``VERIFICATION_RECORDED`` task-state event can move. The
execution report writes that event with its state pinned to ``'unverified'`` (an agent may not
grade its own work), so the evidence-graded route in ``verification_store.record_verification``
is the only producer of a state R9 can accept. ``test_task_state_lite`` covers the passing half.

This file covers the two ways a graded verification legitimately reaches the projection and still
does NOT reach ``DONE``, because "the event is emitted" and "R9 is satisfied" are separate claims
and conflating them is how a gate becomes a rubber stamp:

  1. a ``failed`` verdict — current provenance, wrong state
  2. a ``passed`` verdict whose contract revision has since been superseded — right state, stale
     provenance (S5)

Both assert the positive half too: the event really did land, with the graded state, so a green
test here can never be produced by simply dropping the event on the floor.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.db import _connect
from tce_lite_api.main import app
from tce_lite_api.task_state_store import load_task_state
from tce_shared.identity import credential_fingerprint
from tce_shared.task_state import (
    UNKNOWN_CONTRACT_REVISION,
    VERIFICATION_PASS_STATES,
    PlanState,
    StatusInputs,
    TaskStateProjection,
    TaskStatus,
    approved_plan_id,
    derive_status,
    root_status,
    verification_is_current,
)
from tce_shared.verification import corpus_digest

_TOKEN = "verify-projection-lite-token"
_VERIFIER_TOKEN = "verify-projection-lite-verifier-token"
_WORKSPACE = "personal"
_HUMAN = "human-1"
# One deciding file is enough for the manifest to be able to detect a build-config swap.
_MANIFEST: list[list[str]] = [["pyproject.toml", "a" * 64]]
_MANIFEST_DIGEST = corpus_digest([("pyproject.toml", "a" * 64)])
_MISSING = object()
_GUARDED_SETTINGS = (
    "lite_db_path",
    "api_tokens",
    "default_operation_mode",
    "identity_claims_mode",
    "identity_claims_json",
    "workspace_access_mode",
    "charter_enforcement_enabled",
    "verification_enabled",
    "verification_runner_principal",
    "task_state_enabled",
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
    return tmp_path / "verification-projection-lite.db"


@pytest.fixture()
def lite_client(db_path: Path) -> Iterator[TestClient]:
    snapshot = _snapshot_settings()
    settings = get_settings()
    settings.lite_db_path = str(db_path)
    settings.api_tokens = f"{_TOKEN},{_VERIFIER_TOKEN}"
    settings.default_operation_mode = "clone_advisor"
    # Pre-charter fixture, exactly as in test_task_state_lite: U2 would otherwise refuse the
    # mutating claim these scenarios need, and charter enforcement is covered in its own file.
    settings.charter_enforcement_enabled = False
    settings.identity_claims_mode = "compat"
    settings.workspace_access_mode = "compat"
    settings.verification_enabled = True
    settings.verification_runner_principal = "system:verifier"
    # The verifier carries its OWN bound credential: decide_verdict returns `inconclusive` when the
    # runner IS the executing identity, so without this the verdict under test is unreachable.
    settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", _VERIFIER_TOKEN): {
                "consumer": "system:verifier",
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _HUMAN,
                "behavior_subject_id": _HUMAN,
            }
        }
    )
    settings.task_state_enabled = True
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
        "X-TCE-Workspace": _WORKSPACE,
        "X-TCE-User": _HUMAN,
    }


def _verifier_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_VERIFIER_TOKEN}"}


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


def _projection(session_id: str) -> TaskStateProjection:
    """Observe the folded projection. Read-only; behaviour is always asserted over HTTP."""
    conn = _connect()
    try:
        loaded = load_task_state(
            conn, workspace_id=_WORKSPACE, owner_id=_HUMAN, task_id=session_id
        )
        assert loaded is not None
        return loaded[0]
    finally:
        conn.close()


def _status_inputs(projection: TaskStateProjection) -> StatusInputs:
    """The exact tuple ``derive_status`` folds, rebuilt from the stored projection.

    Asserting on ``verification_is_current`` directly is what separates "R9 declined because the
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


def _rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _drive_plan_to_done(client: TestClient, session_id: str) -> str:
    """Claim and report every step of the plan; returns the LAST directive id.

    Reporting alone leaves the task at ``AWAITING_VERIFICATION`` — R8/R10 are satisfied and R9 is
    the only thing left, which is exactly the position these cases need to measure.
    """
    _step(client, session_id)
    last_directive_id = ""
    for _ in range(3):
        body = _step(client, session_id, message="continue", task=None)
        directive_id = body["directive_id"]
        assert directive_id
        claim = client.post(
            "/v1/takeover/execution/claim",
            json={"session_id": session_id, "directive_id": directive_id, "action_kind": "execute"},
            headers=_headers(),
        )
        assert claim.status_code == 200, claim.text
        report = client.post(
            "/v1/takeover/execution/report",
            json={
                "session_id": session_id,
                "directive_id": directive_id,
                "state": "succeeded",
                "result": "success",
            },
            headers=_headers(),
        )
        assert report.status_code == 200, report.text
        last_directive_id = str(directive_id)
    return last_directive_id


def _freeze(client: TestClient, directive_id: str) -> None:
    response = client.post(
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
            "corpus_manifest": _MANIFEST,
        },
        headers=_verifier_headers(),
    )
    assert response.status_code == 200, response.text


def _grade(client: TestClient, directive_id: str, *, exit_code: int) -> dict[str, Any]:
    response = client.post(
        "/v1/verification/results",
        json={
            "directive_id": directive_id,
            "results": [
                {
                    "check_id": "test",
                    "argv": ["/bin/sh", "-c", "exit 0"],
                    "exit_code": exit_code,
                    "duration_ms": 9,
                    "stdout_sha256": "b" * 64,
                    "stderr_sha256": "c" * 64,
                    "excerpt": "",
                }
            ],
            "observed_corpus_digest": _MANIFEST_DIGEST,
            "observed_corpus_manifest": _MANIFEST,
            "platform": "darwin/arm64 python3.12.12",
            "commit_sha": "d" * 40,
            "tree_sha": "e" * 40,
        },
        headers=_verifier_headers(),
    )
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


# --------------------------------------------------------------------------- 1


def test_a_failed_graded_verdict_reaches_the_projection_and_is_not_done(
    lite_client: TestClient, db_path: Path
) -> None:
    """A ``failed`` verdict is recorded exactly as a passing one is — and R9 declines on state.

    The failure mode this rules out is a graded route that only bothers to tell the projection
    about good news: a projection that never hears "failed" keeps showing the last passing
    verification, and a re-run that regressed reads as still verified.
    """
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    directive_id = _drive_plan_to_done(lite_client, session_id)

    before = _projection(session_id)
    assert before.plan is not None
    assert root_status(before.plan.steps) == "done"
    assert before.status is TaskStatus.AWAITING_VERIFICATION

    _freeze(lite_client, directive_id)
    graded = _grade(lite_client, directive_id, exit_code=1)
    assert graded["verdict"] == "failed"
    assert graded["verification_state"] == "failed"
    # The reviewer-model label. It is not a gate, and it must not have suppressed the event below.
    assert graded["advisory"] is True

    after = _projection(session_id)
    ref = after.latest_verification
    assert ref is not None
    # The event landed, carrying the GRADED state rather than the report's pinned "unverified".
    assert ref.state == "failed"
    assert ref.directive_id == directive_id
    assert ref.method == "evidence_graded"
    # ...with S5 provenance intact, so the ONLY reason R9 declines is the state itself.
    assert after.plan is not None
    assert ref.contract_revision == after.contract_revision
    assert ref.plan_id == after.plan.plan_id
    assert verification_is_current(_status_inputs(after)) is True
    assert ref.state not in VERIFICATION_PASS_STATES

    assert root_status(after.plan.steps) == "done"
    assert after.status is TaskStatus.AWAITING_VERIFICATION
    state = lite_client.get(f"/v1/tasks/{session_id}/state", headers=_headers())
    assert state.status_code == 200, state.text
    assert state.json()["status"] == "awaiting_verification"
    assert state.json()["next_permitted_action"] == "await_verification"

    # The durable row and the projection agree: they were written in one transaction.
    stored = _rows(
        db_path,
        "SELECT state, method, contract_revision FROM task_verifications WHERE directive_id = ?",
        (directive_id,),
    )
    assert [row["state"] for row in stored] == ["failed"]
    assert stored[0]["method"] == "evidence_graded"
    assert int(stored[0]["contract_revision"]) == after.contract_revision


# --------------------------------------------------------------------------- 2


def test_a_passing_verdict_on_a_superseded_contract_does_not_reach_done(
    lite_client: TestClient,
) -> None:
    """S5: a ``passed`` verdict is evidence for the contract it was graded against, and no other.

    Without the stamp, the passing verification from a finished objective would still be sitting in
    ``latest_verification`` when the owner states the NEXT objective, and R9 would hand that wholly
    unverified objective a ``DONE`` on the strength of the previous one's evidence.
    """
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    directive_id = _drive_plan_to_done(lite_client, session_id)
    _freeze(lite_client, directive_id)
    assert _grade(lite_client, directive_id, exit_code=0)["verdict"] == "passed"

    # The graded verdict IS current here, so this half really does reach DONE.
    done = _projection(session_id)
    assert done.latest_verification is not None
    assert done.latest_verification.state == "passed"
    assert verification_is_current(_status_inputs(done)) is True
    assert done.status is TaskStatus.DONE
    superseded_revision = done.contract_revision

    # The owner restates the objective. The contract revision moves; the verification does not.
    revived = _step(lite_client, session_id, message="new objective", task=_OTHER_OBJECTIVE)
    assert revived["task_state"]["contract_revision"] == superseded_revision + 1

    after = _projection(session_id)
    ref = after.latest_verification
    assert ref is not None
    # Still visible as history, with its original stamp...
    assert ref.state == "passed"
    assert ref.contract_revision == superseded_revision
    # ...and no longer evidence for anything.
    assert after.contract_revision != ref.contract_revision
    assert verification_is_current(_status_inputs(after)) is False
    assert after.status is not TaskStatus.DONE
    state = lite_client.get(f"/v1/tasks/{session_id}/state", headers=_headers())
    assert state.status_code == 200, state.text
    assert state.json()["status"] != "done"


# --------------------------------------------------------------------------- 3


def test_a_verification_before_the_report_is_refused(lite_client: TestClient) -> None:
    """Design §7.4 step 1, the Lite half of the parity pair.

    A directive that has been claimed but not reported is still ``in_progress``: there is no work
    to verify yet, so grading it is refused rather than recorded. The twin case lives in
    ``test_verification_projection_full.py``; Full was missing this guard entirely, and a pass
    graded on unreported work reached the projection there.
    """
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    _step(lite_client, session_id)
    body = _step(lite_client, session_id, message="continue", task=None)
    directive_id = str(body["directive_id"])
    claim = lite_client.post(
        "/v1/takeover/execution/claim",
        json={"session_id": session_id, "directive_id": directive_id, "action_kind": "execute"},
        headers=_headers(),
    )
    assert claim.status_code == 200, claim.text
    _freeze(lite_client, directive_id)

    refused = lite_client.post(
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
            "observed_corpus_digest": _MANIFEST_DIGEST,
            "observed_corpus_manifest": _MANIFEST,
            "platform": "darwin/arm64 python3.12.12",
            "commit_sha": "d" * 40,
            "tree_sha": "e" * 40,
        },
        headers=_verifier_headers(),
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"] == "directive_not_terminal"

    after = _projection(session_id)
    assert after.status is not TaskStatus.DONE
    assert getattr(after.latest_verification, "state", None) != "passed"



# --------------------------------------------------------------------------- 4


def test_a_verdict_graded_after_the_objective_moved_is_not_evidence_for_the_new_one(
    lite_client: TestClient, db_path: Path
) -> None:
    """THE STAMP. A verification in flight across an objective change must not be re-badged.

    Case 2 covers the ordering the clock survives: grade, then restate — the ref was stamped
    before the revision moved, so it goes stale on its own. This is the ordering it does NOT
    survive: restate, THEN grade.

    Read the ref's ``contract_revision`` off the projection at grading time and both sides of
    ``verification_is_current`` become the same value, read from the same row, in the same
    instant. The predicate then says "this verification is about whatever the contract is now",
    which is true of every verification and therefore excludes none. The checks frozen against
    objective ONE would be evidence for objective TWO, which nobody ran them against.

    The counterfactual at the end is the part that makes this a measurement rather than an
    observation: the same real projection, with the same real ref re-stamped the way the clock
    would have stamped it and the plan's steps completed so R4-R8 are all satisfied, reaches
    ``DONE``. That is the false ``DONE`` the directive-sourced stamp is buying, stated as a fact
    about ``derive_status`` rather than as a claim in a comment.
    """
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    stale_directive_id = _drive_plan_to_done(lite_client, session_id)
    _freeze(lite_client, stale_directive_id)

    before = _projection(session_id)
    assert before.plan is not None
    superseded_revision = before.contract_revision
    superseded_plan_id = before.plan.plan_id
    assert root_status(before.plan.steps) == "done"
    assert before.status is TaskStatus.AWAITING_VERIFICATION

    # The owner restates the objective while the verification is still in flight.
    restated = _step(lite_client, session_id, message="new objective", task=_OTHER_OBJECTIVE)
    assert restated["task_state"]["contract_revision"] == superseded_revision + 1

    # Only NOW does the first objective's verification land. It grades honestly, and it passes.
    graded = _grade(lite_client, stale_directive_id, exit_code=0)
    assert graded["verdict"] == "passed"
    assert graded["verification_state"] == "passed"

    after = _projection(session_id)
    ref = after.latest_verification
    assert ref is not None
    # It really did reach the projection carrying the graded state — nothing was dropped, and
    # dropping it is not how this case is allowed to pass.
    assert ref.state == "passed"
    assert ref.state in VERIFICATION_PASS_STATES
    assert ref.directive_id == stale_directive_id
    assert ref.method == "evidence_graded"
    # ...stamped with the contract the WORK happened under, not the one in force at grading.
    assert after.contract_revision == superseded_revision + 1
    assert ref.contract_revision == superseded_revision
    assert ref.plan_id == superseded_plan_id
    assert verification_is_current(_status_inputs(after)) is False
    assert after.status is not TaskStatus.DONE
    state = lite_client.get(f"/v1/tasks/{session_id}/state", headers=_headers())
    assert state.status_code == 200, state.text
    assert state.json()["status"] != "done"

    # The durable row and the projection agree: one transaction, one stamp.
    stored = _rows(
        db_path,
        "SELECT contract_revision, plan_id FROM task_verifications"
        " WHERE directive_id = ? AND method = 'evidence_graded'",
        (stale_directive_id,),
    )
    assert [int(row["contract_revision"]) for row in stored] == [superseded_revision]
    assert [row["plan_id"] for row in stored] == [superseded_plan_id]

    # The counterfactual: what the clock stamp would have produced, over the real rule table.
    live_plan = replace(
        after.plan if after.plan is not None else before.plan,
        plan_id=approved_plan_id(session_id, after.contract_revision),
        contract_revision=after.contract_revision,
        state=PlanState.APPROVED,
        steps=tuple(replace(step, status="done") for step in before.plan.steps),
    )
    parts = replace(_status_inputs(after), plan=live_plan, unresolved_effects=())
    assert root_status(live_plan.steps) == "done"
    assert derive_status(parts)[0] is not TaskStatus.DONE
    clock_stamped = replace(
        ref, contract_revision=after.contract_revision, plan_id=live_plan.plan_id
    )
    assert derive_status(replace(parts, latest_verification=clock_stamped))[0] is TaskStatus.DONE


# --------------------------------------------------------------------------- 5


def test_a_verdict_for_a_superseded_plan_names_that_plan_not_the_live_one(
    lite_client: TestClient,
) -> None:
    """The ref must NAME the plan it is evidence for, not merely fail to match the live one.

    A stamp that fell back to ``None``, or to the live plan's id, would also make R9 decline —
    and would tell a reader nothing about which plan run the evidence belongs to, which is the
    whole reason the ref carries a plan id rather than a boolean. §S3.6 makes the plan id
    ``uuid5(task_id | contract_revision)``, so the superseded plan's id is recoverable from the
    revision the work happened under, and the ref stays readable as history.
    """
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    stale_directive_id = _drive_plan_to_done(lite_client, session_id)
    _freeze(lite_client, stale_directive_id)
    superseded = _projection(session_id)
    assert superseded.plan is not None
    superseded_plan_id = superseded.plan.plan_id
    superseded_revision = superseded.contract_revision

    _step(lite_client, session_id, message="new objective", task=_OTHER_OBJECTIVE)
    assert _grade(lite_client, stale_directive_id, exit_code=0)["verdict"] == "passed"

    after = _projection(session_id)
    ref = after.latest_verification
    assert ref is not None
    assert ref.plan_id == superseded_plan_id
    assert ref.plan_id == approved_plan_id(session_id, superseded_revision)
    assert after.plan is not None
    assert ref.plan_id != after.plan.plan_id
    assert verification_is_current(_status_inputs(after)) is False


# --------------------------------------------------------------------------- 6


def test_a_verdict_arriving_after_a_stand_down_and_revival_is_still_current(
    lite_client: TestClient,
) -> None:
    """RULING: a stand-down and revival on the SAME objective does NOT stale a verification.

    This is the third ordering worth asking about — the plan pointer moves through
    ``CANCELLATION_REQUESTED`` and back — and the answer is the opposite of cases 4 and 5.

    Revival is deliberately not a contract change: ``objective_set_required`` accepts a same-hash
    ``OBJECTIVE_SET`` purely to clear the cancel, the revision does not move, no invalidation
    runs, and the plan pointer stays put. The plan id is ``uuid5(task_id | contract_revision)``,
    so it is the SAME plan id at the same revision, and the completed steps the verification is
    evidence about are the same completed steps. Staling the evidence here would say a stand-down
    retroactively unmakes finished work; it does not, and it would force every revived session to
    re-verify work nothing changed.

    The test pins the ruling in both directions: it fails if someone later makes the stamp
    epoch-sensitive without first arguing why revived work is unverified work.
    """
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    directive_id = _drive_plan_to_done(lite_client, session_id)
    _freeze(lite_client, directive_id)
    before = _projection(session_id)
    assert before.plan is not None
    revision = before.contract_revision
    plan_id = before.plan.plan_id

    _step(lite_client, session_id, message="beru stand down", task=None)
    # Re-activate on the SAME objective: the hash is unchanged, so the contract does not move.
    _step(lite_client, session_id, message="beru take over", task=_OBJECTIVE)
    revived = _projection(session_id)
    assert revived.contract_revision == revision
    assert revived.plan is not None
    assert revived.plan.plan_id == plan_id

    assert _grade(lite_client, directive_id, exit_code=0)["verdict"] == "passed"

    after = _projection(session_id)
    ref = after.latest_verification
    assert ref is not None
    assert ref.state == "passed"
    assert ref.contract_revision == revision
    assert ref.plan_id == plan_id
    assert verification_is_current(_status_inputs(after)) is True


# --------------------------------------------------------------------------- 7


def test_a_directive_with_no_projection_history_is_stamped_unknown_and_never_current(
    lite_client: TestClient, db_path: Path
) -> None:
    """Fail-closed: a verification that cannot be tied to a contract is not evidence FOR one.

    A directive that left no ``task_state_events`` row — cancelled by an objective-change
    invalidation before it ever touched the projection, or reported while task state was off —
    has no recoverable contract of its own. Guessing "the current one" there is exactly the clock
    stamp cases 4 and 5 refuse, so the stamp is :data:`UNKNOWN_CONTRACT_REVISION`, which no real
    revision equals.
    """
    session_id = f"codex-{uuid.uuid4().hex[:8]}"
    directive_id = _drive_plan_to_done(lite_client, session_id)
    _freeze(lite_client, directive_id)
    # Erase only THIS directive's trace on the projection; the projection itself stays intact.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE task_state_events SET directive_id = NULL WHERE directive_id = ?",
            (directive_id,),
        )
        conn.commit()
    finally:
        conn.close()

    assert _grade(lite_client, directive_id, exit_code=0)["verdict"] == "passed"

    after = _projection(session_id)
    ref = after.latest_verification
    assert ref is not None
    assert ref.state == "passed"
    assert ref.contract_revision == UNKNOWN_CONTRACT_REVISION
    assert ref.plan_id is None
    assert verification_is_current(_status_inputs(after)) is False
    assert after.status is not TaskStatus.DONE
