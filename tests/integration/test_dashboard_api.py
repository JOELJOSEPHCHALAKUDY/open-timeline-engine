from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.main import app


def _headers(
    role: str = "user",
    consumer: str = "codex-executor",
    workspace: str = "personal",
    user: str = "codex-executor",
) -> dict[str, str]:
    return {
        "Authorization": "Bearer lite-test-token",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": role,
        "X-TCE-Workspace": workspace,
        "X-TCE-User": user,
    }


def _event_payload(title: str) -> dict:
    return {
        "schema_version": 1,
        "ts": datetime.now(tz=UTC).isoformat(),
        "actor": "user",
        "source": "cli",
        "domain": "coding",
        "task_type": "implement_feature",
        "event_type": "TASK_STEP",
        "title": title,
        "payload": {"summary": title},
        "context": {"project": "open-timeline-engine"},
        "inputs": {},
        "steps": [],
        "decision": None,
        "outcome": None,
        "style": None,
        "links": None,
        "tags": ["dashboard-test"],
        "sensitivity": 1,
        "redaction_hints": [],
    }


@pytest.fixture()
def lite_client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    settings = get_settings()
    settings.lite_db_path = str(tmp_path / "tce-lite-dashboard-test.db")
    settings.api_tokens = "lite-test-token"
    settings.default_operation_mode = "clone_advisor"

    monkeypatch.setenv("TCE_MCP_WORKSPACE_ID", "personal")
    monkeypatch.setenv("TCE_MCP_EXECUTOR_USER_ID", "codex-executor")
    monkeypatch.setenv("TCE_MCP_SECONDARY_USER_ID", "claude-executor")
    monkeypatch.setenv("TCE_MCP_EXECUTOR_CONSUMER_ID", "codex-executor")
    monkeypatch.setenv("TCE_MCP_SECONDARY_CONSUMER_ID", "claude-executor")

    with TestClient(app) as client:
        yield client


def test_search_match_all_and_wildcard_semantics(lite_client: TestClient) -> None:
    ingest_a = lite_client.post(
        "/v1/events",
        json=_event_payload("dashboard match all A"),
        headers=_headers(consumer="codex-executor", user="codex-executor"),
    )
    ingest_b = lite_client.post(
        "/v1/events",
        json=_event_payload("dashboard match all B"),
        headers=_headers(consumer="codex-executor", user="codex-executor"),
    )
    assert ingest_a.status_code == 200
    assert ingest_b.status_code == 200

    explicit_match_all = lite_client.post(
        "/v1/search",
        json={"query": "*", "match_all": True, "filters": {"domain": "coding"}, "k": 10},
        headers=_headers(),
    )
    assert explicit_match_all.status_code == 200
    explicit_hits = explicit_match_all.json()["result"]["hits"]
    assert len(explicit_hits) >= 2

    wildcard_match = lite_client.post(
        "/v1/search",
        json={"query": "*", "filters": {"domain": "coding"}, "k": 10},
        headers=_headers(),
    )
    assert wildcard_match.status_code == 200
    wildcard_hits = wildcard_match.json()["result"]["hits"]
    assert len(wildcard_hits) >= 2

    empty_query_match = lite_client.post(
        "/v1/search",
        json={"query": " ", "filters": {"domain": "coding"}, "k": 10},
        headers=_headers(),
    )
    assert empty_query_match.status_code == 200
    empty_hits = empty_query_match.json()["result"]["hits"]
    assert len(empty_hits) >= 2


def test_dashboard_client_config_and_agent_roles(lite_client: TestClient) -> None:
    ingest = lite_client.post(
        "/v1/events",
        json=_event_payload("executor activity for dashboard role status"),
        headers=_headers(role="executor", consumer="codex-executor", user="codex-executor"),
    )
    assert ingest.status_code == 200

    client_config = lite_client.get("/v1/dashboard/client-config", headers=_headers())
    assert client_config.status_code == 200
    config_body = client_config.json()

    assert config_body["api_base"] == "/v1"
    assert config_body["default_workspace_id"] == "personal"
    assert config_body["runtime_mode"]["mode"] in {"timeline_only", "clone_advisor"}
    assert "token" not in config_body
    assert "secret" not in config_body
    assert config_body["known_identities"]["executor"]["user_id"] == "codex-executor"
    assert config_body["known_identities"]["secondary_executor"]["user_id"] == "claude-executor"
    assert config_body["known_identities"]["advisor"]["user_id"] == "claude-executor"

    roles = lite_client.get("/v1/dashboard/agent-roles", headers=_headers())
    assert roles.status_code == 200
    roles_body = roles.json()

    assert roles_body["workspace_id"] == "personal"
    assert roles_body["executor"]["user_id"] == "codex-executor"
    assert roles_body["secondary_executor"]["user_id"] == "claude-executor"
    assert roles_body["advisor"]["user_id"] == "claude-executor"
    assert roles_body["executor"]["status"] in {"active", "registered", "not_seen"}
    assert roles_body["advisor"]["status"] in {"active", "registered", "not_seen"}
    assert "generated_at" in roles_body
