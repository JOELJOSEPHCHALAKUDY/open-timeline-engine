from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from tce_shared.behavior_fidelity import (
    behavior_storage_gate,
    evaluate_behavior_fidelity,
    normalize_behavior_evidence,
    predict_behavior,
)
from tce_shared.events import BehaviorEvidenceRequest


def _evidence(index: int, *, selected: str = "minimal verified fix") -> dict:
    return {
        "id": f"00000000-0000-0000-0000-{index:012d}",
        "ts": (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)).isoformat(),
        "situation_type": "prioritization_needed",
        "situation_summary": "Choose implementation scope for a production bug",
        "objective_text": "Fix production bug without broad regression",
        "constraints": {"risk": "production"},
        "context_snapshot": {"repository": "tce"},
        "available_choices": ["minimal verified fix", "broad refactor"],
        "selected_choice": selected,
        "action_taken": "patch focused module and run scoped tests",
        "outcome": "tests passed",
        "outcome_sentiment": "positive",
        "confidence": 0.95,
        "evidence_source": "explicit",
        "memory_class": "preference",
        "lifecycle_status": "active",
        "learning_eligible": True,
        "valid_from": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        "superseded_by": None,
    }


def test_behavior_evidence_contract_requires_rationale_for_explicit_evidence() -> None:
    with pytest.raises(ValidationError):
        BehaviorEvidenceRequest(
            situation_summary="Choose storage",
            objective="Keep data local",
            selected_choice="SQLite",
        )


def test_normalization_redacts_secrets_before_storage() -> None:
    normalized = normalize_behavior_evidence(
        {
            "situation_type": "token=type-secret-value",
            "situation_summary": "Use token=secret-value-12345",
            "objective": "Configure sk-testSecretToken1234567890",
            "context_snapshot": {"authorization": "Bearer nested-context-secret"},
            "constraints": {"api_key": "nested-constraint-secret"},
            "selected_choice": "local",
            "rationale": "Avoid api_key=plain-secret-value",
            "outcome_sentiment": "token=sentiment-secret",
            "evidence_source": "explicit",
        }
    )
    assert normalized["redaction_applied"] is True
    assert "secret-value-12345" not in str(normalized)
    assert "plain-secret-value" not in str(normalized)
    assert "nested-context-secret" not in str(normalized)
    assert "nested-constraint-secret" not in str(normalized)
    assert "sentiment-secret" not in str(normalized)


def test_storage_gate_keeps_weak_inference_audit_only() -> None:
    gate = behavior_storage_gate(
        {
            "evidence_source": "backfill",
            "memory_class": "hypothesis",
            "confidence": 0.1,
            "lifecycle_status": "active",
        },
        threshold=0.55,
    )
    assert gate["learning_eligible"] is False
    assert gate["decision"] == "audit_only"


def test_prediction_ignores_superseded_evidence() -> None:
    stale = _evidence(1, selected="broad refactor")
    stale["superseded_by"] = "00000000-0000-0000-0000-000000000002"
    result = predict_behavior(
        [stale, _evidence(2)],
        {
            "situation_type": "prioritization_needed",
            "situation_summary": "Choose implementation scope for a production bug",
            "objective_text": "Fix production bug without broad regression",
        },
        candidate_choices=["minimal verified fix", "broad refactor"],
    )
    assert result["predicted_choice"] == "minimal verified fix"
    assert result["citations"] == ["00000000-0000-0000-0000-000000000002"]


def test_historical_prediction_does_not_apply_future_supersession() -> None:
    stale = _evidence(1, selected="broad refactor")
    stale["lifecycle_status"] = "superseded"
    stale["superseded_by"] = "00000000-0000-0000-0000-000000000002"
    historical = predict_behavior(
        [stale],
        {
            "situation_type": "prioritization_needed",
            "situation_summary": "Choose implementation scope for a production bug",
            "objective_text": "Fix production bug without broad regression",
        },
        candidate_choices=["minimal verified fix", "broad refactor"],
        historical_as_of=datetime(2026, 1, 2, 12, tzinfo=UTC),
    )
    assert historical["predicted_choice"] == "broad refactor"

    current = predict_behavior(
        [stale],
        {
            "situation_type": "prioritization_needed",
            "situation_summary": "Choose implementation scope for a production bug",
            "objective_text": "Fix production bug without broad regression",
        },
        candidate_choices=["minimal verified fix", "broad refactor"],
    )
    assert current["predicted_choice"] is None
    assert current["abstained"] is True


