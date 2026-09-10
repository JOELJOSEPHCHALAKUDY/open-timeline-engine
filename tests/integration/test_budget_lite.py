"""P3 dispatch budget accounting, against the Lite HTTP boundary (G10, live half).

The property: spend caps are labelled honestly per surface, and an overshoot is *recorded*, never
clamped.  Quietly clamping the recorded cost to the reservation would make the row lie about the
one thing the charter asks it to make visible.

Matrix:
  1 an overshoot is recorded, not hidden, and governance reports the enforcement label
  2 a request past the charter's cap is 409 `budget_exceeded` -- never the latency `budget_exhausted`
  3 the concurrency cap is the reader of CharterCaps.max_concurrent_dispatches
  4 a non-advisory dispatch without a fresh, matching sandbox self-test is refused
  5 an advisory dispatch is confined to a read-only task family
"""

from __future__ import annotations

import hashlib
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
from tce_shared.decision_capture import compute_delivery_key
from tce_shared.identity import credential_fingerprint
from tce_shared.redaction import redact_text

_EXEC_TOKEN = "budget-exec-token"
_OPERATOR_TOKEN = "budget-operator-token"
_HOST_TOKEN = "budget-host-token"
_HUMAN = "human-1"
_WORKSPACE = "personal"
_PROFILE_DIGEST = "1597dfcdae2a79b8b1294744bbc11a0a2385f150aca3d88cc957d757de9181ec"
_MISSING = object()
_GUARDED_SETTINGS = (
    "lite_db_path",
    "api_tokens",
    "host_capture_tokens",
    "allow_default_token",
    "default_operation_mode",
    "identity_claims_mode",
    "identity_claims_json",
    "workspace_access_mode",
    "charter_enforcement_enabled",
    "budget_default_minor_units",
    "budget_currency",
    "sandbox_self_test_max_age_seconds",
    "dispatch_startup_reconcile_enabled",
)

_ALL_CAPABILITIES = [
    "filesystem.read",
    "filesystem.write",
    "git.read",
    "git.write",
    "network.read",
    "network.write",
    "process.execute",
    "process.inspect",
    "tce.memory.review",
]


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
    settings.lite_db_path = str(tmp_path / "budget-lite.db")
    settings.api_tokens = f"{_EXEC_TOKEN},{_OPERATOR_TOKEN}"
    settings.host_capture_tokens = _HOST_TOKEN
    settings.allow_default_token = False
    settings.default_operation_mode = "clone_advisor"
    settings.identity_claims_mode = "compat"
    settings.workspace_access_mode = "compat"
    settings.charter_enforcement_enabled = True
    settings.budget_default_minor_units = 200
    settings.budget_currency = "USD"
    settings.sandbox_self_test_max_age_seconds = 3600
    settings.identity_claims_json = json.dumps(
        {
            credential_fingerprint("bearer", _OPERATOR_TOKEN): {
                "consumer": "operator-ui",
                "role": "user",
                "workspace_id": _WORKSPACE,
                "user_id": _HUMAN,
                "behavior_subject_id": _HUMAN,
            }
        }
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        _restore_settings(snapshot)


def _exec_headers(consumer: str = "supervisor") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_EXEC_TOKEN}",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": "executor",
        "X-TCE-User": _HUMAN,
        "X-TCE-Workspace": _WORKSPACE,
    }


def _operator_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_OPERATOR_TOKEN}",
        "X-TCE-Consumer": "operator-ui",
        "X-TCE-Role": "user",
        "X-TCE-User": _HUMAN,
        "X-TCE-Behavior-Subject": _HUMAN,
    }


def _host_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_HOST_TOKEN}",
        "X-TCE-Consumer": "host-capture-claude",
        "X-TCE-User": _HUMAN,
        "X-TCE-Workspace": _WORKSPACE,
    }


