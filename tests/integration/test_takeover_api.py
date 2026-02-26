from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.main import app


def _headers(role: str = "advisor", consumer: str = "takeover-test-advisor") -> dict[str, str]:
    return {
        "Authorization": "Bearer lite-test-token",
        "X-TCE-Consumer": consumer,
        "X-TCE-Role": role,
    }


def _seed_event_payload() -> dict:
    return {
        "schema_version": 1,
        "ts": datetime.now(tz=UTC).isoformat(),
        "actor": "user",
        "source": "cli",
        "domain": "coding",
        "task_type": "implement_feature",
        "event_type": "TASK_STEP",
        "title": "seed event for takeover",
        "payload": {"summary": "seed"},
        "context": {"project": "open-timeline-engine"},
        "inputs": {},
        "steps": [],
        "decision": None,
        "outcome": None,
        "style": None,
        "links": None,
        "tags": ["takeover-test"],
        "sensitivity": 1,
        "redaction_hints": [],
    }


@pytest.fixture()
def lite_client(tmp_path) -> TestClient:
    settings = get_settings()
    settings.lite_db_path = str(tmp_path / "tce-lite-takeover-test.db")
    settings.api_tokens = "lite-test-token"
    settings.default_operation_mode = "clone_advisor"
    with TestClient(app) as client:
        yield client


