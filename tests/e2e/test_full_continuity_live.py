from __future__ import annotations

import os
import uuid

import pytest
import requests

pytestmark = pytest.mark.e2e


def _config() -> tuple[str, str, str]:
    base = os.getenv("TCE_E2E_BASE_URL", "").rstrip("/")
    if not base:
        pytest.skip("TCE_E2E_BASE_URL is not configured")
    return base, os.getenv("TCE_E2E_CODEX_TOKEN", ""), os.getenv("TCE_E2E_CLAUDE_TOKEN", "")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_full_postgres_continuity_path() -> None:
    base, codex_token, claude_token = _config()
    key = f"e2e:http:{uuid.uuid4()}"
    whoami = requests.get(f"{base}/v1/auth/whoami", headers=_headers(codex_token), timeout=10)
    assert whoami.status_code == 200, whoami.text
    assert whoami.json()["user_id"] == "codex-executor"
    forged = requests.get(
        f"{base}/v1/auth/whoami",
        headers={**_headers(codex_token), "X-TCE-Workspace": "forged"},
        timeout=10,
    )
    assert forged.status_code == 403

    completion = requests.post(
        f"{base}/v1/completions",
        headers=_headers(codex_token),
        timeout=15,
        json={
            "session_id": "e2e-shared",
            "completion_key": key,
            "source": "full-e2e",
            "state": "succeeded",
            "title": "Full stack durable handoff",
            "payload": {"files": ["services/tce_api/tce_api/continuity_store.py"]},
            "decision": "Use PostgreSQL outbox delivery",
            "outcome": {"status": "succeeded", "next_step": "Open the delivery function"},
            "git": {"branch": "e2e", "commit": "full123"},
            "anchors": [{"file": "services/tce_api/tce_api/continuity_store.py", "line": 70, "symbol": "deliver_handoff"}],
            "milestone_schema": "v1",
        },
    )
    assert completion.status_code == 200, completion.text
    assert completion.json()["delivery_status"] == "delivered"
    resume = requests.post(
        f"{base}/v1/handoff/resume",
        headers=_headers(claude_token),
        timeout=15,
        json={
            "query": "read codex timeline full stack durable handoff",
            "target_owner": "codex-executor",
            "session_id": "e2e-shared",
            "k": 5,
            "include_cross_user": True,
        },
    )
    assert resume.status_code == 200, resume.text
    assert resume.json()["files"][0]["anchors"][0]["symbol"] == "deliver_handoff"