def _capture_receipt(client: TestClient) -> str:
    content = "approve the dispatch charter"
    host_session_id = str(uuid.uuid4())
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    redacted, applied = redact_text(content)
    body = {
        "session_id": host_session_id,
        "delivery_key": compute_delivery_key(host_session_id, "p1", content_sha256),
        "content_sha256": content_sha256,
        "content": redacted,
        "origin_kind": "human_input",
        "observed_at": datetime.now(tz=UTC).isoformat(),
        "original_char_count": len(content),
        "content_truncated": False,
        "redaction_applied": applied,
        "prompt_id": "p1",
        "hook_event_name": "UserPromptSubmit",
        "host_client": "claude",
        "cwd": "/work/open-timeline-engine",
        "project_hint": {"project": "open-timeline-engine", "project_root": "/work/open-timeline-engine"},
        "schema_version": "v1",
    }
    response = client.post("/v1/inputs", json=body, headers=_host_headers())
    assert response.status_code == 201, response.text
    return str(response.json()["receipt_id"])


def _activate_charter(client: TestClient, **caps: Any) -> dict[str, Any]:
    receipt_id = _capture_receipt(client)
    body = {
        "source_receipt_id": receipt_id,
        "enforcement_tier": "container",
        "permitted_roots": ["docs/", "src/"],
        "protected_write_prefixes": ["tests/"],
        "permitted_capabilities": _ALL_CAPABILITIES,
        "caps": {
            "max_attempts": 3,
            "max_concurrent_dispatches": 1,
            "max_wall_seconds": 1800,
            "budget_minor_units": 500,
            "budget_currency": "USD",
            "spend_enforcement": "unsupported",
            **caps,
        },
        "ttl_seconds": 3600,
    }
    created = client.post("/v1/charters", json=body, headers=_operator_headers())
    assert created.status_code == 200, created.text
    charter_id = created.json()["charter_id"]
    approved = client.post(f"/v1/charters/{charter_id}/approve", headers=_operator_headers())
    assert approved.status_code == 200, approved.text
    payload: dict[str, Any] = approved.json()
    return payload


def _self_test(client: TestClient, *, passed: bool = True, digest: str = _PROFILE_DIGEST) -> str:
    response = client.post(
        "/v1/sandbox/self-test",
        json={
            "host_id": "test-host",
            "sandbox_provider": "seatbelt",
            "provider_version": "sandbox-exec",
            "profile_digest": digest,
            "assertions": [{"name": "write_outside_taskdir_denied", "expected": "denied", "observed": "denied", "passed": True}],
            "passed": passed,
            "uid_separation": False,
        },
        headers=_exec_headers(),
    )
    assert response.status_code == 200, response.text
    return str(response.json()["self_test_id"])


def _dispatch_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "session_id": "dispatch-session",
        "directive_id": str(uuid.uuid4()),
        "attempt": 1,
        "runtime_id": "codex",
        "runtime_version": "0.153.1",
        "surface": "codex/app-server",
        "model_id": "gpt-5-codex",
        "task_family": "implement",
        "enforcement_tier": "container",
        "sandbox_provider": "seatbelt",
        "sandbox_profile_digest": _PROFILE_DIGEST,
        "provider_run_id": str(uuid.uuid4()),
        "request_minor_units": 200,
    }
    body.update(overrides)
    return body


def _open(client: TestClient, **overrides: Any) -> Any:
    return client.post(
        "/v1/dispatch",
        json=_dispatch_body(**overrides),
        headers={**_exec_headers(), "Idempotency-Key": uuid.uuid4().hex},
    )


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


# --------------------------------------------------------------------------- cases


def test_overshoot_is_recorded_not_hidden(lite_client: TestClient) -> None:
    """G10, live half — case 1."""
    _activate_charter(lite_client)
    _self_test(lite_client)
    opened = _open(lite_client)
    assert opened.status_code == 200, opened.text
    payload = opened.json()
    assert payload["budget_reserved_minor_units"] == 200
    assert payload["spend_enforcement"] == "unsupported"
    # cap_applied is populated only for an `enforced` surface: it is the number the runtime is
    # actually given. Nothing runnable is `enforced` today, so this is None and says so.
    assert payload["cap_applied"] is None

    reconciled = lite_client.post(
        f"/v1/dispatch/{payload['dispatch_id']}/reconcile",
        json={
            "outcome": "succeeded",
            "terminal_reason": "completed",
            "wall_ms": 42_000,
            "human_intervention_count": 0,
            "reconciliation": {
                "cost_minor_units": 900,
                "cost_source": "estimated",
                "tokens_input": 12000,
                "tokens_output": 3400,
                "tokens_cached_input": 800,
                "tokens_reasoning": 150,
                "overshoot_minor_units": 700,
            },
        },
        headers=_exec_headers(),
    )
    assert reconciled.status_code == 200, reconciled.text
    body = reconciled.json()
    assert body["cost_minor_units"] == 900
    assert body["cost_minor_units"] > body["budget_reserved_minor_units"]
    assert body["cost_source"] == "estimated"
    assert body["spend_enforcement"] == "unsupported"

    status = lite_client.get("/v1/governance/status", headers=_operator_headers())
    assert status.status_code == 200, status.text
    governance = status.json()
    assert governance["spend_enforcement"] == "unsupported"
    assert governance["charter_active"] is True
    assert governance["enforcement_tier"] == "container"
    assert governance["sandbox_self_test_passed"] is True
    assert governance["uid_separation"] is False
    assert any("recorded cost is an estimate" in line for line in governance["limitations"])
    assert any("not UID-level" in line for line in governance["limitations"])


