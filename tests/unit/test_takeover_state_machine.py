from __future__ import annotations

from tce_shared.events import SafetyDecision, TakeoverClassification, TakeoverMode, TakeoverPolicy
from tce_shared.takeover import (
    _is_vague_objective,
    _latest_request,
    _strip_activation_prefix,
    build_next_action,
    compute_decision_confidence,
    ensure_takeover_response,
    evaluate_safety,
    should_trigger_deliberation,
    update_recent_outcomes,
    resolve_objective,
)


def test_takeover_enforcement_passes_through_decisive_in_takeover_mode() -> None:
    """In takeover mode, questions are classified as DECISIVE and passed through."""
    final, enforced, reason, classification = ensure_takeover_response(
        mode=TakeoverMode.TAKEOVER,
        text="Do you want me to continue?",
        task="finish rollout",
        takeover_context={"objective": "finish rollout"},
        advice={"do": ["apply migration", "run smoke"]},
    )
    # In takeover mode, classify_text biases toward DECISIVE — question is passed through
    assert classification.value == "decisive"
    assert enforced is False
    assert final == "Do you want me to continue?"


def test_takeover_enforcement_weak_evidence_produces_task_specific_directive() -> None:
    """Weak evidence should produce task-specific directives, not generic boilerplate."""
    final, enforced, reason, classification = ensure_takeover_response(
        mode=TakeoverMode.TAKEOVER,
        text="No strong prior found; proceeding with conservative defaults.",
        task="implement caching layer",
        takeover_context={"objective": "implement caching layer", "turn_count": 3},
        advice={
            "evidence_strength": "weak",
            "clone_context": {
                "clone_prompt": "full prompt here",
                "situation_type": "routine_task",
                "fingerprint": {
                    "decision_making": {"risk_tolerance": "high"},
                    "priorities": {"speed_vs_quality": 0.3},
                },
                "similar_observations": [],
                "session_context": {},
            },
        },
    )
    assert enforced is True
    assert reason == "weak_evidence_decisive"
    assert "implement caching layer" in final
    assert "ACT NOW" in final  # must contain action instruction
    assert "Move fast" in final  # speed_vs_quality < 0.4


def test_takeover_safety_confirm_and_deny_paths() -> None:
    policy = TakeoverPolicy(
        safety_policy="high-risk-pause",
        confirm_keyword="confirm",
        deny_keyword="abort",
        timeout_minutes=30,
        auto_handoff_on_question=True,
    )
    context = {"pending_safety": {"reason": "destructive_filesystem", "pending_response": "rm -rf /tmp"}}
    decision_confirm, reason_confirm = evaluate_safety(policy, "confirm", "rm -rf /tmp", context)
    decision_deny, reason_deny = evaluate_safety(policy, "abort", "rm -rf /tmp", context)
    assert decision_confirm == SafetyDecision.ALLOW
    assert reason_confirm == "confirmed_high_risk"
    assert decision_deny == SafetyDecision.BLOCKED
    assert reason_deny == "denied_high_risk"


# --- Fix 1: Activation phrases not echoed as Execute directive ---

def test_latest_request_filters_activation_phrases() -> None:
    """Activation phrases should not appear in _latest_request."""
    ctx = {"last_user_message": "hey beru can you take over further implementation for me"}
    result = _latest_request(ctx)
    assert "take over" not in result.lower()
    # Should extract "further implementation for me" or return ""
    assert result == "" or "implementation" in result.lower()


def test_latest_request_passes_through_real_messages() -> None:
    """Real messages should pass through _latest_request unchanged."""
    ctx = {"last_user_message": "implement the authentication system with JWT tokens"}
    result = _latest_request(ctx)
    assert "authentication" in result.lower()
    assert "JWT" in result


def test_latest_request_empty_on_control_messages() -> None:
    """Control messages like 'continue' should return empty."""
    ctx = {"last_user_message": "continue"}
    result = _latest_request(ctx)
    assert result == ""


def test_strip_activation_prefix_extracts_content() -> None:
    """Should extract content after 'take over' prefix."""
    result = _strip_activation_prefix("hey beru take over, implement the auth flow")
    assert "implement the auth flow" == result
    result2 = _strip_activation_prefix("hey beru take over further implementation for me")
    assert result2 == "further implementation for me"


