"""Directive lifecycle guards: report_execution must be idempotent.

The MCP client auto-retries POSTs on 5xx/read-timeout, so a duplicate
report of an already-terminal directive must replay the recorded result —
not mint a second retry directive, re-run side effects, or flip the
terminal state.

Reports carry the lease generation handed out at claim time; a legacy
report without a lease is still accepted while ``takeover_lease_strict``
is off (see tests/integration/test_trust_boundary_lite.py for the fenced
paths).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.main import app


def _headers(role: str = "executor", consumer: str = "lifecycle-test") -> dict[str, str]:
    return {
        "Authorization": "Bearer lite-test-token",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": role,
    }


@pytest.fixture()
def lite_client(tmp_path) -> Iterator[TestClient]:
    settings = get_settings()
    settings.lite_db_path = str(tmp_path / "tce-lite-lifecycle-test.db")
    settings.api_tokens = "lite-test-token"
    settings.default_operation_mode = "clone_advisor"
    with TestClient(app) as client:
        yield client


def _mint_claimed_directive(client: TestClient, session_id: str) -> tuple[str, int]:
    """Permit + activation + claim. Returns (directive_id, lease_generation)."""
    permit = client.post(
        "/v1/takeover/permit",
        json={
            "session_id": session_id,
            "action_kind": "edit",
            "target_paths": ["docs/notes.md"],
            "estimated_change_size": 5,
        },
        headers=_headers(),
    )
    assert permit.status_code == 200
    assert permit.json()["decision"] == "allow"

    step = client.post(
        "/v1/takeover/step",
        json={
            "message": "beru take over",
            "session_id": session_id,
            "persona_mode": "shadow",
            "task": "update the changelog notes",
            # A mutating directive needs a bound project; an unbound context is refused (case 8).
            "app_context": {"domain": "coding", "project": "open-timeline-engine", "project_root": "/work/open-timeline-engine"},
            "constraints": {"k": 4},
            "allow_fallback": True,
        },
        headers=_headers(),
    )
    assert step.status_code == 200
    directive_id = step.json().get("directive_id")
    assert directive_id, "expected a PENDING directive with a valid ALLOW permit"

    claim = client.post(
        "/v1/takeover/execution/claim",
        json={"session_id": session_id, "directive_id": directive_id},
        headers=_headers(),
    )
    assert claim.status_code == 200
    lease_generation = int(claim.json()["lease_generation"])
    assert lease_generation >= 1
    return str(directive_id), lease_generation


def _report(client: TestClient, session_id: str, directive_id: str, state: str, *, lease: int | None = None) -> dict:
    payload: dict = {
        "session_id": session_id,
        "directive_id": directive_id,
        "state": state,
        "result": "failure" if state == "failed" else "success",
        "failure_reason": "tool crashed" if state == "failed" else None,
    }
    if lease is not None:
        payload["lease_generation"] = lease
    resp = client.post("/v1/takeover/execution/report", json=payload, headers=_headers())
    assert resp.status_code == 200, resp.text
    body: dict = resp.json()
    return body


def _pending_count(client: TestClient, session_id: str) -> int:
    status = client.get(
        "/v1/takeover/execution/status",
        params={"session_id": session_id},
        headers=_headers(),
    )
    assert status.status_code == 200
    return len(status.json().get("pending", []))


def test_duplicate_failed_report_replays_without_second_retry(
    lite_client: TestClient,
) -> None:
    session_id = f"lifecycle-{uuid.uuid4().hex[:8]}"
    directive_id, lease = _mint_claimed_directive(lite_client, session_id)

    first = _report(lite_client, session_id, directive_id, "failed", lease=lease)
    assert first["state"] == "failed"
    pending_after_first = _pending_count(lite_client, session_id)

    duplicate = _report(lite_client, session_id, directive_id, "failed", lease=lease)
    assert duplicate["state"] == "failed"
    assert duplicate.get("idempotent_replay") is True
    assert duplicate.get("retry_directive_id") == first.get("retry_directive_id")
    assert _pending_count(lite_client, session_id) == pending_after_first


def test_late_report_cannot_flip_terminal_state(lite_client: TestClient) -> None:
    session_id = f"lifecycle-{uuid.uuid4().hex[:8]}"
    directive_id, lease = _mint_claimed_directive(lite_client, session_id)

    first = _report(lite_client, session_id, directive_id, "failed", lease=lease)
    assert first["state"] == "failed"

    # Legacy no-key, no-lease late report: replays the recorded terminal result.
    late = _report(lite_client, session_id, directive_id, "succeeded")
    assert late["state"] == "failed"
    assert late.get("idempotent_replay") is True
