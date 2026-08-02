from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tce_lite_api.config import get_settings
from tce_lite_api.main import app


def _headers(
    *,
    role: str = "executor",
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
        "payload": {
            "task": "advisor runtime fallback",
            "summary": "advisor runtime fallback cleanup",
            "files": ["services/tce_api/tce_api/main.py"],
            "anchors": [{"file": "services/tce_api/tce_api/main.py", "line": 3900, "symbol": "handoff_resume"}],
            "outcome": {
                "status": "succeeded",
                "next_step": "run runtime regression tests",
            },
        },
        "context": {"project": "open-timeline-engine"},
        "inputs": {},
        "steps": [],
        "decision": {
            "choice": "use deterministic fallback ordering",
            "alternatives": ["provider-first"],
            "rationale": "keep runtime behavior stable across providers",
            "signals_used": ["timeline", "handoff"],
        },
        "outcome": {
            "success": True,
            "metrics": {"tests": 3},
            "followups": ["run runtime regression tests"],
        },
        "style": None,
        "links": None,
        "tags": ["autonomy-upgrade"],
        "sensitivity": 1,
        "redaction_hints": [],
    }


@pytest.fixture()
def lite_client(tmp_path) -> Generator[TestClient, None, None]:
    settings = get_settings()
    original: dict[str, Any] = {
        "lite_db_path": settings.lite_db_path,
        "api_tokens": settings.api_tokens,
        "default_operation_mode": settings.default_operation_mode,
        "context_tiers_enabled": settings.context_tiers_enabled,
        "intent_retrieval_enabled": settings.intent_retrieval_enabled,
        "retry_feedback_enabled": settings.retry_feedback_enabled,
        "profile_tuning_enabled": settings.profile_tuning_enabled,
    }
    settings.lite_db_path = str(tmp_path / "tce-lite-autonomy-upgrade.db")
    settings.api_tokens = "lite-test-token"
    settings.default_operation_mode = "clone_advisor"
    settings.context_tiers_enabled = False
    settings.intent_retrieval_enabled = False
    settings.retry_feedback_enabled = False
    settings.profile_tuning_enabled = False
    with TestClient(app) as client:
        yield client
    settings.lite_db_path = original["lite_db_path"]
    settings.api_tokens = original["api_tokens"]
    settings.default_operation_mode = original["default_operation_mode"]
    settings.context_tiers_enabled = original["context_tiers_enabled"]
    settings.intent_retrieval_enabled = original["intent_retrieval_enabled"]
    settings.retry_feedback_enabled = original["retry_feedback_enabled"]
    settings.profile_tuning_enabled = original["profile_tuning_enabled"]


def test_flags_keep_context_tiers_and_planner_off_by_default(lite_client: TestClient) -> None:
    assert lite_client.post("/v1/events", json=_event_payload("Advisor runtime fallback step"), headers=_headers()).status_code == 200

    search = lite_client.post(
        "/v1/search",
        json={"query": "continue codex work on advisor runtime fallback", "filters": {"domain": "coding"}, "k": 5},
        headers=_headers(),
    )
    assert search.status_code == 200
    search_body = search.json()
    assert search_body["metadata"]["planner_used"] is False
    assert search_body["metadata"]["context_tier_used"] == "l2"

    brief = lite_client.post(
        "/v1/context/brief",
        json={
            "task": "continue codex work on advisor runtime fallback",
            "app_context": {"domain": "coding"},
            "constraints": {"k": 5},
            "max_items": 5,
        },
        headers=_headers(),
    )
    assert brief.status_code == 200
    brief_body = brief.json()
    assert brief_body["context_tier_used"] == "l2"
    assert brief_body["planner_used"] is False
    assert brief_body["current_state"][0]["text"].startswith("Advisor runtime fallback step")


