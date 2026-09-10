"""``decision_confidence`` must measure the DECISION, never how sure the system's own text sounded.

This is the twin of ``test_context_quality_not_self_reported.py`` and the last instance of the same
circularity.  ``decision_confidence < needs_human_threshold`` is the ``low_decision_confidence``
escalation cause in both backends -- the number that decides whether a turn asks its owner.  0.15
of it came from ``classifier_certainty``: ``classify_text`` run over ``body.executor_output``, the
system's own prior text, scoring DECISIVE at 0.92 against HANDOFF's 0.58.  So a turn that had
already committed to an answer scored higher on "should I check with a human", and the more
assertive the text the lower the bar.  Confidence in an answer is not evidence about whether to
ask.  The band the threshold moves in is 0.45-0.60 and the term's swing was 0.051, so it was small
-- and leaving one instance of a circularity because it is small is how it grows back.

Four properties:

1. **Structural** -- there is no ``classification`` parameter, so the term cannot return without
   changing a signature this test reads.
2. **Structural, on the call** -- neither backend hands the function a classification, and neither
   still classifies ``executor_output`` to feed it.  A signature check alone would pass while the
   caller folded the reply into ``message``.
3. **Weights** -- three terms, summing to exactly 1.0, in the ratios the four-term version gave
   them.  Removing a term and leaving the rest at 0.85 would deflate every turn toward the
   escalation line, which is a shutdown wearing a safety argument.
4. **Discriminating** -- each input moves the score by exactly its weight, and a well-founded turn
   still clears the hot threshold.  A test that only asserted "no classification input" would pass
   against ``return 0.0``.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from tce_shared.takeover import DECISION_CONFIDENCE_WEIGHTS, compute_decision_confidence

# ``Settings.takeover_needs_human_threshold_cold`` / ``_hot``: the band the escalation line moves
# in.  Stated here rather than imported so a config drift shows up as a red test.
COLD_THRESHOLD = 0.45
HOT_THRESHOLD = 0.60

BACKEND_MODULES = {
    "full": "services/tce_api/tce_api/main.py",
    "lite": "services/tce_lite_api/tce_lite_api/store.py",
}


def test_the_score_cannot_read_how_sure_the_answer_sounded() -> None:
    parameters = set(inspect.signature(compute_decision_confidence).parameters)
    assert parameters == {"objective", "message", "working_set", "recent_outcomes"}

    banned = {"classification", "classifier", "certainty", "response", "executor_output"}
    assert not (parameters & banned), (
        "decision_confidence is reading a property of the system's own answer again. That is how a "
        "turn lowers its own bar for asking its owner."
    )


def test_the_three_weights_are_the_old_ratios_and_still_sum_to_one() -> None:
    assert set(DECISION_CONFIDENCE_WEIGHTS) == {
        "objective_clarity",
        "evidence_strength",
        "outcome_stability",
    }
    assert sum(DECISION_CONFIDENCE_WEIGHTS.values()) == pytest.approx(1.0), (
        "The weights must sum to 1.0. Dropping a term and leaving the rest where they were "
        "deflates every turn toward the escalation line, which stops the product rather than "
        "gating it."
    )
    # The removed term's 0.15 was redistributed in proportion, so the surviving terms' ratios to
    # one another are exactly what the four-term version gave them.
    for name, weight in (("objective_clarity", 0.40), ("evidence_strength", 0.25), ("outcome_stability", 0.20)):
        assert DECISION_CONFIDENCE_WEIGHTS[name] == pytest.approx(weight / 0.85, abs=0.006)


@pytest.mark.parametrize("name", sorted(BACKEND_MODULES))
def test_neither_backend_hands_it_a_classification_of_its_own_text(name: str) -> None:
    source = (Path(__file__).resolve().parents[2] / BACKEND_MODULES[name]).read_text()
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "compute_decision_confidence"
    ]
    assert len(calls) == 1, f"{name}: expected exactly one confidence call, found {len(calls)}"

    passed = {str(keyword.arg) for keyword in calls[0].keywords}
    assert passed == {"objective", "message", "working_set", "recent_outcomes"}, (
        f"{name}: decision_confidence is being handed {sorted(passed)}."
    )
    for keyword in calls[0].keywords:
        segment = ast.get_source_segment(source, keyword.value) or ""
        assert "executor_output" not in segment, (
            f"{name}: the system's own prior text reached decision_confidence through "
            f"{keyword.arg}. The channel was closed, not narrowed."
        )

    # The self-read classification itself is gone, not merely disconnected: a `classify_text` call
    # over `executor_output` sitting unused is an invitation to wire it back up.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "classify_text":
            segment = ast.get_source_segment(source, node) or ""
            assert "executor_output" not in segment, (
                f"{name}: still classifying executor_output -- the system's own prior text."
            )


def test_each_context_input_moves_the_score_by_exactly_its_weight() -> None:
    """Discrimination: the score is a live function of its inputs, not a constant below the line."""

    floor, _ = compute_decision_confidence(objective="", message="", working_set={}, recent_outcomes=[])
    strong, components = compute_decision_confidence(
        objective="implement takeover autonomy v3 pipeline",
        message="beru take over and implement takeover autonomy v3 pipeline",
        working_set={"evidence_count": 8, "top_patterns": [{"confidence": 0.81}]},
        recent_outcomes=[{"result": "success"}, {"result": "success"}],
    )
    assert set(components) == set(DECISION_CONFIDENCE_WEIGHTS)
    assert strong > floor

    rebuilt = sum(DECISION_CONFIDENCE_WEIGHTS[key] * value for key, value in components.items())
    assert strong == pytest.approx(rebuilt, abs=5e-4)

    # A well-founded turn clears the top of the band with room, so the removal did not quietly
    # turn every turn into an escalation.
    assert strong > HOT_THRESHOLD


def test_a_turn_with_nothing_behind_it_lands_below_the_escalation_band() -> None:
    """The gate still catches the turns it exists for."""

    thin, _ = compute_decision_confidence(
        objective="ok",
        message="hmm",
        working_set={},
        recent_outcomes=[{"result": "failure"}, {"result": "failure"}],
    )
    assert thin < COLD_THRESHOLD, (
        f"a turn with a vague objective, no evidence and a failing history scored {thin}, which "
        "is above the coldest escalation threshold -- it would proceed instead of asking."
    )