def test_takeover_state_step_and_reset(lite_client: TestClient) -> None:
    ingest = lite_client.post("/v1/events", json=_seed_event_payload(), headers=_headers(role="user", consumer="takeover-user"))
    assert ingest.status_code == 200

    session_id = f"takeover-{uuid.uuid4().hex[:8]}"
    state = lite_client.get(
        "/v1/takeover/state",
        params={"session_id": session_id, "persona_mode": "shadow"},
        headers=_headers(),
    )
    assert state.status_code == 200
    assert state.json()["active"] is False

    preload = lite_client.post(
        "/v1/takeover/preload",
        json={
            "session_id": session_id,
            "persona_mode": "shadow",
            "task": "validate takeover",
            "app_context": {"domain": "coding"},
            "constraints": {"k": 8},
        },
        headers=_headers(),
    )
    assert preload.status_code == 200
    preload_body = preload.json()
    assert preload_body["objective_hash"]
    assert isinstance(preload_body["working_set_json"], dict)
    assert isinstance(preload_body["working_set_json"].get("citation_snippets", []), list)

    step = lite_client.post(
        "/v1/takeover/step",
        json={
            "message": "hey beru take over",
            "session_id": session_id,
            "persona_mode": "shadow",
            "task": "validate takeover",
            "app_context": {"domain": "coding"},
            "constraints": {"k": 8},
            "interaction_id": "takeover-int-1",
            "executor_output": "If you want I can continue?",
            "allow_fallback": True,
        },
        headers=_headers(),
    )
    assert step.status_code == 200
    body = step.json()
    assert body["state"]["active"] is True
    assert body["action"] in {"advisor_takeover", "advisor_suggest"}
    assert "clone_advice" in body
    assert "decision_confidence" in body
    assert "decision_source" in body
    assert "next_action" in body
    assert "latency_breakdown_ms" in body
    assert "needs_human" in body
    assert "execution_permit_required" in body
    assert "continuity_ok" in body

    execution_status_before = lite_client.get(
        "/v1/takeover/execution/status",
        params={"session_id": session_id},
        headers=_headers(),
    )
    assert execution_status_before.status_code == 200
    pending_directives = execution_status_before.json().get("pending", [])
    directive_id = pending_directives[0]["directive_id"] if pending_directives else body.get("directive_id")
    if directive_id:
        claim = lite_client.post(
            "/v1/takeover/execution/claim",
            json={
                "session_id": session_id,
                "directive_id": directive_id,
                "claimed_by": "integration-test",
            },
            headers=_headers(role="executor", consumer="takeover-test-advisor"),
        )
        assert claim.status_code == 200

        report = lite_client.post(
            "/v1/takeover/execution/report",
            json={
                "session_id": session_id,
                "directive_id": directive_id,
                "state": "succeeded",
                "result": "applied minimal change",
                "details": {
                    "tools_called": ["rg", "pytest"],
                    "key_findings": ["retrieval quality improved", "no safety regressions"],
                    "files_modified": ["services/tce_api/tce_api/main.py"],
                    "reasoning_summary": "Applied bounded enrichment with redaction and strict caps.",
                },
            },
            headers=_headers(role="executor", consumer="takeover-test-advisor"),
        )
        assert report.status_code == 200

        execution_status = lite_client.get(
            "/v1/takeover/execution/status",
            params={"session_id": session_id},
            headers=_headers(),
        )
        assert execution_status.status_code == 200
        recent = execution_status.json().get("recent", [])
        matched = next((item for item in recent if item.get("directive_id") == directive_id), None)
        assert matched is not None
        meta = matched.get("meta", {})
        assert isinstance(meta.get("execution_transcript_contract", {}), dict)
        assert meta.get("execution_transcript_contract", {}).get("enabled") is True
        assert isinstance(meta.get("execution_transcript", {}), dict)

    feedback = lite_client.post(
        "/v1/takeover/feedback",
        json={
            "session_id": session_id,
            "turn": 1,
            "objective_hash": preload_body["objective_hash"],
            "action_kind": "execute",
            "result": "success",
            "latency_ms": 42,
            "details": {"source": "integration-test"},
        },
        headers=_headers(),
    )
    assert feedback.status_code == 200
    feedback_body = feedback.json()
    assert feedback_body["session_id"] == session_id
    assert isinstance(feedback_body["autonomy_score"], float)
    assert isinstance(feedback_body["recent_outcomes_json"], list)

    discover = lite_client.post(
        "/v1/takeover/goals/discover",
        json={"session_id": session_id, "include_open_discovery": True},
        headers=_headers(),
    )
    assert discover.status_code == 200
    discover_body = discover.json()
    assert discover_body["session_id"] == session_id
    assert isinstance(discover_body["goals"], list)

    goals = lite_client.get(
        "/v1/takeover/goals",
        params={"session_id": session_id},
        headers=_headers(),
    )
    assert goals.status_code == 200
    goals_body = goals.json()
    assert goals_body["session_id"] == session_id

    if goals_body["goals"]:
        goal_id = goals_body["goals"][0]["id"]
        select_goal = lite_client.post(
            f"/v1/takeover/goals/{goal_id}/select",
            json={"session_id": session_id},
            headers=_headers(),
        )
        assert select_goal.status_code == 200
        assert select_goal.json()["id"] == goal_id

    permit = lite_client.post(
        "/v1/takeover/permit",
        json={
            "session_id": session_id,
            "action_kind": "edit",
            "target_paths": ["README.md"],
            "command_preview": "edit README",
            "estimated_change_size": 10,
        },
        headers=_headers(),
    )
    assert permit.status_code == 200
    permit_body = permit.json()
    assert permit_body["decision"] in {"allow", "confirm_required", "blocked"}
    assert permit_body["permit_id"]

    resolve = lite_client.post(
        "/v1/takeover/permit/resolve",
        json={
            "permit_id": permit_body["permit_id"],
            "approved": True,
            "confirmed_by": "integration-test",
        },
        headers=_headers(),
    )
    assert resolve.status_code == 200
    assert resolve.json()["decision"] in {"allow", "blocked"}

    autonomy_status = lite_client.get(
        "/v1/takeover/autonomy/status",
        params={"session_id": session_id},
        headers=_headers(),
    )
    assert autonomy_status.status_code == 200
    status_body = autonomy_status.json()
    assert status_body["session_id"] == session_id
    assert "continuity_ok" in status_body
    assert "queue_size" in status_body

    reset = lite_client.post(
        "/v1/takeover/reset",
        params={"session_id": session_id, "persona_mode": "shadow"},
        headers=_headers(),
    )
    assert reset.status_code == 200
    assert reset.json()["active"] is False
