from dataclasses import replace
from uuid import uuid4

from tce_api.clone import arbitrate, derive_clone_guidance
from tce_api.schemas import ContextBundleResponse, PatternItem
from tce_shared.decision_policy import Adequacy, evidence_strength_label


def _bundle(citations: int, confidence: float) -> ContextBundleResponse:
    pattern = PatternItem(
        id=uuid4(),
        domain="coding",
        pattern_type="workflow",
        statement="Start with backend contracts",
        confidence=confidence,
        status="active",
        evidence_event_ids=[uuid4()],
    )
    return ContextBundleResponse(
        summary="bundle",
        top_patterns=[pattern],
        relevant_workflows=[],
        evidence_events=[],
        do_dont={"do": ["start with contracts"], "dont": ["skip tests"]},
        citations=[uuid4() for _ in range(citations)],
        policy={},
        schema_version=1,
    )


def test_weak_evidence_does_not_rewrite_the_summary():
    """B1: the summary is the bundle's, always.

    This case used to assert the opposite -- that weak evidence replaced ``bundle.summary``
    with a fixed "no strong prior" sentence.  That sentence was then pattern-matched by
    ``ensure_takeover_response`` two modules away, where it triggered a *more* decisive rewrite:
    the lowest-evidence turns produced the most confident text, and the resulting DECISIVE
    classification raised the very ``decision_confidence`` that gates ``needs_human``.  The
    suppression fed the gate that would have caught the suppression.

    A shortage of evidence now travels as ``policy_decision.abstain_reason`` and
    ``reason_for_asking`` -- fields -- not as a string smuggled into a summary.
    """

    bundle = _bundle(1, 0.4)
    summary, actions, flags = derive_clone_guidance(bundle, None)
    assert summary == bundle.summary
    assert actions
    assert isinstance(flags, list)


def test_evidence_strength_label_reads_only_evidence():
    """The label comes from the evidence that was voted on, and from nothing a model said.

    It used to be derived from ``len(bundle.citations)`` -- timeline event ids, which are
    retrieval provenance and explicitly not the evidence for a choice -- crossed with an
    average of pattern confidences that the advisor's own self-reported number then overwrote.
    """

    strong = Adequacy(
        eligible_count=12,
        neighbour_count=8,
        above_floor_count=5,
        effective_sample_size=4.0,
        top_similarity=0.8,
        top_overlap=0.6,
        agreement_share=0.9,
        learning_eligible_count=5,
        median_evidence_age_days=10.0,
        adequate=True,
        shortfalls=(),
    )
    weak = replace(strong, above_floor_count=1, effective_sample_size=1.0, agreement_share=0.4)

    assert evidence_strength_label(strong) == "strong"
    assert evidence_strength_label(weak) == "weak"
    # Same evidence, a different number attached to it, same label: there is no confidence
    # input to read.
    assert evidence_strength_label(replace(strong, top_similarity=0.1)) == "strong"


def test_arbitration_prefers_human_override():
    result = arbitrate(
        executor_plan="do A",
        advisor_input="do B",
        human_override="do C",
        interaction_id="i-1",
    )
    assert result.decision_source == "human_override"
    assert "do C" in result.final_guidance
