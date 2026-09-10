"""P1 identity gate on the Full backend's memory-review promotion path.

``POST /v1/behavior/reviews/{review_id}/resolve`` turns a pending_review row into learning-eligible
evidence, so it is the same trust decision as minting that evidence directly. Without the gate a
header-asserted ``X-TCE-Role: user`` (compat mode, on the executor's own bearer) could round-trip its own
pending row past the identity gate in one extra call.

The store call is mocked out on purpose: the assertion is that an unverified promotion never reaches it.
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

_WORKSPACE = "review-gate-workspace"
_HUMAN = "review-gate-human"


def _headers(token: str, *, role: str = "user", consumer: str = "operator-ui", user: str = _HUMAN) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": role,
        "X-TCE-Workspace": _WORKSPACE,
        "X-TCE-User": user,
        "X-TCE-Behavior-Subject": _HUMAN,
    }


def _review_row(review_id: uuid.UUID, *, status: str) -> dict[str, Any]:
    return {
        "review_id": review_id,
        "target_type": "evidence",
        "target_id": uuid.uuid4(),
        "title": "pending evidence",
        "rationale": "inferred from an executor turn",
        "status": status,
        "proposed_action": "promote",
        "source": "behavior_evidence",
        "score": 0.9,
        "reviewer_id": _HUMAN,
        "review_note": "",
        "created_at": datetime(2026, 7, 22, tzinfo=UTC),
        "resolved_at": datetime(2026, 7, 22, tzinfo=UTC),
        "schema_version": "v1",
    }


@pytest.fixture()
def full_settings() -> Iterator[Any]:
    """Compat identity mode with no server-bound claims: every X-TCE-* assertion is unverified."""
    settings = get_settings()
    snapshot = {
        "workspace_access_mode": settings.workspace_access_mode,
        "identity_claims_mode": settings.identity_claims_mode,
        "identity_claims_json": settings.identity_claims_json,
    }
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
        for key, value in snapshot.items():
            setattr(settings, key, value)


def test_unverified_caller_cannot_promote_a_pending_review(full_settings: Any) -> None:
    token = next(iter(full_settings.token_set))
    review_id = uuid.uuid4()
    client = TestClient(app)

    with (
        patch("tce_api.main.resolve_memory_review") as resolve,
        patch("tce_api.main.write_audit_log"),
    ):
        # (a) a header-asserted human on an unbound bearer
        asserted = client.post(
            f"/v1/behavior/reviews/{review_id}/resolve",
            json={"decision": "promote", "note": "looks right to me"},
            headers=_headers(token),
        )
        # (b) and the executor that filed the pending row in the first place
        executor = client.post(
            f"/v1/behavior/reviews/{review_id}/resolve",
            json={"decision": "promote", "note": "self-promotion"},
            headers=_headers(token, role="executor", consumer="codex-executor", user="codex-executor"),
        )
        assert asserted.status_code == 403, asserted.text
        assert asserted.json()["detail"]["error"] == "behavior_review_promotion_rejected"
        assert "identity_unverified" in asserted.json()["detail"]["reasons"], asserted.text
        assert executor.status_code == 403, executor.text
        assert "non_human_caller" in executor.json()["detail"]["reasons"], executor.text
        # The promotion never reached the store: nothing was written, so nothing became learning-eligible.
        resolve.assert_not_called()

    # Rejecting a pending row needs no such proof: it promotes nothing.
    with (
        patch("tce_api.main.resolve_memory_review", return_value=_review_row(review_id, status="rejected")) as resolve,
        patch("tce_api.main.write_audit_log"),
    ):
        rejected = client.post(
            f"/v1/behavior/reviews/{review_id}/resolve",
            json={"decision": "reject", "note": "not the owner's call"},
            headers=_headers(token),
        )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert resolve.call_args.kwargs["decision"] == "reject"


def test_server_bound_human_may_promote_a_pending_review(full_settings: Any) -> None:
    token = next(iter(full_settings.token_set))
    full_settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", token): {
                "consumer": "operator-ui",
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _HUMAN,
                "behavior_subject_id": _HUMAN,
            }
        }
    )
    review_id = uuid.uuid4()

    with (
        patch("tce_api.main.resolve_memory_review", return_value=_review_row(review_id, status="promoted")) as resolve,
        patch("tce_api.main._rebuild_behavior_fingerprint_full") as rebuild,
        patch("tce_api.main.write_audit_log"),
    ):
        promoted = TestClient(app).post(
            f"/v1/behavior/reviews/{review_id}/resolve",
            json={"decision": "promote", "note": "reviewed by the owner"},
            headers=_headers(token),
        )

    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["status"] == "promoted"
    assert resolve.call_args.kwargs["decision"] == "promote"
    assert resolve.call_args.kwargs["reviewer_id"] == _HUMAN
    rebuild.assert_called_once()
