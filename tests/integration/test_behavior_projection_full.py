from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

from fastapi.testclient import TestClient
from tce_api.config import get_settings
from tce_api.db import get_db
from tce_api.main import app


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-TCE-Consumer": "codex-executor",
        "X-TCE-Role": "executor",
        "X-TCE-Workspace": "projection-workspace",
        "X-TCE-User": "codex-executor",
        "X-TCE-Behavior-Subject": "codex-executor",
    }


def test_full_projection_endpoint_uses_canonical_store_and_shared_contract() -> None:
    settings = get_settings()
    original_enabled = settings.behavior_projections_enabled
    original_access_mode = settings.workspace_access_mode
    settings.behavior_projections_enabled = True
    settings.workspace_access_mode = "compat"
    token = next(iter(settings.token_set))
    observation_id = uuid.uuid4()
    row = {
        "id": observation_id,
        "ts": datetime(2026, 7, 22, tzinfo=UTC),
        "situation_type": "production_change",
        "situation_summary": "Choose a safe API release",
        "objective_text": "Release without regression",
        "selected_choice": "minimal verified patch",
        "response_reasoning": "Keep the change reversible",
        "action_taken": "Patch the focused module",
        "outcome": "Scoped tests passed",
        "memory_class": "decision",
        "evidence_source": "explicit",
        "lifecycle_status": "active",
        "learning_eligible": True,
        "confidence": 0.95,
    }
    db_sentinel = object()

    def fake_db():
        yield db_sentinel

    app.dependency_overrides[get_db] = fake_db
    try:
        with (
            patch("tce_api.main.load_behavior_evidence", return_value=[row]) as load_evidence,
            patch("tce_api.main.write_audit_log") as write_audit,
        ):
            response = TestClient(app).get(
                "/v1/behavior/projections/current?format=json",
                headers=_headers(token),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.behavior_projections_enabled = original_enabled
        settings.workspace_access_mode = original_access_mode

    assert response.status_code == 200, response.text
    assert response.json()["source_evidence_ids"] == [str(observation_id)]
    assert response.json()["format"] == "json"
    load_evidence.assert_called_once_with(
        db_sentinel,
        workspace_id="projection-workspace",
        subject_user_id="codex-executor",
        limit=5000,
        eligible_only=True,
    )
    audit_call = write_audit.call_args.kwargs
    assert audit_call["action"] == "behavior_projection_read"
    assert audit_call["query"]["source_evidence_ids"] == [str(observation_id)]
    assert audit_call["policy_decisions"]["projection_learning_eligible"] is False


def test_full_html_review_uses_all_evidence_and_sets_passive_response_headers() -> None:
    settings = get_settings()
    original_enabled = settings.behavior_projections_enabled
    original_access_mode = settings.workspace_access_mode
    settings.behavior_projections_enabled = True
    settings.workspace_access_mode = "compat"
    token = next(iter(settings.token_set))
    observation_id = uuid.uuid4()
    row = {
        "id": observation_id,
        "ts": datetime(2026, 7, 22, tzinfo=UTC),
        "situation_type": "review",
        "situation_summary": "<script>attack</script>",
        "objective_text": "Review historical evidence",
        "selected_choice": "inspect",
        "response_reasoning": "Human review",
        "memory_class": "decision",
        "evidence_source": "inferred",
        "lifecycle_status": "superseded",
        "learning_eligible": False,
        "confidence": 0.4,
    }
    db_sentinel = object()

    def fake_db():
        yield db_sentinel

    app.dependency_overrides[get_db] = fake_db
    try:
        with (
            patch("tce_api.main.load_behavior_evidence", return_value=[row]) as load_evidence,
            patch("tce_api.main.write_audit_log"),
        ):
            response = TestClient(app).get(
                "/v1/behavior/projections/review.html",
                headers=_headers(token),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.behavior_projections_enabled = original_enabled
        settings.workspace_access_mode = original_access_mode

    assert response.status_code == 200, response.text
    assert "<script" not in response.text.lower()
    assert "&lt;script&gt;" in response.text.lower()
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    load_evidence.assert_called_once_with(
        db_sentinel,
        workspace_id="projection-workspace",
        subject_user_id="codex-executor",
        limit=5000,
        eligible_only=False,
    )


def test_full_projection_pilot_assignment_outcome_and_status_contract() -> None:
    settings = get_settings()
    original_pilot = settings.behavior_projection_pilot_enabled
    original_access_mode = settings.workspace_access_mode
    settings.behavior_projection_pilot_enabled = True
    settings.workspace_access_mode = "compat"
    token = next(iter(settings.token_set))
    observation_id = uuid.uuid4()
    assignment_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    row = {
        "id": observation_id,
        "ts": now,
        "situation_type": "production_change",
        "situation_summary": "Choose a safe release",
        "objective_text": "Release without regression",
        "selected_choice": "minimal patch",
        "response_reasoning": "Keep it reversible",
        "memory_class": "decision",
        "evidence_source": "explicit",
        "lifecycle_status": "active",
        "learning_eligible": True,
        "confidence": 0.95,
    }
    db_sentinel = object()

    def fake_db():
        yield db_sentinel

    def fake_assignment(*_args, **kwargs):
        return (
            {
                "id": assignment_id,
                "variant": kwargs["variant"],
                "assigned_at": kwargs["assigned_at"],
                "expires_at": kwargs["expires_at"],
                "context_payload": kwargs["context_payload"],
                "citations": kwargs["citations"],
                "source_revision": kwargs["source_revision"],
                "context_sha256": kwargs["context_sha256"],
                "injected_tokens": kwargs["injected_tokens"],
                "retrieval_latency_ms": kwargs["retrieval_latency_ms"],
            },
            True,
        )

    app.dependency_overrides[get_db] = fake_db
    try:
        with (
            patch("tce_api.main.load_behavior_evidence", return_value=[row]),
            patch("tce_api.main.create_or_get_behavior_pilot_assignment", side_effect=fake_assignment),
            patch("tce_api.main.record_behavior_pilot_outcome") as record_outcome,
            patch("tce_api.main.list_behavior_pilot_rows", return_value=[]),
            patch("tce_api.main.write_audit_log"),
        ):
            assigned = TestClient(app).post(
                "/v1/behavior/projections/pilot/assign",
                headers=_headers(token),
                json={
                    "trial_key": "full-pilot-1",
                    "situation_type": "production_change",
                    "situation_summary": "Choose release scope",
                    "objective": "Release without regression",
                    "candidate_choices": ["minimal patch", "rewrite"],
                },
            )
            record_outcome.return_value = (
                {
                    "id": outcome_id,
                    "assignment_id": assignment_id,
                    "stale_evidence_used": False,
                    "reported_at": now,
                },
                True,
            )
            outcome = TestClient(app).post(
                "/v1/behavior/projections/pilot/outcome",
                headers=_headers(token),
                json={
                    "assignment_id": str(assignment_id),
                    "agent_choice": "minimal patch",
                    "top3_choices": ["minimal patch"],
                    "actual_choice": "minimal patch",
                    "used_evidence_ids": [str(observation_id)],
                },
            )
            status = TestClient(app).get(
                "/v1/behavior/projections/pilot/status",
                headers=_headers(token),
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
        settings.behavior_projection_pilot_enabled = original_pilot
        settings.workspace_access_mode = original_access_mode

    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["assignment_id"] == str(assignment_id)
    assert assigned.json()["projection_learning_eligible"] is False
    assert outcome.status_code == 200, outcome.text
    assert outcome.json()["outcome_id"] == str(outcome_id)
    assert status.status_code == 200
    assert status.json()["status"] == "collecting"
    assert status.json()["gate"]["window_complete"] is False
