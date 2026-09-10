"""P3 verification authority, against the Lite HTTP boundary (U5, G3 and its three companions).

The property under test is narrow and load-bearing: ``verification_state`` is computed by the API
from raw evidence plus a frozen criteria row, and there is no wire field in which a caller can
assert a verdict about itself.

Matrix:
  1 the verifier overrides the agent's self-report (G3)
  2 a `verdict` in the POST body changes nothing; the stored verdict is the computed one
  3 `runner_principal` comes from auth, not from the body
  4 an empty criteria set FAILS -- it never passes vacuously
  5 a second freeze is 409, and there is no UPDATE path
  6 the runner identity is server-bound: a header-asserted non-executor role is refused (F1)
  7 the autonomy KPI counts verification_results rows, not the agent's own booleans (F3)
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
from tce_lite_api.main import app
from tce_shared.identity import credential_fingerprint
from tce_shared.verification import corpus_digest

_EXEC_TOKEN = "verify-exec-token"
_VERIFIER_TOKEN = "verify-runner-token"
_MANAGER_TOKEN = "verify-manager-token"
_HUMAN = "human-1"
_WORKSPACE = "personal"
_MISSING = object()
_GUARDED_SETTINGS = (
    "lite_db_path",
    "api_tokens",
    "allow_default_token",
    "default_operation_mode",
    "identity_claims_mode",
    "identity_claims_json",
    "workspace_access_mode",
    "charter_enforcement_enabled",
    "verification_enabled",
    "verification_runner_principal",
    "verification_max_checks",
    "verification_check_timeout_seconds",
    "dispatch_startup_reconcile_enabled",
)

_BOUND_APP_CONTEXT: dict[str, Any] = {
    "domain": "coding",
    "project": "open-timeline-engine",
    "project_root": "/work/open-timeline-engine",
}
# One deciding file is enough for the manifest to be able to detect a build-config swap.
_MANIFEST: list[list[str]] = [["pyproject.toml", "a" * 64]]
_MANIFEST_DIGEST = corpus_digest([("pyproject.toml", "a" * 64)])


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
def lite_client(tmp_path: Path) -> Iterator[TestClient]:
    snapshot = _snapshot_settings()
    settings = get_settings()
    settings.lite_db_path = str(tmp_path / "verification-lite.db")
    settings.api_tokens = f"{_EXEC_TOKEN},{_VERIFIER_TOKEN},{_MANAGER_TOKEN}"
    settings.allow_default_token = False
    settings.default_operation_mode = "clone_advisor"
    settings.identity_claims_mode = "compat"
    settings.workspace_access_mode = "compat"
    settings.charter_enforcement_enabled = False
    settings.verification_enabled = True
    settings.verification_runner_principal = "system:verifier"
    settings.identity_claims_json = json.dumps(
        {
            # The verifier has its OWN credential. Without that, "a principal distinct from the
            # executing identity" is a string the caller asserts about itself.
            credential_fingerprint("bearer", _VERIFIER_TOKEN): {
                "consumer": "system:verifier",
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _HUMAN,
                "behavior_subject_id": _HUMAN,
            },
            credential_fingerprint("bearer", _MANAGER_TOKEN): {
                "consumer": "manager-ui",
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _HUMAN,
                "behavior_subject_id": _HUMAN,
            },
        }
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        _restore_settings(snapshot)


def _exec_headers(consumer: str = "worker-a") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_EXEC_TOKEN}",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": "executor",
        "X-TCE-User": _HUMAN,
        "X-TCE-Workspace": _WORKSPACE,
    }


def _verifier_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_VERIFIER_TOKEN}"}


def _manager_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_MANAGER_TOKEN}"}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(get_settings().lite_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    conn = _db()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _reported_directive(client: TestClient, session_id: str) -> str:
    """Permit -> step -> claim -> report succeeded, with the agent claiming it verified itself."""
    permit = client.post(
        "/v1/takeover/permit",
        json={"session_id": session_id, "action_kind": "edit", "target_paths": ["docs/notes.md"], "estimated_change_size": 5},
        headers=_exec_headers(),
    )
    assert permit.status_code == 200, permit.text
    step = client.post(
        "/v1/takeover/step",
        json={
            "message": "beru take over",
            "session_id": session_id,
            "persona_mode": "shadow",
            "task": "update the changelog notes",
            "app_context": _BOUND_APP_CONTEXT,
            "allow_fallback": True,
        },
        headers=_exec_headers(),
    )
    assert step.status_code == 200, step.text
    directive_id = str(step.json()["directive_id"])
    claim = client.post(
        "/v1/takeover/execution/claim",
        json={"session_id": session_id, "directive_id": directive_id},
        headers=_exec_headers(),
    )
    assert claim.status_code == 200, claim.text
    report = client.post(
        "/v1/takeover/execution/report",
        json={
            "session_id": session_id,
            "directive_id": directive_id,
            "state": "succeeded",
            "lease_generation": int(claim.json()["lease_generation"]),
            # The agent's own claim that it built, tested and linted. It must count for nothing.
            "details": {"verification": {"build": True, "test": True, "lint": True}},
        },
        headers=_exec_headers(),
    )
    assert report.status_code == 200, report.text
    return directive_id


def _freeze(client: TestClient, directive_id: str, *, expect_exit_code: int = 0) -> dict[str, Any]:
    response = client.post(
        "/v1/verification/criteria",
        json={
            "directive_id": directive_id,
            "checks": [
                {
                    "check_id": "test",
                    "argv": ["/bin/sh", "-c", "exit 1"],
                    "cwd_rel": ".",
                    "expect_exit_code": expect_exit_code,
                    "timeout_seconds": 60,
                }
            ],
            "corpus_manifest": _MANIFEST,
        },
        headers=_manager_headers(),
    )
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def _evidence(directive_id: str, *, exit_code: int = 1, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "directive_id": directive_id,
        "results": [
            {
                "check_id": "test",
                "argv": ["/bin/sh", "-c", "exit 1"],
                "exit_code": exit_code,
                "duration_ms": 12,
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
    }
    body.update(extra)
    return body


# --------------------------------------------------------------------------- cases


def test_verifier_overrides_the_self_report(lite_client: TestClient) -> None:
    """G3 — case 1.  The agent reported success and asserted its own verification; the state stays
    ``unverified`` until evidence arrives, and then the API grades it."""
    session_id = f"verify-{uuid.uuid4().hex[:8]}"
    directive_id = _reported_directive(lite_client, session_id)
    assert str(_rows("SELECT verification_state FROM directive_executions WHERE directive_id = ?", (directive_id,))[0]["verification_state"]) == "unverified"

    _freeze(lite_client, directive_id)
    response = lite_client.post("/v1/verification/results", json=_evidence(directive_id), headers=_verifier_headers())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["verdict"] == "failed"
    assert payload["criteria_digest_match"] is True
    assert payload["corpus_digest_match"] is True
    assert payload["platform"] == "darwin/arm64 python3.12.12"
    assert str(_rows("SELECT verification_state FROM directive_executions WHERE directive_id = ?", (directive_id,))[0]["verification_state"]) == "failed"
    stored = _rows("SELECT checks_json, verdict, executing_identity FROM verification_results WHERE directive_id = ?", (directive_id,))
    assert len(stored) == 1
    checks = json.loads(str(stored[0]["checks_json"]))
    assert checks[0]["exit_code"] == 1
    assert checks[0]["stdout_sha256"]
    assert str(stored[0]["executing_identity"]) == "worker-a"


def test_a_passing_run_marks_the_directive_passed(lite_client: TestClient) -> None:
    """The other side of case 1: matching evidence produces ``passed``, so the gate is not a
    constant."""
    session_id = f"verify-ok-{uuid.uuid4().hex[:8]}"
    directive_id = _reported_directive(lite_client, session_id)
    _freeze(lite_client, directive_id, expect_exit_code=1)
    response = lite_client.post("/v1/verification/results", json=_evidence(directive_id, exit_code=1), headers=_verifier_headers())
    assert response.status_code == 200, response.text
    assert response.json()["verdict"] == "passed"
    assert str(_rows("SELECT verification_state FROM directive_executions WHERE directive_id = ?", (directive_id,))[0]["verification_state"]) == "passed"


def test_body_cannot_carry_a_verdict(lite_client: TestClient) -> None:
    """Case 2.  ``VerificationResultRequest`` has no verdict field; an extra key is dropped by the
    model and the stored verdict is the computed one."""
    session_id = f"verdict-{uuid.uuid4().hex[:8]}"
    directive_id = _reported_directive(lite_client, session_id)
    _freeze(lite_client, directive_id)
    body = _evidence(directive_id, verdict="passed", reason="trust me", criteria_digest_match=True)
    response = lite_client.post("/v1/verification/results", json=body, headers=_verifier_headers())
    assert response.status_code == 200, response.text
    assert response.json()["verdict"] == "failed"
    assert str(_rows("SELECT verdict FROM verification_results WHERE directive_id = ?", (directive_id,))[0]["verdict"]) == "failed"


def test_runner_principal_comes_from_auth_not_body(lite_client: TestClient) -> None:
    """Case 3.  The executor's own credential cannot post a verification, whatever it claims about
    itself in the body."""
    session_id = f"runner-{uuid.uuid4().hex[:8]}"
    directive_id = _reported_directive(lite_client, session_id)
    _freeze(lite_client, directive_id)
    body = _evidence(directive_id, runner_principal="system:verifier")
    forged = lite_client.post("/v1/verification/results", json=body, headers=_exec_headers())
    assert forged.status_code == 403, forged.text
    assert forged.json()["detail"]["error"] == "unknown_runner"
    assert _rows("SELECT id FROM verification_results WHERE directive_id = ?", (directive_id,)) == []
    assert str(_rows("SELECT verification_state FROM directive_executions WHERE directive_id = ?", (directive_id,))[0]["verification_state"]) == "unverified"


def test_empty_criteria_set_fails(lite_client: TestClient) -> None:
    """Case 4.  A zero-check criteria row must FAIL, never pass vacuously.

    The empty row is inserted directly because the freeze route refuses to create one — which is
    itself the first half of the property.
    """
    session_id = f"empty-{uuid.uuid4().hex[:8]}"
    directive_id = _reported_directive(lite_client, session_id)
    refused = lite_client.post(
        "/v1/verification/criteria",
        json={"directive_id": directive_id, "checks": [], "corpus_manifest": _MANIFEST},
        headers=_manager_headers(),
    )
    assert refused.status_code == 422, refused.text

    conn = _db()
    try:
        conn.execute(
            """
            INSERT INTO acceptance_criteria (
                id, workspace_id, owner_id, task_id, directive_id, charter_id, checks_json,
                criteria_digest, corpus_digest, corpus_manifest_json, frozen_at, frozen_by,
                policy_revision, schema_version
            ) VALUES (?, ?, ?, NULL, ?, NULL, '[]', '', ?, ?, ?, 'manager-ui', 'p3-2026-09', 'v1')
            """,
            (
                str(uuid.uuid4()),
                _WORKSPACE,
                _HUMAN,
                directive_id,
                _MANIFEST_DIGEST,
                json.dumps(_MANIFEST),
                datetime.now(tz=UTC).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    response = lite_client.post("/v1/verification/results", json=_evidence(directive_id), headers=_verifier_headers())
    assert response.status_code == 200, response.text
    assert response.json()["verdict"] == "failed"
    assert response.json()["reason"] == "empty_criteria_set"


def test_a_second_freeze_is_refused(lite_client: TestClient) -> None:
    """Case 5.  The whole point of freezing is that the implementing agent cannot move the
    goalposts after seeing the checks."""
    session_id = f"freeze-{uuid.uuid4().hex[:8]}"
    directive_id = _reported_directive(lite_client, session_id)
    _freeze(lite_client, directive_id)
    second = lite_client.post(
        "/v1/verification/criteria",
        json={
            "directive_id": directive_id,
            "checks": [{"check_id": "easy", "argv": ["/bin/true"], "cwd_rel": ".", "expect_exit_code": 0, "timeout_seconds": 5}],
            "corpus_manifest": _MANIFEST,
        },
        headers=_manager_headers(),
    )
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["error"] == "criteria_frozen"
    assert len(_rows("SELECT id FROM acceptance_criteria WHERE directive_id = ?", (directive_id,))) == 1


def test_a_manifest_without_a_deciding_file_is_refused(lite_client: TestClient) -> None:
    """S7's satisfiable half: a manifest that covers no build-deciding file could not detect a
    ``pyproject.toml`` swap, so freezing against it would be a corpus digest that proves nothing."""
    session_id = f"manifest-{uuid.uuid4().hex[:8]}"
    directive_id = _reported_directive(lite_client, session_id)
    response = lite_client.post(
        "/v1/verification/criteria",
        json={
            "directive_id": directive_id,
            "checks": [{"check_id": "t", "argv": ["/bin/true"], "cwd_rel": ".", "expect_exit_code": 0, "timeout_seconds": 5}],
            "corpus_manifest": [["docs/readme.md", "f" * 64]],
        },
        headers=_manager_headers(),
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"] == "corpus_manifest_incomplete"


def test_a_header_asserted_verifier_is_refused(lite_client: TestClient) -> None:
    """F1 — case 6.  ``identity_claims_mode`` defaults to ``compat``, so ``X-TCE-Role`` is a header
    the caller writes, not authentication.

    The executor-role check alone was therefore no gate at all: the implementing agent need only
    stop calling itself an executor, on its own unbound token, to grade its own directive.  The
    runner principal has to come from a server-bound claim.  This is the third appearance of the
    same laundering — P1 closed it for evidence, P3 for charters, and this route was missed.
    """
    session_id = f"unbound-{uuid.uuid4().hex[:8]}"
    directive_id = _reported_directive(lite_client, session_id)
    _freeze(lite_client, directive_id, expect_exit_code=1)
    laundered = dict(_exec_headers())
    laundered["X-TCE-Role"] = "user"  # the only thing the agent has to change
    response = lite_client.post("/v1/verification/results", json=_evidence(directive_id, exit_code=1), headers=laundered)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "unknown_runner"
    assert response.json()["detail"]["reasons"] == ["identity_unverified"]
    assert _rows("SELECT id FROM verification_results WHERE directive_id = ?", (directive_id,)) == []
    assert str(_rows("SELECT verification_state FROM directive_executions WHERE directive_id = ?", (directive_id,))[0]["verification_state"]) == "unverified"

    # The server-bound verifier credential still records, so the gate is not a constant.
    allowed = lite_client.post("/v1/verification/results", json=_evidence(directive_id, exit_code=1), headers=_verifier_headers())
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["verdict"] == "passed"


def test_project_kpis_count_verification_results_not_self_report(lite_client: TestClient) -> None:
    """F3 — case 7.  ``autonomy_project_kpis`` read the directive's own ``meta["verification"]``
    booleans, which the executor wrote about itself, to decide whether it may keep running
    unattended.  It must read ``verification_results``, and it must fail CLOSED at zero runs.
    """
    session_id = f"kpi-{uuid.uuid4().hex[:8]}"
    directive_id = _reported_directive(lite_client, session_id)
    # The agent's own {"build": true, "test": true, "lint": true} is already stored on the row.
    meta = json.loads(str(_rows("SELECT meta FROM directive_executions WHERE directive_id = ?", (directive_id,))[0]["meta"]) or "{}")
    assert meta.get("verification", {}).get("build") is True

    before = lite_client.get("/v1/takeover/autonomy/project-kpis", params={"session_id": session_id}, headers=_manager_headers())
    assert before.status_code == 200, before.text
    assert before.json()["metrics"]["verification_runs"] == 0
    assert before.json()["checks"]["verification_pass_rate"] is False
    assert before.json()["band"] == "project_autonomy_blocked"
    assert "verification_pass_rate" in before.json()["failing_checks"]

    _freeze(lite_client, directive_id, expect_exit_code=1)
    graded = lite_client.post("/v1/verification/results", json=_evidence(directive_id, exit_code=1), headers=_verifier_headers())
    assert graded.status_code == 200, graded.text
    assert graded.json()["verdict"] == "passed"

    after = lite_client.get("/v1/takeover/autonomy/project-kpis", params={"session_id": session_id}, headers=_manager_headers())
    assert after.status_code == 200, after.text
    assert after.json()["metrics"]["verification_runs"] == 1
    assert after.json()["metrics"]["verification_pass_rate"] == 1.0
    assert after.json()["checks"]["verification_pass_rate"] is True
