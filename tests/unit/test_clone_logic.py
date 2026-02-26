from uuid import uuid4

from tce_api.clone import arbitrate, derive_clone_guidance
from tce_api.schemas import ContextBundleResponse, PatternItem


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


def test_weak_evidence_triggers_caution():
    summary, actions, confidence, strength, flags = derive_clone_guidance(_bundle(1, 0.4), None)
    assert "No strong prior found" in summary
    assert strength == "weak"
    assert actions
    assert confidence >= 0.0
    assert isinstance(flags, list)


def test_arbitration_prefers_human_override():
    result = arbitrate(
        executor_plan="do A",
        advisor_input="do B",
        human_override="do C",
        interaction_id="i-1",
    )
    assert result.decision_source == "human_override"
    assert "do C" in result.final_guidance
