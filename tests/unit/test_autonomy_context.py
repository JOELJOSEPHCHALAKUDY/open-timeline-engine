from __future__ import annotations

from tce_shared.autonomy_context import (
    autonomy_profile_tuning,
    build_retry_feedback,
    plan_retrieval_subqueries,
    propagate_episode_score,
    summarize_event_record,
    summary_coverage_ratio,
)


def test_summarize_event_record_builds_redacted_tiers() -> None:
    summary_l0, summary_l1 = summarize_event_record(
        title="Advisor runtime fallback",
        task_type="implement_feature",
        domain="coding",
        payload={
            "task": "stabilize advisor runtime fallback token=secret-token",
            "files": [
                "services/tce_api/tce_api/main.py",
                "services/tce_api/tce_api/main.py",
            ],
            "anchors": [{"file": "services/tce_api/tce_api/main.py", "line": 3912, "symbol": "handoff_resume"}],
            "outcome": {
                "status": "succeeded",
                "next_step": "run scoped verification api_key=12345",
            },
        },
        decision={
            "rationale": "keep the fallback order deterministic secret=my-secret",
        },
        outcome=None,
    )

    assert summary_l0.startswith("Advisor runtime fallback")
    assert "succeeded" in summary_l0
    assert summary_l1["files"] == ["services/tce_api/tce_api/main.py"]
    assert summary_l1["anchors"][0]["line"] == 3912
    assert "12345" not in summary_l1["next_step"]
    assert "my-secret" not in summary_l1["decision"]
    assert "REDACTED" in summary_l1["next_step"]
    assert "REDACTED" in summary_l1["decision"]


def test_plan_retrieval_subqueries_respects_flag_and_continuity_intent() -> None:
    disabled = plan_retrieval_subqueries(
        "continue codex work on advisor runtime fallback",
        enabled=False,
    )
    enabled = plan_retrieval_subqueries(
        "continue codex work on advisor runtime fallback",
        enabled=True,
    )

    assert disabled == [{"label": "objective", "query": "continue codex work on advisor runtime fallback"}]
    assert enabled[0] == {"label": "objective", "query": "advisor runtime fallback"}
    assert [item["label"] for item in enabled] == ["objective", "decision_history", "constraints_workflow"]


def test_summary_coverage_and_episode_propagation_are_deterministic() -> None:
    coverage = summary_coverage_ratio(
        [
            {"summary_l0": "event one"},
            {"summary_l0": ""},
            {"summary_l0": "event three"},
        ]
    )

    assert coverage == 0.6667
    assert propagate_episode_score(0.4, 0.8) == 0.5
    assert propagate_episode_score(0.4, 0.0) == 0.4


def test_retry_feedback_and_profile_tuning_defaults() -> None:
    feedback = build_retry_feedback(
        action_kind="edit files",
        failure_class="tool_error",
        failure_reason="token=my-token broke the command",
        retry_strategy="alternate_path",
    )

    assert feedback["failure_class"] == "tool_error"
    assert feedback["recommended_retry_strategy"] == "alternate_path"
    assert "my-token" not in feedback["failure_reason_short"]
    assert "REDACTED" in feedback["failure_reason_short"]

    assert autonomy_profile_tuning("human_safe") == {
        "retrieval_trigger_delta": 0.06,
        "needs_human_delta": 0.08,
        "evidence_floor_delta": 1,
        "advisor_cadence_turns": 1,
    }
    assert autonomy_profile_tuning("human_consultative") == {
        "retrieval_trigger_delta": 0.0,
        "needs_human_delta": 0.0,
        "evidence_floor_delta": 0,
        "advisor_cadence_turns": 2,
    }
