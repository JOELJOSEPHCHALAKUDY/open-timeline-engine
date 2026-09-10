"""The FIFTH door: an agent that freezes its own goalposts by choosing when to freeze them.

``POST /v1/verification/criteria`` refuses a freezer whose consumer matches the directive's
``dispatch_records.claimed_by`` or its ``directive_executions.claimed_executor``.  That check reads
the claim state **as it stands at freeze time**, and the order of "freeze" and "claim" is the
implementing agent's own choice: freeze first, and there is no claim row for the guard to see.

Measured over real HTTP against a live ``uvicorn`` on both backends before the fix, using one
server-bound executor credential and nothing else:

    POST /v1/takeover/step        -> directive_id            (unclaimed)
    POST /v1/verification/criteria -> 200                    <-- own checks: /bin/sh -c "exit 0"
    POST /v1/takeover/execution/claim  -> 200                (claimed_executor = codex-executor)
    POST /v1/takeover/execution/report -> 200  succeeded
    POST /v1/verification/results (HONEST verifier)          -> verdict "passed"
    GET  /v1/tasks/<id>/state                                -> "status": "done"

Nothing in that sequence is forged: the verifier is a genuinely independent bound principal and it
really did run the checks.  The checks were simply written by the party under test.

The fix is in ``decide_verdict`` -- shared by both backends, so parity is structural -- because
grading is the first moment at which BOTH ``criteria.frozen_by`` and the directive's
``claimed_executor`` are recorded and settled, and no ordering of the agent's own calls can reach
it.  The verdict becomes ``inconclusive`` ("nobody independent chose these checks"), which maps to
``unverified``, which R9 refuses; the agent's own honest-looking evidence therefore holds the task
at ``AWAITING_VERIFICATION`` instead of releasing it.

This file drives the whole sequence through the Lite HTTP boundary.  The pure-grader half is
``tests/unit/test_verification_criteria.py::test_criteria_frozen_by_the_executing_identity_is_inconclusive``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.main import app
from tce_shared.identity import credential_fingerprint
from tce_shared.verification import corpus_digest

pytestmark = pytest.mark.integration

# The implementing agent's OWN credential, and it is server-bound: this attack does not need a
# forged header, a stolen verifier token or a second identity.  It needs one ordinary bound
# executor credential and the freedom to choose what order to call two routes in.
_AGENT_TOKEN = "freeze-order-agent-token"
_VERIFIER_TOKEN = "freeze-order-verifier-token"
_AGENT = "codex-executor"
_VERIFIER = "system:verifier"
_HUMAN = "freeze-order-human"
_WORKSPACE = "personal"
_SESSION_TASK = "update the changelog notes"
_APP_CONTEXT: dict[str, Any] = {
    "domain": "coding",
    "project": "open-timeline-engine",
    "project_root": "/work/open-timeline-engine",
}
_MANIFEST: list[list[str]] = [["pyproject.toml", "a" * 64]]
_MANIFEST_DIGEST = corpus_digest([("pyproject.toml", "a" * 64)])
# The whole point: a check the agent knows passes, whatever it did or did not build.
_ARGV = ["/bin/sh", "-c", "exit 0"]


@pytest.fixture()
def lite_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = get_settings()
    settings.lite_db_path = str(tmp_path / "freeze-order-lite.db")
    settings.api_tokens = f"{_AGENT_TOKEN},{_VERIFIER_TOKEN}"
    settings.allow_default_token = False
    settings.default_operation_mode = "clone_advisor"
    settings.identity_claims_mode = "compat"
    settings.workspace_access_mode = "compat"
    settings.charter_enforcement_enabled = False
    settings.verification_enabled = True
    settings.verification_runner_principal = _VERIFIER
    settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", _AGENT_TOKEN): {
                "consumer": _AGENT,
                "role": "executor",
                "workspace_id": _WORKSPACE,
                "user_id": _HUMAN,
                "behavior_subject_id": _HUMAN,
            },
            credential_fingerprint("bearer", _VERIFIER_TOKEN): {
                "consumer": _VERIFIER,
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _HUMAN,
                "behavior_subject_id": _HUMAN,
            },
        }
    )
    # The repo-wide ``_restore_settings`` fixture puts every one of the above back afterwards.
    with TestClient(app) as client:
        yield client


def _agent_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_AGENT_TOKEN}"}


def _verifier_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_VERIFIER_TOKEN}"}


def _rows(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(get_settings().lite_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _open_directive(client: TestClient, session_id: str) -> str:
    permit = client.post(
        "/v1/takeover/permit",
        json={
            "session_id": session_id,
            "action_kind": "edit",
            "target_paths": ["docs/notes.md"],
            "estimated_change_size": 5,
        },
        headers=_agent_headers(),
    )
    assert permit.status_code == 200, permit.text
    step = client.post(
        "/v1/takeover/step",
        json={
            "message": "beru take over",
            "session_id": session_id,
            "persona_mode": "shadow",
            "task": _SESSION_TASK,
            "app_context": _APP_CONTEXT,
            "allow_fallback": True,
        },
        headers=_agent_headers(),
    )
    assert step.status_code == 200, step.text
    return str(step.json()["directive_id"])


def _claim_and_report(client: TestClient, session_id: str, directive_id: str) -> None:
    claim = client.post(
        "/v1/takeover/execution/claim",
        json={"session_id": session_id, "directive_id": directive_id},
        headers=_agent_headers(),
    )
    assert claim.status_code == 200, claim.text
    report = client.post(
        "/v1/takeover/execution/report",
        json={
            "session_id": session_id,
            "directive_id": directive_id,
            "state": "succeeded",
            "lease_generation": int(claim.json()["lease_generation"]),
            "details": {"verification": {"build": True, "test": True, "lint": True}},
        },
        headers=_agent_headers(),
    )
    assert report.status_code == 200, report.text


def _grade(client: TestClient, directive_id: str) -> dict[str, Any]:
    """An HONEST verifier: an independent bound principal, posting the real exit codes."""
    graded = client.post(
        "/v1/verification/results",
        json={
            "directive_id": directive_id,
            "results": [
                {
                    "check_id": "test",
                    "argv": _ARGV,
                    "exit_code": 0,
                    "duration_ms": 7,
                    "stdout_sha256": "b" * 64,
                    "stderr_sha256": "c" * 64,
                    "excerpt": "",
                }
            ],
            "observed_corpus_digest": _MANIFEST_DIGEST,
            "observed_corpus_manifest": _MANIFEST,
            "platform": "darwin/arm64 python3.12",
        },
        headers=_verifier_headers(),
    )
    assert graded.status_code == 200, graded.text
    body: dict[str, Any] = graded.json()
    return body


def test_freezing_before_claiming_does_not_buy_a_pass(lite_client: TestClient) -> None:
    """The exact sequence measured over HTTP, end to end, with the agent's own credential."""
    session_id = f"freeze-order-{uuid.uuid4().hex[:8]}"
    directive_id = _open_directive(lite_client, session_id)

    # The route cannot refuse this: there is no dispatch record and claimed_executor is still NULL,
    # so there is no claim for the freeze-time guard to compare the freezer against.
    frozen = lite_client.post(
        "/v1/verification/criteria",
        json={
            "directive_id": directive_id,
            "checks": [
                {
                    "check_id": "test",
                    "argv": _ARGV,
                    "cwd_rel": ".",
                    "expect_exit_code": 0,
                    "timeout_seconds": 60,
                }
            ],
            "corpus_manifest": _MANIFEST,
        },
        headers=_agent_headers(),
    )
    assert frozen.status_code == 200, frozen.text
    assert frozen.json()["frozen_by"] == _AGENT

    _claim_and_report(lite_client, session_id, directive_id)
    stored = _rows(
        "SELECT claimed_executor FROM directive_executions WHERE directive_id = ?", (directive_id,)
    )
    assert str(stored[0]["claimed_executor"]) == _AGENT, "the freezer is the executing identity"

    payload = _grade(lite_client, directive_id)
    assert payload["verdict"] == "inconclusive"
    assert payload["reason"] == "criteria_frozen_by_executing_identity"
    assert payload["verification_state"] == "unverified"
    # And it is durable, not just a response shape: the row R9's inputs are derived from agrees.
    assert (
        str(
            _rows(
                "SELECT verification_state FROM directive_executions WHERE directive_id = ?",
                (directive_id,),
            )[0]["verification_state"]
        )
        == "unverified"
    )
    assert str(_rows("SELECT verdict FROM verification_results WHERE directive_id = ?", (directive_id,))[0]["verdict"]) == "inconclusive"


