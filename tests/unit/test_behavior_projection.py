from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from tce_shared.behavior_projection import BehaviorProjectionNotFound, build_behavior_projection

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _row(
    observation_id: uuid.UUID,
    *,
    days_ago: int = 0,
    source: str = "explicit",
    lifecycle: str = "active",
    eligible: bool = True,
    summary: str = "Choose a safe production deployment",
    objective: str = "Deploy the API without regression",
) -> dict:
    ts = NOW - timedelta(days=days_ago)
    return {
        "id": str(observation_id),
        "ts": ts,
        "situation_type": "production_change",
        "situation_summary": summary,
        "objective_text": objective,
        "context_snapshot": {"component": "api", "api_key": "plain-context-secret"},
        "constraints": {"token": "plain-constraint-secret", "risk": "production"},
        "available_choices": ["minimal patch", "broad rewrite"],
        "selected_choice": "minimal patch",
        "response_reasoning": "Keep token=plain-text-secret out of memory and minimize risk",
        "action_taken": "Patch the affected module",
        "outcome": "Scoped tests passed",
        "memory_class": "decision",
        "evidence_source": source,
        "lifecycle_status": lifecycle,
        "learning_eligible": eligible,
        "confidence": 0.93,
        "storage_score": 0.91,
        "storage_decision": "learn",
        "valid_from": ts,
    }


def test_current_projection_is_deterministic_redacted_and_lifecycle_aware() -> None:
    superseded_id = uuid.uuid4()
    successor_id = uuid.uuid4()
    active_id = uuid.uuid4()
    expired_id = uuid.uuid4()
    superseded = _row(superseded_id, days_ago=4, lifecycle="superseded")
    superseded["superseded_by"] = str(successor_id)
    successor = _row(successor_id, days_ago=2, summary="Correct production deployment policy")
    active = _row(active_id, days_ago=1, source="inferred", summary="Prefer scoped production tests")
    expired = _row(expired_id, days_ago=3)
    expired["valid_until"] = NOW - timedelta(days=1)

    first = build_behavior_projection(
        [superseded, successor, active, expired],
        workspace_id="workspace-a",
        subject_user_id="human-a",
        at=NOW,
    )
    second = build_behavior_projection(
        [superseded, successor, active, expired],
        workspace_id="workspace-a",
        subject_user_id="human-a",
        at=NOW,
    )

    assert first == second
    assert first["evidence_count"] == 2
    assert first["trust_level"] == "mixed"
    assert first["read_only"] is True
    assert first["projection_learning_eligible"] is False
    assert str(successor_id) in first["source_evidence_ids"]
    assert str(active_id) in first["source_evidence_ids"]
    assert str(superseded_id) not in first["content"]
    assert str(expired_id) not in first["content"]
    assert "plain-context-secret" not in first["content"]
    assert "plain-constraint-secret" not in first["content"]
    assert "plain-text-secret" not in first["content"]
    assert "<REDACTED" in first["content"]
    _, separator, body = first["content"].partition("\n---\n\n")
    assert separator
    assert hashlib.sha256(body.encode()).hexdigest() == first["content_sha256"]


def test_json_and_topic_projections_have_verifiable_body_hashes() -> None:
    production_id = uuid.uuid4()
    editor_id = uuid.uuid4()
    rows = [
        _row(production_id, summary="Production rollback policy"),
        _row(
            editor_id,
            summary="Editor formatting preference",
            objective="Format Python code consistently",
        ),
    ]
    rows[1]["situation_type"] = "editor_workflow"
    projection = build_behavior_projection(
        rows,
        workspace_id="workspace-a",
        subject_user_id="human-a",
        view="decisions",
        topic="production",
        format_name="json",
        at=NOW,
    )
    document = json.loads(projection["content"])
    canonical_body = json.dumps(
        document["projection"],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    assert projection["evidence_count"] == 1
    assert projection["source_evidence_ids"] == [str(production_id)]
    assert hashlib.sha256(canonical_body.encode()).hexdigest() == projection["content_sha256"]
    assert document["metadata"]["source_revision"] == projection["source_revision"]
    assert document["metadata"]["projection_learning_eligible"] is False


def test_evidence_projection_preserves_historical_lifecycle_for_citation_drilldown() -> None:
    observation_id = uuid.uuid4()
    row = _row(observation_id, lifecycle="superseded")
    row["superseded_by"] = str(uuid.uuid4())

    projection = build_behavior_projection(
        [row],
        workspace_id="workspace-a",
        subject_user_id="human-a",
        view="evidence",
        observation_id=str(observation_id),
        format_name="json",
        at=NOW,
    )

    assert projection["evidence_count"] == 1
    assert projection["trust_level"] == "historical"
    assert json.loads(projection["content"])["projection"]["evidence"][0]["lifecycle_status"] == "superseded"


def test_projection_rejects_invalid_view_and_missing_evidence() -> None:
    with pytest.raises(ValueError, match="view must be"):
        build_behavior_projection([], workspace_id="workspace-a", subject_user_id="human-a", view="unknown")
    with pytest.raises(BehaviorProjectionNotFound):
        build_behavior_projection(
            [],
            workspace_id="workspace-a",
            subject_user_id="human-a",
            view="evidence",
            observation_id=str(uuid.uuid4()),
        )


def test_html_review_is_passive_escaped_and_includes_historical_evidence() -> None:
    active_id = uuid.uuid4()
    historical_id = uuid.uuid4()
    active = _row(active_id, summary='<script src="https://evil.invalid/x.js">attack</script>')
    historical = _row(historical_id, lifecycle="superseded", eligible=False)
    historical["superseded_by"] = str(active_id)

    projection = build_behavior_projection(
        [active, historical],
        workspace_id="workspace-a",
        subject_user_id="human-a",
        view="review",
        format_name="html",
        at=NOW,
    )

    lowered = projection["content"].lower()
    assert projection["format"] == "html"
    assert projection["mime_type"] == "text/html; charset=utf-8"
    assert set(projection["source_evidence_ids"]) == {str(active_id), str(historical_id)}
    assert "<script" not in lowered
    assert "javascript:" not in lowered
    assert "&lt;script" in lowered
    assert "content-security-policy" in lowered
    assert hashlib.sha256(projection["content"].encode()).hexdigest() == projection["content_sha256"]


def test_html_is_restricted_to_review_projection() -> None:
    with pytest.raises(ValueError, match="only available for review"):
        build_behavior_projection(
            [],
            workspace_id="workspace-a",
            subject_user_id="human-a",
            view="current",
            format_name="html",
        )
    with pytest.raises(ValueError, match="require html"):
        build_behavior_projection(
            [],
            workspace_id="workspace-a",
            subject_user_id="human-a",
            view="review",
            format_name="json",
        )
