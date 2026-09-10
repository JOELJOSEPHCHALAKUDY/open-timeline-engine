"""P3 F1: ``POST /v1/verification/results`` may not take its runner identity from a header.

The route computes the verdict itself and is the only writer of a non-``unverified``
``directive_executions.verification_state`` — so "who ran the checks" is the whole trust decision.
``_runner_principal_for(auth)`` derives that principal from ``auth.consumer``, and on the compat
path ``auth.consumer`` is simply whatever ``X-TCE-Consumer`` said.  Without an identity gate the
implementing agent sends ``X-TCE-Consumer: system:verifier`` on its own bearer, says
``X-TCE-Role: user`` to clear the executor check, and records ``verification_state='passed'`` on
its own directive.

This is the third appearance of the same laundering class: P1 closed it for evidence promotion,
P3 closed it for charters, this route was missed.  The gate is the charter routes' idiom — a
server-bound claim or the host-capture credential — minus the "must be a human" half, because the
verifier is a system principal.

The store call is mocked on purpose: the assertion is that an unverified runner never reaches it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from tce_api.config import get_settings
from tce_api.db import get_db
from tce_api.main import app
from tce_shared.identity import credential_fingerprint

pytestmark = pytest.mark.integration

_WORKSPACE = "verification-authority-workspace"
_OWNER = "verification-owner"
_RUNNER = "system:verifier"


def _headers(token: str, *, role: str = "user", consumer: str = _RUNNER) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": role,
        "X-TCE-Workspace": _WORKSPACE,
        "X-TCE-User": _OWNER,
    }


def _evidence(directive_id: uuid.UUID) -> dict[str, Any]:
    return {
        "directive_id": str(directive_id),
        "results": [
            {
                "check_id": "pytest",
                "argv": ["pytest", "-q"],
                "exit_code": 0,
                "duration_ms": 1200,
                "stdout_sha256": "a" * 64,
                "stderr_sha256": "b" * 64,
                "excerpt": "921 passed",
            }
        ],
        "observed_corpus_digest": "c" * 64,
        "observed_corpus_manifest": [["services/tce_api/tce_api/main.py", "d" * 64]],
        "platform": "darwin-arm64",
    }


def _recorded(directive_id: uuid.UUID) -> tuple[object, dict[str, Any]]:
    return (
        object(),
        {
            "verification_id": uuid.uuid4(),
            "directive_id": directive_id,
            "verdict": "passed",
            "reason": "",
            "verification_state": "passed",
            "criteria_digest_at_run": "e" * 64,
            "corpus_digest_at_run": "c" * 64,
            "criteria_digest_match": True,
            "corpus_digest_match": True,
            "runner_principal": _RUNNER,
            "executing_identity": "codex-executor",
            "platform": "darwin-arm64",
            "commit_sha": None,
            "tree_sha": None,
            "advisory": False,
            "recorded_at": datetime(2026, 9, 10, tzinfo=UTC),
        },
    )


@pytest.fixture()
def full_settings() -> Iterator[Any]:
    """Compat identity mode with no server-bound claims: every X-TCE-* assertion is unverified."""
    settings = get_settings()
    settings.workspace_access_mode = "compat"
    settings.identity_claims_mode = "compat"
    settings.identity_claims_json = "{}"

    def fake_db() -> Iterator[object]:
        yield object()

    app.dependency_overrides[get_db] = fake_db
    try:
        yield settings
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_header_asserted_runner_cannot_record_a_verdict(full_settings: Any) -> None:
    token = next(iter(full_settings.token_set))
    directive_id = uuid.uuid4()

    with (
        patch("tce_api.main.record_verification") as record,
        patch("tce_api.main.write_audit_log"),
    ):
        # (a) the agent naming itself the verifier on its own bearer, in the shipped default config
        asserted = TestClient(app).post(
            "/v1/verification/results",
            json=_evidence(directive_id),
            headers=_headers(token),
        )
        # (b) and the same agent not bothering to lie about its role
        executor = TestClient(app).post(
            "/v1/verification/results",
            json=_evidence(directive_id),
            headers=_headers(token, role="executor", consumer="codex-executor"),
        )

    assert asserted.status_code == 403, asserted.text
    assert asserted.json()["detail"]["error"] == "unknown_runner"
    assert asserted.json()["detail"]["reason"] == "identity_unverified", asserted.text
    assert executor.status_code == 403, executor.text
    assert executor.json()["detail"]["reason"] == "executor_role", executor.text
    # Nothing was written, so no directive's verification_state moved off 'unverified'.
    record.assert_not_called()


def test_server_bound_runner_may_record_a_verdict(full_settings: Any) -> None:
    token = next(iter(full_settings.token_set))
    full_settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", token): {
                "consumer": _RUNNER,
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _OWNER,
                "behavior_subject_id": _OWNER,
            }
        }
    )
    directive_id = uuid.uuid4()

    with (
        patch("tce_api.main.record_verification", return_value=_recorded(directive_id)) as record,
        patch("tce_api.main.write_audit_log"),
    ):
        recorded = TestClient(app).post(
            "/v1/verification/results",
            json=_evidence(directive_id),
            headers=_headers(token),
        )

    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["verdict"] == "passed"
    # The principal the store sees is the bound claim's, never the body's and never the header's.
    assert record.call_args.kwargs["runner_principal"] == _RUNNER
    assert record.call_args.kwargs["directive_id"] == directive_id


def test_a_bound_claim_cannot_be_overridden_by_a_conflicting_header(full_settings: Any) -> None:
    """Enforce mode: the header may not contradict the claim, so the runner name stays the claim's."""
    token = next(iter(full_settings.token_set))
    full_settings.identity_claims_mode = "enforce"
    full_settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", token): {
                "consumer": "codex-executor",
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _OWNER,
                "behavior_subject_id": _OWNER,
            }
        }
    )

    with (
        patch("tce_api.main.record_verification") as record,
        patch("tce_api.main.write_audit_log"),
    ):
        conflicting = TestClient(app).post(
            "/v1/verification/results",
            json=_evidence(uuid.uuid4()),
            headers=_headers(token, consumer=_RUNNER),
        )

    assert conflicting.status_code == 403, conflicting.text
    record.assert_not_called()
