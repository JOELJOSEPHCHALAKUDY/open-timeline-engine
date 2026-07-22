from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from tce_mcp.server import (
    behavior_decisions_markdown,
    behavior_evidence_json,
    behavior_review_html,
    current_behavior_json,
    current_behavior_markdown,
    mcp,
)
from tce_mcp.tools import (
    assign_behavior_projection_pilot,
    consume_capability_grant,
    mine_behavior_processes,
    predict_behavior,
    record_behavior_counterfactual,
    record_behavior_evidence,
    report_behavior_projection_pilot_outcome,
    request_capability_grant,
    run_behavior_fidelity_eval,
)


def test_record_behavior_evidence_returns_observation_citation() -> None:
    with patch("tce_mcp.tools.client") as mock_client:
        mock_client.record_behavior_evidence.return_value = {
            "observation_id": "00000000-0000-0000-0000-000000000001",
            "stored": True,
        }
        result = record_behavior_evidence(
            situation_summary="Choose storage",
            objective="Keep data local",
            selected_choice="SQLite",
            rationale="No external infrastructure",
        )
    assert result["citations"] == ["00000000-0000-0000-0000-000000000001"]


def test_prediction_and_evaluation_tools_preserve_citations() -> None:
    with patch("tce_mcp.tools.client") as mock_client:
        mock_client.predict_behavior.return_value = {
            "predicted_choice": None,
            "abstained": True,
            "citations": ["00000000-0000-0000-0000-000000000001"],
        }
        predicted = predict_behavior(
            situation_summary="Unknown requirement",
            objective="Decide whether to continue",
        )
        mock_client.run_behavior_evaluation.return_value = {
            "case_results": [{"observation_id": "00000000-0000-0000-0000-000000000002"}]
        }
        evaluated = run_behavior_fidelity_eval()
    assert predicted["citations"] == ["00000000-0000-0000-0000-000000000001"]
    assert evaluated["citations"] == ["00000000-0000-0000-0000-000000000002"]


def test_control_plane_tools_preserve_contracts_and_process_citations() -> None:
    with patch("tce_mcp.tools.client") as mock_client:
        mock_client.create_capability_grant.return_value = {"grant_id": "grant-1", "decision": "allow"}
        grant = request_capability_grant("filesystem.read", "open", "src/app.py")
        mock_client.consume_capability_grant.return_value = {"grant_id": "grant-1", "authorized": True}
        consumed = consume_capability_grant("grant-1", "x" * 32, "filesystem.read", "open", "src/app.py")
        mock_client.mine_behavior_processes.return_value = {
            "models": [{"evidence_ids": ["event-1", "event-2"]}]
        }
        processes = mine_behavior_processes()
        mock_client.create_behavior_counterfactual.return_value = {"counterfactual_id": "counter-1"}
        counterfactual = record_behavior_counterfactual(
            "minimal patch",
            "broad rewrite",
            "higher regression risk",
            observation_id="observation-1",
        )
    assert grant["result"]["decision"] == "allow"
    assert consumed["result"]["authorized"] is True
    assert processes["citations"] == ["event-1", "event-2"]
    assert counterfactual["citations"] == ["observation-1"]


def test_behavior_projection_resources_are_registered_with_native_mime_types() -> None:
    templates = asyncio.run(mcp.list_resource_templates())
    resources = {str(item.uriTemplate): item.mimeType for item in templates}

    assert resources["tce://workspace/{workspace_id}/behavior/{subject_id}/current.md"] == "text/markdown"
    assert resources["tce://workspace/{workspace_id}/behavior/{subject_id}/current.json"] == "application/json"
    assert resources[
        "tce://workspace/{workspace_id}/behavior/{subject_id}/review.html"
    ] == "text/html"
    assert resources[
        "tce://workspace/{workspace_id}/behavior/{subject_id}/decisions/{topic}.md"
    ] == "text/markdown"
    assert resources[
        "tce://workspace/{workspace_id}/behavior/{subject_id}/evidence/{observation_id}.json"
    ] == "application/json"


def test_behavior_projection_resources_enforce_configured_scope_and_return_content() -> None:
    scope = SimpleNamespace(mcp_workspace_id="workspace-a", mcp_effective_behavior_subject_id="human-a")
    with (
        patch("tce_mcp.server.get_settings", return_value=scope),
        patch("tce_mcp.server.tools.get_current_behavior_projection") as current,
        patch("tce_mcp.server.tools.get_behavior_decisions_projection") as decisions,
        patch("tce_mcp.server.tools.get_behavior_evidence_projection") as evidence,
        patch("tce_mcp.server.tools.get_behavior_review_projection") as review,
    ):
        current.side_effect = lambda format_name: {"content": f"current:{format_name}"}
        decisions.return_value = {"content": "decision-content"}
        evidence.return_value = {"content": "evidence-content"}
        review.return_value = {"content": "review-content"}

        assert current_behavior_markdown("workspace-a", "human-a") == "current:markdown"
        assert current_behavior_json("workspace-a", "human-a") == "current:json"
        assert behavior_decisions_markdown("workspace-a", "human-a", "production") == "decision-content"
        assert behavior_evidence_json("workspace-a", "human-a", "observation-1") == "evidence-content"
        assert behavior_review_html("workspace-a", "human-a") == "review-content"
        with pytest.raises(ValueError, match="scope does not match"):
            current_behavior_markdown("other-workspace", "human-a")

    decisions.assert_called_once_with(topic="production", format_name="markdown")
    evidence.assert_called_once_with(observation_id="observation-1", format_name="json")


def test_behavior_projection_pilot_tools_preserve_assignment_and_evidence_citations() -> None:
    with patch("tce_mcp.tools.client") as mock_client:
        mock_client.assign_behavior_projection_pilot.return_value = {
            "assignment_id": "00000000-0000-0000-0000-000000000010",
            "variant": "projection_index",
            "citations": ["00000000-0000-0000-0000-000000000001"],
        }
        assignment = assign_behavior_projection_pilot(
            trial_key="trial-1",
            situation_summary="Choose release scope",
            objective="Release without regression",
        )
        mock_client.report_behavior_projection_pilot_outcome.return_value = {
            "assignment_id": "00000000-0000-0000-0000-000000000010",
            "outcome_id": "00000000-0000-0000-0000-000000000020",
            "recorded": True,
        }
        outcome = report_behavior_projection_pilot_outcome(
            assignment_id="00000000-0000-0000-0000-000000000010",
            agent_choice="minimal patch",
            actual_choice="minimal patch",
            used_evidence_ids=["00000000-0000-0000-0000-000000000001"],
        )

    assert assignment["citations"] == ["00000000-0000-0000-0000-000000000001"]
    assert outcome["citations"] == ["00000000-0000-0000-0000-000000000001"]


def test_behavior_projection_pilot_tools_are_registered() -> None:
    registered = {item.name for item in asyncio.run(mcp.list_tools())}
    assert "tce.assign_behavior_projection_pilot" in registered
    assert "tce.report_behavior_projection_pilot_outcome" in registered
    assert "tce.get_behavior_projection_pilot_status" in registered
