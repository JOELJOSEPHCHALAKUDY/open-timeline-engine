from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from tce_shared.behavior_pilot import (
    assign_behavior_pilot_variant,
    behavior_pilot_status,
    prepare_behavior_pilot_context,
    sanitize_behavior_pilot_payload,
)
from tce_shared.events import BehaviorPilotArmMetrics, BehaviorPilotOutcomeRequest, BehaviorPilotVariant

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _evidence() -> dict:
    return {
        "id": str(uuid.uuid4()),
        "ts": NOW - timedelta(days=1),
        "situation_type": "production_change",
        "situation_summary": "Choose a safe deployment",
        "objective_text": "Deploy without regression",
        "context_snapshot": {"api_key": "plain-secret-value"},
        "constraints": {"risk": "production"},
        "available_choices": ["minimal patch", "rewrite"],
        "selected_choice": "minimal patch",
        "response_reasoning": "Keep the change reversible",
        "action_taken": "Patch one package",
        "outcome": "Scoped tests passed",
        "memory_class": "decision",
        "evidence_source": "explicit",
        "lifecycle_status": "active",
        "learning_eligible": True,
        "confidence": 0.95,
    }


def _request() -> dict:
    return {
        "trial_key": "trial-1",
        "situation_type": "production_change",
        "situation_summary": "Choose deployment scope",
        "objective": "Deploy without regression",
        "constraints": {},
        "context_snapshot": {},
        "candidate_choices": ["minimal patch", "rewrite"],
    }


def test_assignment_is_stable_and_context_arms_are_isolated() -> None:
    first = assign_behavior_pilot_variant(
        workspace_id="workspace-a",
        subject_user_id="human-a",
        trial_key="stable-trial",
        assignment_salt="pilot-salt",
    )
    second = assign_behavior_pilot_variant(
        workspace_id="workspace-a",
        subject_user_id="human-a",
        trial_key="stable-trial",
        assignment_salt="pilot-salt",
    )
    assert first == second

    row = _evidence()
    contexts = {
        variant: prepare_behavior_pilot_context(
            [row],
            workspace_id="workspace-a",
            subject_user_id="human-a",
            request=_request(),
            variant=variant,
            at=NOW,
        )
        for variant in BehaviorPilotVariant
    }
    assert contexts[BehaviorPilotVariant.NO_MEMORY]["citations"] == []
    assert str(row["id"]) not in str(contexts[BehaviorPilotVariant.NO_MEMORY]["context_payload"])
    for variant in (
        BehaviorPilotVariant.CANONICAL_STRUCTURED,
        BehaviorPilotVariant.MARKDOWN_PROJECTION,
        BehaviorPilotVariant.PROJECTION_INDEX,
    ):
        assert contexts[variant]["citations"] == [row["id"]]
        assert "plain-secret-value" not in str(contexts[variant]["context_payload"])


def test_pilot_input_redaction_is_recursive_and_bounded() -> None:
    sanitized, redacted = sanitize_behavior_pilot_payload(
        {"objective": "Use token=plain-token-value", "context": {"api_key": "secret"}}
    )
    assert redacted is True
    assert "plain-token-value" not in str(sanitized)
    assert "secret" not in str(sanitized)


def test_projection_index_quotes_identity_components_in_citations() -> None:
    context = prepare_behavior_pilot_context(
        [_evidence()],
        workspace_id="workspace/with space",
        subject_user_id="human#one",
        request=_request(),
        variant=BehaviorPilotVariant.PROJECTION_INDEX,
        at=NOW,
    )

    citation = context["context_payload"]["items"][0]["citation"]
    assert citation.startswith("tce://workspace/workspace%2Fwith%20space/behavior/human%23one/")


def _completed_rows(*, malicious: bool = False) -> list[dict]:
    rows: list[dict] = []
    for variant in BehaviorPilotVariant:
        for index in range(30):
            assigned_at = NOW - timedelta(days=29, minutes=index)
            rows.append(
                {
                    "assignment_id": str(uuid.uuid4()),
                    "variant": variant.value,
                    "assigned_at": assigned_at,
                    "expires_at": NOW + timedelta(days=1),
                    "injected_tokens": 50,
                    "retrieval_latency_ms": 10,
                    "outcome_id": str(uuid.uuid4()),
                    "agent_choice": "minimal patch",
                    "top3_choices": ["minimal patch"],
                    "actual_choice": "minimal patch",
                    "abstained": False,
                    "correction_required": False,
                    "outcome_regret": False,
                    "irrelevant_personalization": False,
                    "malicious_memory_activated": malicious and not rows,
                    "stale_evidence_used": False,
                    "reported_at": NOW - timedelta(minutes=index),
                }
            )
    return rows


def test_pilot_gate_requires_real_window_samples_latency_and_safety() -> None:
    ready = behavior_pilot_status(_completed_rows(), now=NOW)
    assert ready["status"] == "ready_for_review"
    assert ready["gate"]["evaluation_ready"] is True
    assert ready["gate"]["quality_passed"] is True

    collecting = behavior_pilot_status(_completed_rows(), now=NOW - timedelta(days=28))
    assert collecting["status"] == "collecting"
    assert collecting["gate"]["window_complete"] is False

    unsafe = behavior_pilot_status(_completed_rows(malicious=True), now=NOW)
    assert unsafe["status"] == "failed_safety"
    assert unsafe["gate"]["safety_passed"] is False



def test_no_self_reported_metric_survives() -> None:
    """G6.  The three metrics the system computed about itself are gone, and the safety clause
    no longer passes on an empty corpus.

    ``calibration_brier`` read ``agent_confidence``, a number the party under test posted about
    its own answer, and it scored **0.0 — a perfect Brier score — for a reporter that posts 1.0
    when it is right and 0.0 when it is wrong**: perfectly inverted, perfectly rewarded.  The two
    similarity means came from the same self-report.  ``safety_passed`` read ``True`` over zero
    rows, so an empty pilot looked like a safe one.
    """

    outcome_fields = set(BehaviorPilotOutcomeRequest.model_fields)
    metric_fields = set(BehaviorPilotArmMetrics.model_fields)
    for gone in ("agent_confidence", "action_similarity", "workflow_similarity"):
        assert gone not in outcome_fields, gone
    for gone in ("calibration_brier", "mean_action_similarity", "mean_workflow_similarity"):
        assert gone not in metric_fields, gone

    empty = behavior_pilot_status([], now=NOW)
    assert empty["gate"]["safety_passed"] == "not_computable"
    assert empty["gate"]["evaluation_ready"] is False
    assert empty["status"] != "failed_safety"
    assert any("not computable" in reason for reason in empty["gate"]["reasons"])
    for arm in empty["arms"]:
        for gone in ("calibration_brier", "mean_action_similarity", "mean_workflow_similarity"):
            assert gone not in arm, gone


def test_a_populated_safe_corpus_still_reports_a_real_boolean() -> None:
    """Three-valued does not mean never-true: with completed rows the clause is a boolean again."""

    populated = behavior_pilot_status(_completed_rows(), now=NOW)
    assert populated["gate"]["safety_passed"] is True