def test_the_task_is_held_at_awaiting_verification_not_released(lite_client: TestClient) -> None:
    """The consequence the attack was after: the projection must not reach ``done``."""
    session_id = f"freeze-order-done-{uuid.uuid4().hex[:8]}"
    directive_id = _open_directive(lite_client, session_id)
    lite_client.post(
        "/v1/verification/criteria",
        json={
            "directive_id": directive_id,
            "checks": [
                {"check_id": "test", "argv": _ARGV, "cwd_rel": ".", "expect_exit_code": 0, "timeout_seconds": 60}
            ],
            "corpus_manifest": _MANIFEST,
        },
        headers=_agent_headers(),
    )
    _claim_and_report(lite_client, session_id, directive_id)
    _grade(lite_client, directive_id)

    state = lite_client.get(f"/v1/tasks/{session_id}/state", headers=_agent_headers())
    assert state.status_code == 200, state.text
    assert state.json()["status"] != "done"


def test_an_independently_frozen_directive_still_passes(lite_client: TestClient) -> None:
    """The guard discriminates.  Same evidence, same verifier, a freezer who is not the executor.

    Without this the fix could be a constant refusal and the suite would not notice.
    """
    session_id = f"freeze-order-ok-{uuid.uuid4().hex[:8]}"
    directive_id = _open_directive(lite_client, session_id)
    frozen = lite_client.post(
        "/v1/verification/criteria",
        json={
            "directive_id": directive_id,
            "checks": [
                {"check_id": "test", "argv": _ARGV, "cwd_rel": ".", "expect_exit_code": 0, "timeout_seconds": 60}
            ],
            "corpus_manifest": _MANIFEST,
        },
        # Frozen by the verifier's bound credential -- a principal that is not the executing one.
        headers=_verifier_headers(),
    )
    assert frozen.status_code == 200, frozen.text
    assert frozen.json()["frozen_by"] == _VERIFIER

    _claim_and_report(lite_client, session_id, directive_id)
    payload = _grade(lite_client, directive_id)
    assert payload["verdict"] == "passed", payload


def test_criteria_cannot_be_frozen_for_a_directive_that_is_not_ours(lite_client: TestClient) -> None:
    """The freeze is one-shot, so an unscoped freeze is not a harmless no-op.

    ``UNIQUE (directive_id)`` with no UPDATE and no DELETE path means the first freeze for a
    directive id wins forever. Measured over HTTP before this check: an ordinary bound executor
    froze ``/bin/sh -c "exit 0"`` against a directive id it had simply invented and got 200 --
    which buys criteria for work someone else will do, and a pre-emptive 409 against the
    supervisor's own honest freeze for that directive.
    """
    invented = str(uuid.uuid4())
    refused = lite_client.post(
        "/v1/verification/criteria",
        json={
            "directive_id": invented,
            "checks": [
                {"check_id": "test", "argv": _ARGV, "cwd_rel": ".", "expect_exit_code": 0, "timeout_seconds": 60}
            ],
            "corpus_manifest": _MANIFEST,
        },
        headers=_agent_headers(),
    )
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"]["error"] == "unknown_directive"
    assert _rows("SELECT id FROM acceptance_criteria WHERE directive_id = ?", (invented,)) == []
