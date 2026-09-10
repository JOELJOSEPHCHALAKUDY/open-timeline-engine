"""``context_quality_score`` must measure the CONTEXT, and a cold corpus must reach the owner.

The defect this pins is the one that survives every other fix in P4: the score named for the
quality of the context took 0.40 of its value from ``decision_confidence``, one of whose four
terms is ``classifier_certainty`` -- how the system classified its own reply.  The reply fed the
score; the score gates bounded retrieval and the ``needs_human`` escalation.  So the lowest-
evidence turns, which produce the most assertive text, scored DECISIVE at 0.92 against HANDOFF's
0.58 and *raised* the very gate that existed to catch them.  A system that can talk itself past
its own safety gate has no safety gate.

Three properties, each of which fails if the circularity comes back or the score drifts back over
the escalation line:

1. **Structural** -- the function has no way to read the answer.  There is no ``confidence``,
   ``classification`` or ``response`` parameter, so a future edit cannot reintroduce the term
   without changing the signature, which this test reads.
2. **Numeric** -- a cold corpus scores below ``context_retrieval_escalate_score``, with margin.
   That inequality is what makes ``poor_retrieval_quality`` fire and the turn ask its owner.
3. **Discriminating** -- the score is not a constant that happens to sit below the line.  Each
   context input moves it by exactly its weight, and a corpus with real, fresh, corroborated
   evidence clears the line comfortably.  A test that only asserted "low" would still pass if
   the score were hardcoded to zero, which would be a product shutdown rather than a gate.
"""

from __future__ import annotations

import inspect

import pytest
from tce_shared.takeover import CONTEXT_QUALITY_WEIGHTS, compute_context_quality_score

# The default both backends ship (``Settings.context_retrieval_escalate_score``).  A turn whose
# retrieval fired and whose quality is below this asks the human instead of continuing.
ESCALATE_SCORE = 0.52
# ``Settings.context_retrieval_trigger_score`` -- below this, bounded retrieval runs first.
TRIGGER_SCORE = 0.68

# What an empty corpus actually presents: no decision evidence above the similarity floor, the
# recency floor for "there is nothing to be fresh", and no failures yet to destabilise anything.
COLD_EVIDENCE = 0.0
COLD_RECENCY = 0.2
COLD_STABILITY = 1.0


def test_the_score_cannot_read_the_answer_it_is_scoring() -> None:
    """The circularity is excluded by the signature, not by a comment."""

    parameters = set(inspect.signature(compute_context_quality_score).parameters)
    assert parameters == {"evidence_strength", "recency_coverage", "outcome_stability"}

    banned = {"confidence", "decision_confidence", "classification", "response", "certainty"}
    assert not (parameters & banned), (
        "context_quality_score is reading a property of the system's own answer again. That is "
        "how a turn talks itself past its own safety gate."
    )
    assert set(CONTEXT_QUALITY_WEIGHTS) == parameters
    assert sum(CONTEXT_QUALITY_WEIGHTS.values()) == pytest.approx(1.0)


def test_a_cold_corpus_lands_below_the_escalation_line() -> None:
    """Property 2 -- the pause itself.

    With no evidence the score must sit below ``context_retrieval_escalate_score`` by a real
    margin, not by a rounding error, because that inequality is the whole reason a cold turn asks
    its owner before acting.
    """

    score = compute_context_quality_score(
        evidence_strength=COLD_EVIDENCE,
        recency_coverage=COLD_RECENCY,
        outcome_stability=COLD_STABILITY,
    )
    assert score < TRIGGER_SCORE, "a cold corpus must still trigger bounded retrieval"
    assert score <= ESCALATE_SCORE - 0.15, (
        f"a cold-corpus turn scores {score}, within 0.15 of the {ESCALATE_SCORE} escalation line. "
        "The owner stops being asked before anything else visibly breaks."
    )


def test_the_confidence_of_the_reply_is_no_longer_reachable_from_the_score() -> None:
    """The regression in numbers.

    The old formula added ``0.40 * decision_confidence``.  Two cold turns whose only difference
    is how assertive the reply sounded -- 0.693 against 0.7255, both real values from the Y1
    five-turn script -- used to score 0.4672 and 0.4802, and a DECISIVE rewrite pushed that as
    high as 0.6272, over the line.  Same context now means the same score, whatever the answer
    sounded like.
    """

    cold = dict(
        evidence_strength=COLD_EVIDENCE,
        recency_coverage=COLD_RECENCY,
        outcome_stability=COLD_STABILITY,
    )
    score = compute_context_quality_score(**cold)

    for laundered_confidence in (0.693, 0.7255, 0.92, 1.0):
        old_formula = round((0.40 * laundered_confidence) + (0.60 * score), 4)
        assert old_formula != score or laundered_confidence == score, (
            "the old weighting is being reproduced"
        )
    # The score is a pure function of the three context measurements: nothing about the reply is
    # an argument, so no confidence value can change this number.
    assert compute_context_quality_score(**cold) == score


