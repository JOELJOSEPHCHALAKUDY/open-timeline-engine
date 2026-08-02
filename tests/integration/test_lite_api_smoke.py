from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.db import init_db
from tce_lite_api.main import app


def _headers(role: str = "user", consumer: str = "lite-test-user") -> dict[str, str]:
    return {
        "Authorization": "Bearer lite-test-token",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": role,
    }


def _event_payload(title: str, sensitivity: int = 1) -> dict:
    now = datetime.now(tz=UTC).isoformat()
    return {
        "schema_version": 1,
        "ts": now,
        "actor": "user",
        "source": "cli",
        "domain": "coding",
        "task_type": "debug",
        "event_type": "TASK_STEP",
        "title": title,
        "payload": {"summary": "debugging timeout", "token": "secret=ABC123XYZ"},
        "context": {"project": "open-timeline-engine"},
        "inputs": {},
        "steps": [],
        "decision": None,
        "outcome": None,
        "style": None,
        "links": None,
        "tags": ["lite-test"],
        "sensitivity": sensitivity,
        "redaction_hints": [],
    }


@pytest.fixture()
def lite_client(tmp_path) -> Iterator[TestClient]:
    settings = get_settings()
    settings.lite_db_path = str(tmp_path / "tce-lite-test.db")
    settings.api_tokens = "lite-test-token"
    settings.default_operation_mode = "timeline_only"
    with TestClient(app) as client:
        yield client


def test_lite_ingest_search_and_bundle(lite_client: TestClient) -> None:
    ingest = lite_client.post("/v1/events", json=_event_payload("lite smoke event"), headers=_headers())
    assert ingest.status_code == 200
    event_id = ingest.json()["event_id"]

    search = lite_client.post(
        "/v1/search",
        json={"query": "smoke event", "filters": {"domain": "coding"}, "k": 5},
        headers=_headers(),
    )
    assert search.status_code == 200
    search_body = search.json()
    assert search_body["result"]["hits"]
    assert "metadata" in search_body
    assert "handoff_hits_count" in search_body["metadata"]
    assert "top_handoff_record_ids" in search_body["metadata"]
    assert search_body["policy"]["retrieval"]["lexical_channel"].startswith("fts5_")
    assert search_body["policy"]["retrieval"]["scope_prefilter_applied"] is False

    event = lite_client.get(f"/v1/events/{event_id}", headers=_headers())
    assert event.status_code == 200
    assert "REDACTED" in str(event.json()["payload"])

    bundle = lite_client.post(
        "/v1/context_bundle",
        json={"task": "debug timeout", "app_context": {"domain": "coding"}, "constraints": {"k": 8}},
        headers=_headers(),
    )
    assert bundle.status_code == 200
    body = bundle.json()
    assert "citations" in body
    assert "policy" in body