def test_flags_enable_tiered_context_and_intent_planner_metadata(lite_client: TestClient) -> None:
    settings = get_settings()
    settings.context_tiers_enabled = True
    settings.intent_retrieval_enabled = True

    assert lite_client.post("/v1/events", json=_event_payload("Advisor runtime fallback step"), headers=_headers()).status_code == 200
    assert lite_client.post("/v1/events", json=_event_payload("Advisor runtime fallback validation"), headers=_headers()).status_code == 200

    search = lite_client.post(
        "/v1/search",
        json={"query": "continue codex work on advisor runtime fallback", "filters": {"domain": "coding"}, "k": 5},
        headers=_headers(),
    )
    assert search.status_code == 200
    search_body = search.json()
    assert search_body["metadata"]["planner_used"] is True
    assert search_body["metadata"]["subquery_count"] >= 2
    assert search_body["metadata"]["context_tier_used"] == "l1"
    assert search_body["metadata"]["summary_coverage"] > 0.0
    assert any(hit["summary_l0"] for hit in search_body["result"]["hits"])
    assert any(
        hit["summary_l1"]["files"] == ["services/tce_api/tce_api/main.py"]
        for hit in search_body["result"]["hits"]
    )

    bundle = lite_client.post(
        "/v1/context_bundle",
        json={
            "task": "continue codex work on advisor runtime fallback",
            "app_context": {"domain": "coding"},
            "constraints": {"k": 5},
        },
        headers=_headers(),
    )
    assert bundle.status_code == 200
    bundle_body = bundle.json()
    assert bundle_body["context_tier_used"] == "l1"
    assert bundle_body["planner_used"] is True
    assert bundle_body["policy"]["context_tier_used"] == "l1"
    assert bundle_body["evidence_events"][0]["summary_l0"]

    brief = lite_client.post(
        "/v1/context/brief",
        json={
            "task": "continue codex work on advisor runtime fallback",
            "app_context": {"domain": "coding"},
            "constraints": {"k": 5},
            "max_items": 5,
        },
        headers=_headers(),
    )
    assert brief.status_code == 200
    brief_body = brief.json()
    assert brief_body["context_tier_used"] == "l1"
    assert brief_body["planner_used"] is True
    assert any("next: run runtime regression tests" in item["text"] for item in brief_body["current_state"])


def test_retry_feedback_is_returned_and_persisted_for_retry_directives(lite_client: TestClient) -> None:
    settings = get_settings()
    settings.retry_feedback_enabled = True

    assert lite_client.post("/v1/events", json=_event_payload("Takeover retry seed"), headers=_headers(role="user", consumer="takeover-user")).status_code == 200

    session_id = f"autonomy-upgrade-{uuid.uuid4().hex[:8]}"
    step = lite_client.post(
        "/v1/takeover/step",
        json={
            "message": "hey beru take over",
            "session_id": session_id,
            "persona_mode": "shadow",
            "task": "stabilize advisor runtime fallback",
            "app_context": {"domain": "coding"},
            "constraints": {"k": 6},
            "interaction_id": "autonomy-upgrade-retry-1",
            "executor_output": "continue",
            "allow_fallback": True,
        },
        headers=_headers(),
    )
    assert step.status_code == 200

    execution_status = lite_client.get(
        "/v1/takeover/execution/status",
        params={"session_id": session_id},
        headers=_headers(),
    )
    assert execution_status.status_code == 200
    pending = execution_status.json().get("pending", [])
    directive_id = pending[0]["directive_id"] if pending else step.json().get("directive_id")
    assert directive_id, step.json()

    claim = lite_client.post(
        "/v1/takeover/execution/claim",
        json={"session_id": session_id, "directive_id": directive_id, "claimed_by": "integration-test"},
        headers=_headers(role="executor", consumer="codex-executor"),
    )
    assert claim.status_code == 200

    report = lite_client.post(
        "/v1/takeover/execution/report",
        json={
            "session_id": session_id,
            "directive_id": directive_id,
            "state": "failed",
            "result": "runtime probe still failed",
            "failure_reason": "provider fallback mismatch",
            "details": {
                "tools_called": ["rg", "pytest"],
                "key_findings": ["provider order still unstable"],
                "files_modified": ["services/tce_api/tce_api/main.py"],
                "reasoning_summary": "Need alternate path before retry.",
            },
        },
        headers=_headers(role="executor", consumer="codex-executor"),
    )
    assert report.status_code == 200
    report_body = report.json()
    assert report_body["retry_scheduled"] is True
    assert report_body["retry_directive_id"]
    assert report_body["retry_feedback"]["failure_class"]
    assert report_body["retry_feedback"]["recommended_retry_strategy"]

    follow_up_status = lite_client.get(
        "/v1/takeover/execution/status",
        params={"session_id": session_id},
        headers=_headers(),
    )
    assert follow_up_status.status_code == 200
    retry_directive = next(
        item
        for item in follow_up_status.json().get("pending", [])
        if item.get("directive_id") == report_body["retry_directive_id"]
    )
    assert retry_directive["meta"]["retry_feedback"]["failure_reason_short"] == report_body["retry_feedback"]["failure_reason_short"]
