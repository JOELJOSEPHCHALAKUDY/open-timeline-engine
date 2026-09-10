from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from tce_shared.decision_policy import (
    AbstainReason,
    DecisionRequest,
    DecisionResult,
    DecisionStatus,
    ExposureState,
    decide,
)
from tce_shared.events import SafetyDecision, TakeoverClassification, TakeoverMode, TakeoverPolicy
from tce_shared.takeover import (
    _is_vague_objective,
    _latest_request,
    _strip_activation_prefix,
    build_next_action,
    compute_decision_confidence,
    ensure_takeover_response,
    evaluate_safety,
    resolve_objective,
    should_trigger_deliberation,
    update_recent_outcomes,
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


_POLICY_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _thin_evidence_row(index: int) -> dict[str, Any]:
    summary = "should i add a caching layer in front of the read path"
    return {
        "id": f"00000000-0000-0000-0000-{index:012d}",
        "ts": (_POLICY_NOW - timedelta(days=2)).isoformat(),
        "situation_type": "routine_task",
        "situation_summary": summary,
        "objective_text": summary,
        "constraints": {},
        "context_snapshot": {},
        "selected_choice": "add a caching layer",
        "action_taken": "add a caching layer",
        "evidence_source": "explicit",
        "lifecycle_status": "active",
        "learning_eligible": True,
        "valid_from": (_POLICY_NOW - timedelta(days=2)).isoformat(),
        "project_id": "proj-1",
    }


def _abstaining_policy() -> DecisionResult:
    """A real, unexposed abstention from the real policy — not a hand-built stub."""

    summary = "should i add a caching layer in front of the read path"
    request = DecisionRequest(
        decision_family="routine_task",
        situation_type="routine_task",
        situation_summary=summary,
        objective_text=summary,
        constraints={},
        context_snapshot={},
        candidate_options=("add a caching layer", "leave the read path alone"),
        evidence_rows=(_thin_evidence_row(1),),
        decision_at=_POLICY_NOW,
        workspace_id="ws",
        subject_user_id="subject",
        project_id="proj-1",
        episode_key="episode",
        evidence_revision="rev-1",
        evidence_cutoff_at=None,
        retrieval_version="full-knn-v1",
        model_id="model-a",
        runtime_version="runtime-1",
    )
    result = decide(request)
    assert result.status is DecisionStatus.ABSTAINED
    assert result.abstain_reason is AbstainReason.INADEQUATE_EVIDENCE
    assert result.exposed is False  # no qualification record exists, and that is the default
    return result


def test_exposed_abstention_hands_off() -> None:
    """Weak evidence used to produce the MOST confident text this function can emit.

    The turn was rewritten through ``build_decisive_response`` under
    ``weak_evidence_decisive``, and the resulting DECISIVE classification then scored higher
    in the certainty heuristic than a HANDOFF would have — so suppressing the uncertainty
    raised the confidence number that gates ``needs_human``.  An exposed abstention now asks
    instead.
    """

    policy = replace(_abstaining_policy(), exposed=True, exposure_state=ExposureState.EXPOSED)
    final, enforced, reason, classification = ensure_takeover_response(
        mode=TakeoverMode.TAKEOVER,
        text="No prior to go on; proceeding with conservative defaults.",
        task="implement caching layer",
        takeover_context={"objective": "implement caching layer", "turn_count": 3},
        advice={"evidence_strength": "weak"},
        policy=policy,
    )
    assert enforced is True
    assert reason == "policy_abstention"
    assert classification == TakeoverClassification.HANDOFF
    assert final == policy.reason_for_asking
    assert "ACT NOW" not in final


def test_unexposed_abstention_is_byte_identical_to_no_policy() -> None:
    """The unit half of the compatibility gate.

    Every decision family is unqualified today, so every turn takes this path.  If an
    unqualified family changed the turn, P4 would be a product shutdown rather than a
    measurement.
    """

    policy = _abstaining_policy()
    assert policy.exposed is False
    kwargs: dict[str, Any] = dict(
        mode=TakeoverMode.TAKEOVER,
        text="No prior to go on; proceeding with conservative defaults.",
        task="implement caching layer",
        takeover_context={"objective": "implement caching layer", "turn_count": 3},
        advice={"evidence_strength": "weak"},
    )
    assert ensure_takeover_response(**kwargs, policy=policy) == ensure_takeover_response(**kwargs, policy=None)


def test_weak_evidence_no_longer_triggers_a_decisive_rewrite() -> None:
    """The reason code ``weak_evidence_decisive`` is gone, and so is the branch behind it."""

    final, enforced, reason, _ = ensure_takeover_response(
        mode=TakeoverMode.TAKEOVER,
        text="Applying the migration and running the smoke suite now.",
        task="implement caching layer",
        takeover_context={"objective": "implement caching layer", "turn_count": 3},
        advice={"evidence_strength": "weak"},
    )
    assert reason != "weak_evidence_decisive"
    assert enforced is False
    assert final == "Applying the migration and running the smoke suite now."


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


def test_is_self_referential_event_rejects_tce_own_telemetry() -> None:
    """Goal discovery must not propose working on records of TCE working.

    Observed live: 36 of 40 discovered goals were "Interaction: takeover_step - ..."
    and 2 were "Directive succeeded: takeover_step". TCE writes an interaction event
    on every call, so its own telemetry is the highest-frequency row in the events
    table and crowds real work out of the discovery window entirely.
    """
    from tce_shared.takeover import is_self_referential_event

    assert is_self_referential_event(
        "Interaction: takeover_step - beru take over", "interaction_takeover_step"
    )
    assert is_self_referential_event("Directive succeeded: takeover_step", "coding")
    # Any directive lifecycle state, not just the happy one — "Directive failed:"
    # slipped through a prefix-list that only named "succeeded" and came back as a
    # top-ranked goal.
    assert is_self_referential_event("Directive failed: takeover_step", "coding")
    assert is_self_referential_event("Directive retried: takeover_step", "coding")
    assert is_self_referential_event("Interaction: search_events", "interaction_search_events")
    assert is_self_referential_event("anything", "interaction_context_bundle_cache_hit")
    assert is_self_referential_event("", "human_input_backfill")


def test_is_self_referential_event_keeps_real_work() -> None:
    """Genuine user work must survive the filter."""
    from tce_shared.takeover import is_self_referential_event

    assert not is_self_referential_event(
        "Historical claude user input: i still lot of ci fails", "human_input_backfill"
    )
    assert not is_self_referential_event("Fix the retrieval regression", "implement_feature")
    assert not is_self_referential_event("Add a logo for open-witness-engine", "human_input_backfill")
    # "interaction" as a substring of ordinary prose must not trip the filter
    assert not is_self_referential_event("Improve user interaction in the dashboard", "coding")


def test_strip_activation_prefix_removes_sentence_ending_punctuation() -> None:
    """A sentence break after the activation phrase must not leak into the objective.

    Observed live: "beru take over. Objective: ..." produced an objective of
    ". Objective: ...", because only ",;:- " was stripped. The leading period then
    shows up in every directive and in the executor's marching orders.
    """
    assert (
        _strip_activation_prefix("beru take over. Objective: count the markdown files")
        == "Objective: count the markdown files"
    )
    assert _strip_activation_prefix("beru take over! fix the auth flow") == "fix the auth flow"
    assert _strip_activation_prefix("beru take over? review the diff") == "review the diff"


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
