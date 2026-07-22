from __future__ import annotations

from unittest.mock import patch

from tce_mcp.tools import (
    consume_capability_grant,
    mine_behavior_processes,
    predict_behavior,
    record_behavior_counterfactual,
    record_behavior_evidence,
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