def test_lite_fts_index_is_idempotent_and_tracks_event_mutations(
    lite_client: TestClient,
) -> None:
    ingest = lite_client.post(
        "/v1/events",
        json=_event_payload("ftsinsertmarker continuity"),
        headers=_headers(),
    )
    assert ingest.status_code == 200
    event_id = ingest.json()["event_id"]
    db_path = get_settings().lite_db_path

    init_db()
    init_db()
    with sqlite3.connect(db_path) as conn:
        count = conn.execute(
            "SELECT count(*) FROM events_fts WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0]
        assert count == 1

        conn.execute(
            "UPDATE events SET title = ? WHERE id = ?",
            ("ftsupdatedmarker continuity", event_id),
        )
        assert conn.execute(
            "SELECT count(*) FROM events_fts WHERE events_fts MATCH ? AND event_id = ?",
            ('"ftsupdatedmarker"', event_id),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM events_fts WHERE events_fts MATCH ? AND event_id = ?",
            ('"ftsinsertmarker"', event_id),
        ).fetchone()[0] == 0

        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        assert conn.execute(
            "SELECT count(*) FROM events_fts WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0] == 0


def test_lite_governance_status_is_honest_about_protocol_only_enforcement(
    lite_client: TestClient,
) -> None:
    response = lite_client.get("/v1/governance/status", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["runtime"] == "lite"
    assert body["lifecycle_protocol_enforced"] is True
    assert body["effective_execution_enforcement"] == "protocol_only"
    assert body["non_bypassable_execution"] is False
    assert any("cooperative protocol" in item for item in body["limitations"])


def test_lite_advisor_is_readonly(lite_client: TestClient) -> None:
    response = lite_client.post("/v1/events", json=_event_payload("advisor write"), headers=_headers(role="advisor"))
    assert response.status_code == 403


def test_lite_sensitivity_three_is_blocked_on_read(lite_client: TestClient) -> None:
    ingest = lite_client.post("/v1/events", json=_event_payload("secret event", sensitivity=3), headers=_headers())
    assert ingest.status_code == 200
    event_id = ingest.json()["event_id"]

    event = lite_client.get(f"/v1/events/{event_id}", headers=_headers())
    assert event.status_code == 403


def test_lite_clone_flow_fallback_and_arbitration(lite_client: TestClient) -> None:
    lite_client.post("/v1/events", json=_event_payload("clone event"), headers=_headers())

    advice = lite_client.post(
        "/v1/clone/advice",
        json={
            "task": "implement retry policy",
            "app_context": {"domain": "coding"},
            "constraints": {"k": 8},
            "interaction_id": "lite-test-001",
        },
        headers=_headers(role="advisor", consumer="lite-advisor"),
    )
    assert advice.status_code == 200
    assert advice.json()["interaction_id"] == "lite-test-001"

    mode = lite_client.put(
        "/v1/runtime/mode",
        json={"mode": "clone_advisor"},
        headers=_headers(),
    )
    assert mode.status_code == 200
    assert mode.json()["mode"] == "clone_advisor"

    arbitrate = lite_client.post(
        "/v1/clone/arbitrate",
        json={
            "interaction_id": "lite-test-001",
            "executor_plan": "retry outbound requests",
            "advisor_input": "add retry metrics",
            "human_override": None,
        },
        headers=_headers(role="executor", consumer="lite-executor"),
    )
    assert arbitrate.status_code == 200
    assert "final_guidance" in arbitrate.json()


def test_lite_graph_and_team_endpoints(lite_client: TestClient) -> None:
    ingest = lite_client.post("/v1/events", json=_event_payload("graph retry metric"), headers=_headers())
    assert ingest.status_code == 200
    event_id = ingest.json()["event_id"]

    graph_entity = lite_client.get(
        "/v1/graph/entities",
        params={"query": "retry", "k": 10},
        headers=_headers(),
    )
    assert graph_entity.status_code == 200
    assert "entities" in graph_entity.json()

    graph_event = lite_client.get(f"/v1/graph/event/{event_id}", headers=_headers())
    assert graph_event.status_code == 200
    assert "graph" in graph_event.json()

    upsert_member = lite_client.post(
        "/v1/team/memberships",
        json={"user_id": "teammate", "role": "member", "active": True},
        headers=_headers(),
    )
    assert upsert_member.status_code == 200

    team_list = lite_client.get("/v1/team/memberships", headers=_headers())
    assert team_list.status_code == 200
    assert any(item["user_id"] == "teammate" for item in team_list.json())


def test_lite_workspace_scope_isolated(lite_client: TestClient) -> None:
    ingest = lite_client.post(
        "/v1/events",
        json=_event_payload("workspace private item"),
        headers={
            **_headers(),
            "X-TCE-Workspace": "ws-a",
            "X-TCE-User": "alice",
        },
    )
    assert ingest.status_code == 200
    event_id = ingest.json()["event_id"]

    out_of_scope = lite_client.get(
        f"/v1/events/{event_id}",
        headers={
            **_headers(consumer="lite-test-user-b"),
            "X-TCE-Workspace": "ws-b",
            "X-TCE-User": "bob",
        },
    )
    assert out_of_scope.status_code == 403


def test_lite_resume_packet_cross_user(lite_client: TestClient) -> None:
    base_headers = {
        **_headers(consumer="lite-resume"),
        "X-TCE-Workspace": "personal",
    }
    now = datetime.now(tz=UTC).isoformat()
    codex_headers = {**base_headers, "X-TCE-User": "codex-executor"}
    claude_headers = {**base_headers, "X-TCE-User": "claude-executor"}
    assert lite_client.post("/v1/events", json=_event_payload("codex seed event"), headers=codex_headers).status_code == 200
    add_member = lite_client.post(
        "/v1/team/memberships",
        json={"user_id": "claude-executor", "role": "member", "active": True},
        headers=codex_headers,
    )
    assert add_member.status_code == 200
    assert lite_client.post("/v1/events", json=_event_payload("claude seed event"), headers=claude_headers).status_code == 200

    settings = get_settings()
    conn = sqlite3.connect(settings.lite_db_path)
    try:
        conn.execute(
            """
            INSERT INTO handoff_records(
                id, workspace_id, owner_id, session_id, directive_id, ts,
                title, decision, next_step, status, files_json, anchors_json, git_json,
                change_summary_json, objective_text, source, event_id, schema_version, redaction_applied, expires_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                "personal",
                "codex-executor",
                "default",
                None,
                now,
                "Refactor advisor fallback path",
                "Use deterministic fallback order for runtime provider resolution.",
                "Run scoped tests for advisor runtime routes.",
                "succeeded",
                json.dumps(["services/tce_api/tce_api/main.py"]),
                json.dumps([{"file": "services/tce_api/tce_api/main.py", "line": 3900, "symbol": "handoff_resume_packet"}]),
                json.dumps({"branch": "codex/resume-v1"}),
                json.dumps(
                    {
                        "services/tce_api/tce_api/main.py": {
                            "added": 42,
                            "removed": 10,
                            "intent": "Added resume packet endpoint and selector integration.",
                        }
                    }
                ),
                "advisor runtime fallback resume packet",
                "native",
                None,
                "v1",
                0,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    response = lite_client.post(
        "/v1/handoff/resume",
        json={
            "query": "continue codex work on advisor runtime fallback",
            "target_owner": "codex-executor",
            "session_id": "default",
            "k": 5,
            "include_cross_user": True,
        },
        headers=claude_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["task_summary"] == "Refactor advisor fallback path"
    assert body["cross_user_scope_applied"] is True
    assert body["files"]
    assert body["files"][0]["anchors"]