def test_prediction_ignores_confirmed_contradicted_evidence() -> None:
    stale = _evidence(1, selected="broad refactor")
    correction = _evidence(2)
    correction["evidence_source"] = "correction"
    correction["contradicts_observation_ids"] = [stale["id"]]
    result = predict_behavior(
        [stale, correction],
        {
            "situation_type": "prioritization_needed",
            "situation_summary": "Choose implementation scope for a production bug",
            "objective_text": "Fix production bug without broad regression",
        },
        candidate_choices=["minimal verified fix", "broad refactor"],
    )
    assert result["predicted_choice"] == "minimal verified fix"
    assert result["citations"] == [correction["id"]]


def test_chronological_evaluation_is_deterministic_and_reports_real_metrics() -> None:
    rows = [_evidence(index) for index in range(1, 31)]
    first = evaluate_behavior_fidelity(rows, holdout_ratio=0.30, min_train=5)
    second = evaluate_behavior_fidelity(rows, holdout_ratio=0.30, min_train=5)
    assert first["status"] == "completed"
    assert first["metrics"] == second["metrics"]
    assert first["metrics"]["evaluation_count"] == 9
    assert first["metrics"]["top1_accuracy"] == 1.0
    assert first["metrics"]["majority_baseline_accuracy"] == 1.0
    assert first["gate"]["passed"] is False  # The production gate requires 30 held-out cases.


def test_trivial_majority_accuracy_cannot_unlock_autonomy() -> None:
    rows = [_evidence(index) for index in range(1, 101)]
    result = evaluate_behavior_fidelity(rows, holdout_ratio=0.30, min_train=5)
    assert result["metrics"]["evaluation_count"] == 30
    assert result["metrics"]["top1_accuracy"] == 1.0
    assert result["metrics"]["lift_over_majority"] == 0.0
    assert result["gate"]["passed"] is False
    assert result["gate"]["thresholds"]["lift_over_majority"] == 0.05


def test_context_dependent_choices_can_pass_production_gate() -> None:
    rows = []
    for index in range(1, 121):
        frontend = index % 2 == 0
        context_tag = f"module-{chr(97 + (index // 26))}{chr(97 + (index % 26))}"
        domain = (
            f"frontend css layout browser {context_tag}"
            if frontend
            else f"backend database transaction sql {context_tag}"
        )
        choice = "visual regression test" if frontend else "database transaction test"
        row = _evidence(index, selected=choice)
        row.update(
            {
                "situation_type": "verification_choice",
                "situation_summary": f"Choose verification for {domain}",
                "objective_text": f"Validate {domain} change",
                "available_choices": ["visual regression test", "database transaction test"],
                "action_taken": choice,
            }
        )
        rows.append(row)

    result = evaluate_behavior_fidelity(rows, holdout_ratio=0.25, min_train=5)
    assert result["metrics"]["evaluation_count"] == 30
    assert result["metrics"]["unique_evaluation_contexts"] == 30
    assert result["metrics"]["lift_over_majority"] == 0.5
    assert result["gate"]["passed"] is True


def test_prediction_never_returns_choice_outside_candidate_set() -> None:
    evidence = _evidence(1, selected="rewrite everything")
    result = predict_behavior(
        [evidence],
        {
            "situation_type": "prioritization_needed",
            "situation_summary": "Choose implementation scope for a production bug",
            "objective_text": "Fix production bug without broad regression",
        },
        candidate_choices=["minimal verified fix", "ask for clarification"],
    )

    assert result["predicted_choice"] is None
    assert result["abstained"] is True
    assert result["ranked_choices"] == []
