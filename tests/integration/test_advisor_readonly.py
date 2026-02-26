from datetime import UTC, datetime

from fastapi.testclient import TestClient
from tce_api.main import app


def test_advisor_cannot_write_events():
    client = TestClient(app)
    payload = {
        "schema_version": 1,
        "ts": datetime.now(UTC).isoformat(),
        "actor": "user",
        "source": "cli",
        "domain": "coding",
        "task_type": "debug",
        "event_type": "TASK_STEP",
        "title": "step",
        "payload": {},
        "context": {},
        "inputs": {},
        "steps": [],
        "decision": None,
        "outcome": None,
        "style": None,
        "links": None,
        "tags": [],
        "sensitivity": 1,
        "redaction_hints": [],
    }
    response = client.post(
        "/v1/events",
        json=payload,
        headers={
            "Authorization": "Bearer local-dev-token",
            "X-TCE-Role": "advisor",
            "X-TCE-Consumer": "claude-advisor",
        },
    )
    assert response.status_code == 403
