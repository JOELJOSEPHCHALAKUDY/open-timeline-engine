from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tce_shared.autonomy_goals import (
    adjust_consultative_threshold,
    classify_risk_tier,
    continuity_health,
    evaluate_execution_permit,
    score_goal,
)
from tce_shared.events import AutonomyPolicyProfile, AutonomyRiskTier, ExecutionPermitDecision


def test_goal_score_clamped_and_weighted() -> None:
    score = score_goal(urgency=1.0, recency=1.0, blocker_impact=1.0, success_probability=1.0)
    assert score == 1.0
    low = score_goal(urgency=0.0, recency=0.0, blocker_impact=0.0, success_probability=0.0)
    assert low == 0.0


def test_risk_classification_detects_critical_tokens() -> None:
    risk = classify_risk_tier(
        action_kind="command_run",
        target_paths=["scripts/cleanup.sh"],
        command_preview="rm -rf /tmp/data",
        estimated_change_size=3,
    )
    assert risk == AutonomyRiskTier.CRITICAL


def test_execution_permit_matrix_consultative_medium_scope() -> None:
    decision, _reason = evaluate_execution_permit(
        policy_profile=AutonomyPolicyProfile.HUMAN_CONSULTATIVE,
        risk_tier=AutonomyRiskTier.MEDIUM,
        estimated_change_size=10,
        role="executor",
        sensitive_path_hit=False,
    )
    assert decision == ExecutionPermitDecision.ALLOW


def test_execution_permit_blocks_advisor_writes() -> None:
    decision, _reason = evaluate_execution_permit(
        policy_profile=AutonomyPolicyProfile.HUMAN_AGGRESSIVE,
        risk_tier=AutonomyRiskTier.LOW,
        estimated_change_size=1,
        role="advisor",
        sensitive_path_hit=False,
    )
    assert decision == ExecutionPermitDecision.BLOCKED


def test_threshold_adjustment_reacts_to_failures() -> None:
    threshold = adjust_consultative_threshold(
        0.5,
        recent_outcomes=[{"result": "failure"}, {"result": "blocked"}, {"result": "success"}],
        semantic_ratio=0.7,
    )
    assert threshold > 0.5


def test_continuity_health_flags_large_gap() -> None:
    now = datetime.now(tz=UTC)
    violations, ok = continuity_health(
        was_active=True,
        last_message_at=now - timedelta(minutes=30),
        now=now,
        previous_violations=2,
        max_gap_seconds=300,
    )
    assert violations >= 3
    assert ok is False