@pytest.mark.parametrize("moved", sorted(CONTEXT_QUALITY_WEIGHTS))
def test_each_context_input_actually_moves_the_score(moved: str) -> None:
    """Property 3 -- discrimination.

    Perturb one input at a time; the score must move by exactly that input's weight.  Without
    this, hardcoding the score to 0 would pass the two tests above and shut the product down.
    """

    floor = dict(evidence_strength=0.0, recency_coverage=0.0, outcome_stability=0.0)
    raised = dict(floor, **{moved: 1.0})

    assert compute_context_quality_score(**floor) == 0.0
    assert compute_context_quality_score(**raised) == pytest.approx(
        CONTEXT_QUALITY_WEIGHTS[moved], abs=1e-4
    )


def test_a_warm_corpus_clears_the_line_so_the_gate_is_not_a_shutdown() -> None:
    """The other direction: real evidence must be able to buy autonomy back.

    Six-plus prior decisions above the similarity floor, days rather than months old, and no
    recent failures -- that turn must not be forced to ask, or "ask the owner" degenerates into
    "never act", which is the failure mode the escalation-cause design warns about.
    """

    warm = compute_context_quality_score(
        evidence_strength=1.0,
        recency_coverage=0.97,  # ~90-day half-life, evidence a few days old
        outcome_stability=1.0,
    )
    assert warm > ESCALATE_SCORE
    assert warm > TRIGGER_SCORE, "a well-evidenced turn should not force bounded retrieval"

    # And evidence alone is not enough: the same corpus gone stale, with a failure behind it,
    # drops back under the line.
    stale = compute_context_quality_score(
        evidence_strength=0.3333,
        recency_coverage=0.25,
        outcome_stability=0.6667,
    )
    assert stale < ESCALATE_SCORE


# --------------------------------------------------------------------------------------
# The other half: the score has to be able to SEE the corpus
# --------------------------------------------------------------------------------------


def _snapshot(row_count: int, *, age_days: float, options: tuple[str, ...] = ()) -> tuple[int, float]:
    """Run the real policy over ``row_count`` prior decisions and read what the score reads."""

    import importlib.util
    import sys
    from pathlib import Path

    from tce_shared.decision_policy import context_evidence_snapshot, decide

    # The policy's own request/row builders, reused rather than re-typed.
    fixtures_path = Path(__file__).with_name("test_decision_policy.py")
    spec = importlib.util.spec_from_file_location("_policy_fixtures", fixtures_path)
    assert spec is not None and spec.loader is not None
    fixtures = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_policy_fixtures", fixtures)
    spec.loader.exec_module(fixtures)

    rows = [fixtures._row(index, age_days=age_days) for index in range(row_count)]
    request = fixtures._request(rows=rows, options=options)
    return context_evidence_snapshot(request, decide(request))


def test_the_score_can_still_see_a_corpus_that_offers_no_candidate_options() -> None:
    """``decide()`` abstains at stage 2 on a turn with no candidate options -- every ordinary
    takeover turn -- and that exit reports empty adequacy no matter what the corpus holds.

    Reading the context's quality out of that abstention makes the score a constant pinned under
    the escalation line, so every turn on every corpus escalates forever.  "Always ask the human"
    is a shutdown, not a gate, and it fails as loudly as the opposite defect.  So the evidence is
    read from the request.
    """

    empty_count, _ = _snapshot(0, age_days=3.0)
    assert empty_count == 0

    warm_count, warm_age = _snapshot(6, age_days=3.0)
    assert warm_count > 0, (
        "a corpus with six relevant prior decisions reports zero evidence, so context quality is "
        "a constant and every turn escalates regardless of what is known."
    )
    assert warm_age == pytest.approx(3.0, abs=0.5)

    # And offering candidate options -- the case that does reach the policy's own adequacy --
    # must not report *less* evidence than the same rows without them.
    with_options_count, _ = _snapshot(6, age_days=3.0, options=("pause and verify", "force push to prod"))
    assert with_options_count >= warm_count


def test_a_corpus_worth_trusting_scores_above_the_line_and_a_stale_one_does_not() -> None:
    """End to end through the real policy: the score separates the cases it exists to separate."""

    import math

    def score(row_count: int, age_days: float) -> float:
        count, age = _snapshot(row_count, age_days=age_days)
        return compute_context_quality_score(
            evidence_strength=min(1.0, count / 6.0),
            recency_coverage=(math.exp(-math.log(2) * age / 90.0) if count > 0 else 0.2),
            outcome_stability=1.0,
        )

    cold = score(0, 3.0)
    warm = score(6, 3.0)
    stale = score(6, 400.0)

    assert cold <= ESCALATE_SCORE - 0.15, "a cold corpus must reach its owner"
    assert warm > TRIGGER_SCORE, "six fresh prior decisions must be able to buy autonomy back"
    assert cold < stale < warm, (
        f"the score does not order the three corpora it exists to tell apart: "
        f"cold={cold} stale={stale} warm={warm}"
    )
