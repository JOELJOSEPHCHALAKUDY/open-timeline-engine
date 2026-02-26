from __future__ import annotations

import os

import pytest
import requests


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {os.getenv('TCE_API_TOKEN', 'local-dev-token')}",
        "X-TCE-Consumer": os.getenv("TCE_TEST_CONSUMER", "test-suite"),
        "X-TCE-Role": os.getenv("TCE_TEST_ROLE", "executor"),
        "X-TCE-Workspace": os.getenv("TCE_TEST_WORKSPACE", "default"),
        "X-TCE-User": os.getenv("TCE_TEST_USER", "default-user"),
        "Content-Type": "application/json",
    }


@pytest.mark.integration
def test_retrieval_eval_run_and_status_live() -> None:
    base_url = os.getenv("TCE_API_BASE_URL", "http://localhost:8080").rstrip("/")
    try:
        health = requests.get(f"{base_url}/v1/health", timeout=5)
    except requests.RequestException:
        pytest.skip("TCE API is not reachable for integration test")
    if health.status_code >= 500:
        pytest.skip("TCE API unhealthy for integration test")

    run_resp = requests.post(
        f"{base_url}/v1/retrieval/eval/run",
        headers=_headers(),
        json={
            "session_id": "integration-test",
            "tasks": ["Evaluate retrieval quality for integration path"],
            "with_brief": True,
        },
        timeout=15,
    )
    assert run_resp.status_code == 200
    run_payload = run_resp.json()
    assert "run_id" in run_payload
    assert run_payload.get("session_id") == "integration-test"

    status_resp = requests.get(
        f"{base_url}/v1/retrieval/eval/status",
        headers=_headers(),
        params={"session_id": "integration-test"},
        timeout=10,
    )
    assert status_resp.status_code == 200
    status_payload = status_resp.json()
    assert status_payload.get("session_id") == "integration-test"
    assert isinstance(status_payload.get("history"), list)

