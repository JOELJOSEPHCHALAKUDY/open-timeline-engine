from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.main import app


def _headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer lite-test-token",
        "X-TCE-Consumer": "graph-test-user",
        "X-TCE-Role": "user",
        "X-TCE-Workspace": "personal",
        "X-TCE-User": "graph-test-user",
    }


def _event_payload(
    title: str,
    *,
    task_type: str = "debug",
    payload: dict | None = None,
) -> dict:
    now = datetime.now(tz=UTC).isoformat()
    return {
        "schema_version": 1,
        "ts": now,
        "actor": "user",
        "source": "cli",
        "domain": "coding",
        "task_type": task_type,
        "event_type": "TASK_STEP",
        "title": title,
        "payload": payload or {"summary": title},
        "context": {"project": "open-timeline-engine", "repo": "open-timeline-engine"},
        "inputs": {},
        "steps": [],
        "decision": None,
        "outcome": None,
        "style": None,
        "links": None,
        "tags": ["graph-test", "retry-policy"],
        "sensitivity": 1,
        "redaction_hints": [],
    }


@pytest.fixture()
def lite_client_graph(tmp_path) -> Iterator[TestClient]:
    settings = get_settings()
    settings.lite_db_path = str(tmp_path / "tce-lite-graph.db")
    settings.api_tokens = "lite-test-token"
    settings.default_operation_mode = "timeline_only"
    with TestClient(app) as client:
        yield client


def test_entity_extraction_is_queryable(lite_client_graph: TestClient) -> None:
    ingest = lite_client_graph.post(
        "/v1/events",
        json=_event_payload("Tune retry-policy for payments-service"),
        headers=_headers(),
    )
    assert ingest.status_code == 200

    entity_search = lite_client_graph.get(
        "/v1/graph/entities",
        params={"query": "payments-service", "k": 20},
        headers=_headers(),
    )
    assert entity_search.status_code == 200
    entities = entity_search.json()["entities"]
    assert entities
    assert any("payments-service" in item["key"] for item in entities)


def test_follows_relationship_created_for_event_sequence(lite_client_graph: TestClient) -> None:
    first = lite_client_graph.post(
        "/v1/events",
        json=_event_payload("Retry policy first pass", task_type="implement_feature"),
        headers=_headers(),
    )
    assert first.status_code == 200

    second = lite_client_graph.post(
        "/v1/events",
        json=_event_payload("Retry policy second pass", task_type="implement_feature"),
        headers=_headers(),
    )
    assert second.status_code == 200
    second_event_id = second.json()["event_id"]

    graph = lite_client_graph.get(f"/v1/graph/event/{second_event_id}", headers=_headers())
    assert graph.status_code == 200
    relationships = graph.json()["graph"]["relationships"]
    assert any(rel["type"] == "follows" for rel in relationships)


def test_fact_conflict_creates_contradiction_relationship(lite_client_graph: TestClient) -> None:
    first = lite_client_graph.post(
        "/v1/events",
        json=_event_payload(
            "Set deployment region east",
            payload={"summary": "region set", "facts": {"deployment_region": "us-east-1"}},
        ),
        headers=_headers(),
    )
    assert first.status_code == 200

    second = lite_client_graph.post(
        "/v1/events",
        json=_event_payload(
            "Set deployment region west",
            payload={"summary": "region updated", "facts": {"deployment_region": "us-west-2"}},
        ),
        headers=_headers(),
    )
    assert second.status_code == 200
    second_event_id = second.json()["event_id"]

    graph = lite_client_graph.get(f"/v1/graph/event/{second_event_id}", headers=_headers())
    assert graph.status_code == 200
    body = graph.json()["graph"]

    assert any(rel["type"] == "contradicts" for rel in body["relationships"])
    assert any(fact["key"] == "deployment_region" for fact in body["facts"])