def test_strip_activation_prefix_empty_on_pure_activation() -> None:
    """Pure activation phrase should return empty string."""
    result = _strip_activation_prefix("hey beru take over")
    assert result == ""


# --- Fix 2: Vague/meta objectives detected and replaced ---

def test_is_vague_objective_detects_meta() -> None:
    """Self-referential meta objectives should be detected as vague."""
    assert _is_vague_objective("can you make it continues and autonous") is True
    assert _is_vague_objective("can you make it continues and autonomous") is True
    assert _is_vague_objective("current objective") is True
    assert _is_vague_objective("continue") is True
    assert _is_vague_objective("keep going") is True
    assert _is_vague_objective("do it") is True  # too short


def test_is_vague_objective_accepts_real_objectives() -> None:
    """Real task objectives should not be detected as vague."""
    assert _is_vague_objective("implement caching layer for API responses") is False
    assert _is_vague_objective("fix the authentication bug in login flow") is False
    assert _is_vague_objective("add dark mode toggle to settings page") is False


def test_resolve_objective_replaces_vague_with_activation_content() -> None:
    """When existing objective is vague, activation phrase content should replace it."""
    ctx = {"objective": "can you make it continues and autonous"}
    result = resolve_objective(
        message="hey beru take over, implement the user dashboard",
        task=None,
        takeover_context=ctx,
    )
    assert "implement the user dashboard" in result.lower()


def test_resolve_objective_replaces_vague_with_real_message() -> None:
    """When existing objective is vague and message is real, replace objective."""
    ctx = {"objective": "current objective"}
    result = resolve_objective(
        message="build the REST API endpoints",
        task=None,
        takeover_context=ctx,
    )
    assert "REST API endpoints" in result


def test_resolve_objective_preserves_good_objective_on_activation() -> None:
    """When existing objective is good, activation phrase should not replace it."""
    ctx = {"objective": "implement the caching layer"}
    result = resolve_objective(
        message="hey beru take over",
        task=None,
        takeover_context=ctx,
    )
    assert result == "implement the caching layer"


# --- Fix 3: Integration test for build_decisive_response with filtered activation ---

def test_build_decisive_response_no_activation_in_execute() -> None:
    """build_decisive_response should not echo activation phrases in Execute field."""
    from tce_shared.takeover import build_decisive_response

    ctx = {
        "objective": "implement authentication system",
        "last_user_message": "hey beru take over further implementation for me",
        "turn_count": 5,
    }
    result = build_decisive_response(
        task="implement authentication system",
        takeover_context=ctx,
        advice={
            "clone_context": {
                "clone_prompt": "full prompt",
                "situation_type": "feature_implementation",
                "fingerprint": {"decision_making": {"risk_tolerance": "moderate"}, "priorities": {"speed_vs_quality": 0.5}},
                "similar_observations": [{"id": "1"}],
                "session_context": {},
            }
        },
    )
    # Should NOT contain "take over" anywhere in directive
    assert "take over" not in result.lower()
    assert "implement authentication system" in result.lower()
    assert "ACT NOW" in result


def test_decision_confidence_and_deliberation_gating() -> None:
    confidence, components = compute_decision_confidence(
        objective="implement takeover autonomy v3 pipeline",
        message="hey beru take over and implement takeover autonomy v3 pipeline",
        classification=TakeoverClassification.DECISIVE,
        working_set={"evidence_count": 8, "top_patterns": [{"confidence": 0.81}]},
        recent_outcomes=[{"result": "success"}, {"result": "success"}],
    )
    assert confidence > 0.6
    assert "objective_clarity" in components
    assert should_trigger_deliberation(
        decision_confidence=0.55,
        objective_changed=False,
        turn_count=1,
        recent_failures=0,
        message="continue",
    )


def test_update_outcomes_and_next_action() -> None:
    outcomes, autonomy = update_recent_outcomes(
        recent_outcomes=[{"result": "success"}],
        result="failure",
        turn=2,
        latency_ms=55,
    )
    assert len(outcomes) >= 2
    assert 0.0 <= autonomy <= 1.0
    next_action = build_next_action(
        objective="verify tests",
        mode=TakeoverMode.TAKEOVER,
        safety_decision=SafetyDecision.ALLOW,
        needs_human=False,
    )
    assert next_action["kind"] == "execute"