def test_a_request_past_the_cap_is_budget_exceeded(lite_client: TestClient) -> None:
    """Case 2.  The code is `budget_exceeded`, deliberately NOT the pre-existing latency code
    `budget_exhausted` -- confusing the two would report a spend refusal as a timeout."""
    _activate_charter(lite_client)
    _self_test(lite_client)
    response = _open(lite_client, request_minor_units=5000)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "budget_exceeded"
    assert detail["cap_minor_units"] == 500
    assert _rows("SELECT id FROM dispatch_records") == []


def test_the_concurrency_cap_is_enforced(lite_client: TestClient) -> None:
    """Case 3 — the reader of ``CharterCaps.max_concurrent_dispatches``."""
    _activate_charter(lite_client, max_concurrent_dispatches=1)
    _self_test(lite_client)
    first = _open(lite_client, request_minor_units=100)
    assert first.status_code == 200, first.text
    second = _open(lite_client, request_minor_units=100)
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["error"] == "dispatch_concurrency_exceeded"


def test_a_stale_or_mismatched_self_test_refuses_dispatch(lite_client: TestClient) -> None:
    """Case 4 (S16).  An unmeasured sandbox is a claim, not a control, and a measurement of a
    DIFFERENT profile measures nothing about the one about to run."""
    _activate_charter(lite_client)
    missing = _open(lite_client)
    assert missing.status_code == 409, missing.text
    assert missing.json()["detail"]["error"] == "sandbox_self_test_missing"

    _self_test(lite_client, digest="0" * 64)
    mismatch = _open(lite_client)
    assert mismatch.status_code == 409, mismatch.text
    assert mismatch.json()["detail"]["error"] == "sandbox_self_test_digest_mismatch"

    _self_test(lite_client, passed=False)
    failed = _open(lite_client)
    assert failed.status_code == 409, failed.text
    assert failed.json()["detail"]["error"] == "sandbox_self_test_missing"


def test_advisory_tier_is_confined_to_read_only_task_families(lite_client: TestClient) -> None:
    """Case 5.  ``advisory`` applies no OS boundary; letting it carry mutating work would be the
    tier ladder in name only."""
    _activate_charter(lite_client)
    refused = _open(lite_client, enforcement_tier="advisory", task_family="implement", sandbox_provider="none")
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["error"] == "task_family_not_read_only"

    allowed = _open(
        lite_client, enforcement_tier="advisory", task_family="read_only_review", sandbox_provider="none", request_minor_units=50
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["enforcement_tier"] == "advisory"
    assert allowed.json()["sandbox_self_test_id"] is None


def test_dispatch_is_idempotent_on_directive_and_attempt(lite_client: TestClient) -> None:
    """A retried open must not mint a second reservation against the same cap."""
    _activate_charter(lite_client)
    _self_test(lite_client)
    directive_id = str(uuid.uuid4())
    first = _open(lite_client, directive_id=directive_id, request_minor_units=100)
    second = _open(lite_client, directive_id=directive_id, request_minor_units=100)
    assert first.status_code == 200 and second.status_code == 200, second.text
    assert first.json()["dispatch_id"] == second.json()["dispatch_id"]
    assert len(_rows("SELECT id FROM dispatch_records")) == 1
