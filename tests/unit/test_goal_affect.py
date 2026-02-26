from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tce_shared.goal_affect import classify_goal_kind, compute_affective_scores, score_goal_affective


def _outcome(result: str, minutes_ago: int) -> dict[str, str]:
    ts = datetime.now(tz=UTC) - timedelta(minutes=minutes_ago)
    return {"result": result, "ts": ts.isoformat()}


def _assert_unit_interval(value: float) -> None:
    assert 0.0 <= float(value) <= 1.0


def test_compute_affective_scores_shape_and_ranges() -> None:
    scores = compute_affective_scores(
        source="open_discovery",
        evidence_count=4,
        rehearsal_count=5,
        confidence=0.62,
        urgency=0.55,
        recency=0.7,
        blocker_impact=0.35,
        success_probability=0.68,
        recent_outcomes=[
            _outcome("failure", 45),
            _outcome("success", 10),
        ],
        fingerprint={
            "decision_making": {"risk_tolerance": "moderate"},
            "context_switching": {"multitask_tolerance": "moderate"},
            "learning_style": {"exploration_vs_exploitation": 0.65},
            "priorities": {"top_recurring_concerns": ["reliability"]},
            "emotional_patterns": {"frustration_triggers": ["blocked"]},
        },
        similarity={"context_relevance": 0.82, "recent_event_affinity": 0.76},
    )

    assert scores["version"] == 1
    assert isinstance(scores["computed_at"], str)
    for section in ("temporal", "emotional", "behavioral", "meta", "human", "similarity"):
        assert section in scores

    assert scores["temporal"]["rehearsal_count"] == 5
    _assert_unit_interval(scores["temporal"]["forget"])
    _assert_unit_interval(scores["temporal"]["dream"])
    _assert_unit_interval(scores["temporal"]["rehearsal_strength"])

    _assert_unit_interval(scores["emotional"]["pain"])
    _assert_unit_interval(scores["emotional"]["happy"])
    _assert_unit_interval(scores["emotional"]["anger"])
    _assert_unit_interval(scores["emotional"]["anxiety"])

    _assert_unit_interval(scores["behavioral"]["distraction"])
    _assert_unit_interval(scores["behavioral"]["momentum"])
    _assert_unit_interval(scores["behavioral"]["completion_proximity"])

    _assert_unit_interval(scores["meta"]["exploration_bonus"])
    _assert_unit_interval(scores["meta"]["overwhelm"])

    _assert_unit_interval(scores["human"]["curiosity"])
    _assert_unit_interval(scores["human"]["social"])
    _assert_unit_interval(scores["human"]["identity"])
    _assert_unit_interval(scores["human"]["guilt"])
    _assert_unit_interval(scores["human"]["cognitive_load"])

    _assert_unit_interval(scores["similarity"]["context_relevance"])
    _assert_unit_interval(scores["similarity"]["recent_event_affinity"])
    _assert_unit_interval(scores["similarity"]["entity_overlap"])
    _assert_unit_interval(scores["similarity"]["pattern_confidence"])
    _assert_unit_interval(scores["similarity"]["nearest_goal_distance"])
    _assert_unit_interval(scores["similarity"]["evidence_depth"])


def test_classify_goal_kind_priority_and_defaults() -> None:
    assert classify_goal_kind({}) == "normal"
    assert classify_goal_kind({"meta": {"unknown_goal": True}}) == "unknown"
    assert classify_goal_kind({"meta": {"unknown_goal": True, "nothing_goal": True}}) == "nothing"


def test_overwhelmed_cold_start_marks_unknown_and_nothing() -> None:
    scores = compute_affective_scores(
        source="open_discovery",
        evidence_count=0,
        rehearsal_count=0,
        confidence=0.10,
        urgency=0.10,
        recency=0.0,
        blocker_impact=1.0,
        success_probability=0.0,
        recent_outcomes=[_outcome("failure", 1), _outcome("blocked", 2), _outcome("failure", 3)],
        fingerprint={
            "decision_making": {"risk_tolerance": "conservative"},
            "context_switching": {"multitask_tolerance": "low"},
            "learning_style": {"exploration_vs_exploitation": 0.5},
            "priorities": {"top_recurring_concerns": []},
            "emotional_patterns": {"frustration_triggers": ["blocked"]},
        },
        similarity=None,
    )

    assert scores["meta"]["unknown_goal"] is True
    assert scores["meta"]["nothing_goal"] is True
    assert scores["meta"]["overwhelm"] >= 0.6
    assert classify_goal_kind(scores) == "nothing"


def test_score_goal_affective_is_deterministic_for_fixed_inputs() -> None:
    affective_scores = {
        "temporal": {"forget": 0.42, "dream": 0.22, "rehearsal_strength": 0.58},
        "emotional": {"pain": 0.3, "anger": 0.25, "happy": 0.35, "anxiety": 0.4},
        "behavioral": {"distraction": 0.2, "momentum": 0.6, "completion_proximity": 0.55},
        "meta": {"exploration_bonus": 0.05, "nothing_goal": False},
        "human": {"curiosity": 0.5, "identity": 0.65, "guilt": 0.2, "social": 0.25, "cognitive_load": 0.45},
        "similarity": {"context_relevance": 0.72, "recent_event_affinity": 0.68, "pattern_confidence": 0.7},
    }

    score_a, breakdown_a = score_goal_affective(
        urgency=0.61,
        recency=0.57,
        blocker_impact=0.4,
        success_probability=0.66,
        affective_scores=affective_scores,
    )
    score_b, breakdown_b = score_goal_affective(
        urgency=0.61,
        recency=0.57,
        blocker_impact=0.4,
        success_probability=0.66,
        affective_scores=affective_scores,
    )

    assert score_a == score_b
    assert breakdown_a == breakdown_b
    _assert_unit_interval(score_a)
    assert breakdown_a["final"] == score_a

